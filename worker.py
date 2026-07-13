import os
import asyncio
from datetime import datetime, timezone
from typing import Callable, Any, Dict
from openai import AsyncOpenAI
from arq import cron
from arq.connections import RedisSettings

from database import ClientDatabaseManager
from services.whatsapp import send_text, send_audio
from services.agent import generate_llm_response, format_text_response, generate_tts_audio

# EDW Step execution helper with retry and tracking
async def run_step_with_retry(
    step_name: str,
    execution_id: str,
    tenant_supabase: Any,
    worker_func: Callable[[], Any],
    input_data: Dict[str, Any] = None,
    max_retries: int = 3
) -> Any:
    """
    Runs a workflow step, registers executions in workflow_step_executions,
    and executes retry logic with exponential backoff + jitter.
    """
    attempt = 1
    delay = 1.0
    
    while attempt <= max_retries:
        # Create running step execution record
        step_exec = tenant_supabase.table("workflow_step_executions").insert({
            "execution_id": execution_id,
            "step_name": step_name,
            "status": "RUNNING",
            "attempt": attempt,
            "input_data": input_data,
            "started_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        
        step_id = step_exec.data[0]["id"]
        
        try:
            # Execute logic
            result = await worker_func()
            
            # Record success
            tenant_supabase.table("workflow_step_executions").update({
                "status": "SUCCESS",
                "output_data": result if isinstance(result, dict) else {"result": str(result)},
                "completed_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", step_id).execute()
            
            return result
            
        except Exception as e:
            error_msg = str(e)
            
            # Record failure
            tenant_supabase.table("workflow_step_executions").update({
                "status": "FAILED",
                "error_details": error_msg,
                "completed_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", step_id).execute()
            
            if attempt == max_retries:
                raise e
            
            # Exponential backoff + jitter
            await asyncio.sleep(delay + (asyncio.subprocess.sys.float_info.min or 0.1))
            delay *= 2
            attempt += 1

# ARQ Worker Task
async def process_whatsapp_response(ctx: Dict[str, Any], client_id: str, phone: str, aggregated_text: str, execution_id: str):
    tenant_supabase = ClientDatabaseManager.get_client(client_id)
    openai_client = ctx["openai"]
    
    # Update master workflow status to RUNNING
    tenant_supabase.table("workflow_executions").update({
        "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", execution_id).execute()
    
    try:
        # Fetch configurations
        config = ClientDatabaseManager.get_client_config(client_id)
        prompt_id = config.get("prompt_id")
        
        # Fetch LLM configurations (defaulting to n8n parameters if not present)
        llm_model = config.get("llm_model") or "gpt-4"
        llm_temp = config.get("llm_temperature")
        llm_temperature = float(llm_temp) if llm_temp is not None else 0.8
        
        fallback_llm_model = config.get("fallback_llm_model") or "gpt-4o-mini"
        fallback_llm_temp = config.get("fallback_llm_temperature")
        fallback_llm_temperature = float(fallback_llm_temp) if fallback_llm_temp is not None else 0.7
        
        # 1. Blacklist check
        async def blacklist_check():
            res = tenant_supabase.table("Blacklist_Mindflow").select("id").eq("Número", phone).execute()
            if res.data:
                raise ValueError("USER_BLACKLISTED")
            return {"status": "clean"}
            
        try:
            await run_step_with_retry("whatsapp_flow_blacklist_check", execution_id, tenant_supabase, blacklist_check, {"phone": phone})
        except ValueError as e:
            if str(e) == "USER_BLACKLISTED":
                # Finish master as SUCCESS (business outcome: ignored blacklisted)
                tenant_supabase.table("workflow_executions").update({
                    "status": "SUCCESS",
                    "output_data": {"outcome": "ignored_blacklisted"},
                    "completed_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", execution_id).execute()
                return

        # 2. Check and Create Lead
        async def lead_check_and_create():
            res = tenant_supabase.table("Leads_Mindflow").select("*").eq("Número", phone).execute()
            lead_data = res.data[0] if res.data else None
            
            if not lead_data:
                # Create lead
                res_create = tenant_supabase.table("Leads_Mindflow").insert({
                    "Número": phone,
                    "created_at": datetime.now(timezone.utc).isoformat()
                }).execute()
                lead_data = res_create.data[0]
                created = True
            else:
                created = False
            return {"lead": lead_data, "created": created}
            
        lead_res = await run_step_with_retry("whatsapp_flow_lead_check_create", execution_id, tenant_supabase, lead_check_and_create, {"phone": phone})
        lead = lead_res["lead"]

        # 3. Update Last Message Time
        async def update_last_msg():
            tenant_supabase.table("Leads_Mindflow").update({
                "data ultima msgm": datetime.now(timezone.utc).isoformat(),
                "Ultima msgm (texto)": aggregated_text
            }).eq("Número", phone).execute()
            return {"status": "updated"}
            
        await run_step_with_retry("whatsapp_flow_update_last_msg_time", execution_id, tenant_supabase, update_last_msg, {"phone": phone, "message": aggregated_text})

        # 4. Fetch Prompt
        async def fetch_prompt():
            if not prompt_id:
                raise ValueError("prompt_id not configured for this client.")
            res = tenant_supabase.table("Prompts").select("Prompt_Text").eq("id", prompt_id).single().execute()
            return {"prompt": res.data["Prompt_Text"]}
            
        prompt_res = await run_step_with_retry("whatsapp_flow_fetch_prompt", execution_id, tenant_supabase, fetch_prompt, {"prompt_id": prompt_id})
        system_prompt = prompt_res["prompt"]

        # 5. Load Chat History
        async def load_chat_history():
            # Query n8n_chat_histories table inside client's DB
            try:
                res = tenant_supabase.table("n8n_chat_histories")\
                    .select("message, id")\
                    .eq("session_id", phone)\
                    .order("id", desc=True)\
                    .limit(10)\
                    .execute()
                # Reverse to get chronological order
                raw_history = list(reversed(res.data))
                history = []
                for row in raw_history:
                    msg = row.get("message") or {}
                    msg_type = msg.get("type")
                    content = msg.get("content", "")
                    role = "user" if msg_type == "human" else "assistant"
                    history.append({"role": role, "content": content})
            except Exception:
                # If table does not exist or fails, return empty list
                history = []
            return {"history": history}
            
        history_res = await run_step_with_retry("whatsapp_flow_load_history", execution_id, tenant_supabase, load_chat_history, {"phone": phone})
        chat_history = history_res["history"]

        # 6. Generate Agent Response (LLM)
        async def call_agent_llm():
            res = await generate_llm_response(
                openai_client,
                system_prompt,
                chat_history,
                aggregated_text,
                model=llm_model,
                temperature=llm_temperature,
                fallback_model=fallback_llm_model,
                fallback_temperature=fallback_llm_temperature
            )
            return res
            
        response_data = await run_step_with_retry("whatsapp_flow_llm_response", execution_id, tenant_supabase, call_agent_llm, {"message": aggregated_text})
        response_type = response_data.get("type", "texto")
        output_text = response_data.get("output", "")

        # 7. Dispatch Response (Text / Audio)
        if response_type == "audio":
            # Generate Audio
            async def generate_audio():
                audio_b64 = await generate_tts_audio(openai_client, output_text)
                return {"audio_b64_len": len(audio_b64), "audio_b64": audio_b64}
                
            audio_res = await run_step_with_retry("whatsapp_flow_tts_generation", execution_id, tenant_supabase, generate_audio, {"text": output_text})
            audio_b64 = audio_res["audio_b64"]
            
            # Send Audio
            async def send_audio_zapi():
                res = await send_audio(config, phone, audio_b64)
                return res
                
            await run_step_with_retry("whatsapp_flow_send_audio", execution_id, tenant_supabase, send_audio_zapi, {"phone": phone})
        else:
            # Format text into short messages
            async def format_messages():
                msgs = await format_text_response(openai_client, output_text)
                return {"messages": msgs}
                
            format_res = await run_step_with_retry("whatsapp_flow_format_text_response", execution_id, tenant_supabase, format_messages, {"text": output_text})
            messages_to_send = format_res["messages"]
            
            # Send each text message with 1.5s delay
            async def send_text_messages():
                sent_results = []
                for i, msg in enumerate(messages_to_send):
                    if i > 0:
                        await asyncio.sleep(1.5)
                    res = await send_text(config, phone, msg)
                    sent_results.append(res)
                return {"sent": sent_results}
                
            await run_step_with_retry("whatsapp_flow_send_messages", execution_id, tenant_supabase, send_text_messages, {"phone": phone, "messages": messages_to_send})

        # 8. Save Memory
        async def save_chat_memory():
            try:
                # Add user message
                tenant_supabase.table("n8n_chat_histories").insert({
                    "session_id": phone,
                    "message": {
                        "type": "human",
                        "content": aggregated_text,
                        "additional_kwargs": {},
                        "response_metadata": {}
                    },
                    "created_at": datetime.now(timezone.utc).isoformat()
                }).execute()
                # Add assistant response
                tenant_supabase.table("n8n_chat_histories").insert({
                    "session_id": phone,
                    "message": {
                        "type": "ai",
                        "content": output_text,
                        "additional_kwargs": {},
                        "response_metadata": {}
                    },
                    "created_at": datetime.now(timezone.utc).isoformat()
                }).execute()
            except Exception as e:
                return {"status": "failed_saving_memory", "error": str(e)}
            return {"status": "saved"}
            
        await run_step_with_retry("whatsapp_flow_save_memory", execution_id, tenant_supabase, save_chat_memory, {"phone": phone})

        # Finish master as SUCCESS
        tenant_supabase.table("workflow_executions").update({
            "status": "SUCCESS",
            "output_data": {"outcome": f"responded_via_{response_type}", "response": output_text},
            "completed_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", execution_id).execute()
        
    except Exception as e:
        # Finish master as FAILED
        tenant_supabase.table("workflow_executions").update({
            "status": "FAILED",
            "error_details": str(e),
            "completed_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", execution_id).execute()
        raise e

# Startup / Shutdown Hooks
async def startup(ctx):
    openai_key = os.getenv("OPENAI_API_KEY") or os.getenv("Openai_api_key")
    if not openai_key:
        raise ValueError("OPENAI_API_KEY (or Openai_api_key) must be set in the environment")
    ctx["openai"] = AsyncOpenAI(api_key=openai_key)

async def shutdown(ctx):
    if "openai" in ctx:
        await ctx["openai"].close()

# ARQ Worker Settings
class WorkerSettings:
    functions = [process_whatsapp_response]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://localhost:6379"))
