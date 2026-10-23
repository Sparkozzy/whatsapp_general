import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Callable, Any, Dict, Optional
from openai import AsyncOpenAI

from arq import cron
from arq.connections import RedisSettings

from database import ClientDatabaseManager, master_supabase
from services.whatsapp import send_text, send_audio
from services.agent import (
    generate_llm_response,
    generate_llm_response_with_mcp,
    format_text_response,
    generate_tts_audio,
    generate_fup_decision
)
import httpx


def _clean_null_bytes(val: Any) -> Any:
    if isinstance(val, str):
        return val.replace("\x00", "")
    elif isinstance(val, dict):
        return {k: _clean_null_bytes(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [_clean_null_bytes(item) for item in val]
    return val

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
    safe_input = _clean_null_bytes(input_data)
    
    while attempt <= max_retries:
        # Create running step execution record
        step_exec = tenant_supabase.table("workflow_step_executions").insert({
            "execution_id": execution_id,
            "step_name": step_name,
            "status": "RUNNING",
            "attempt": attempt,
            "input_data": safe_input,
            "started_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        
        step_id = step_exec.data[0]["id"]
        
        try:
            # Execute logic
            result = await worker_func()
            safe_output = _clean_null_bytes(result if isinstance(result, dict) else {"result": str(result)})
            
            # Record success
            tenant_supabase.table("workflow_step_executions").update({
                "status": "SUCCESS",
                "output_data": safe_output,
                "completed_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", step_id).execute()
            
            return result
            
        except Exception as e:
            error_msg = _clean_null_bytes(str(e))
            
            # Record failure
            tenant_supabase.table("workflow_step_executions").update({
                "status": "FAILED",
                "error_details": error_msg,
                "completed_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", step_id).execute()
            
            if attempt == max_retries:
                raise e

            
            # Exponential backoff + jitter
            import sys
            await asyncio.sleep(delay + (sys.float_info.min or 0.1))
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
        voice_id = config.get("voice_id") or "nova"
        
        # TTS Provider configs (Supabase)
        tts_provider = config.get("tts_provider") or "openai"
        tts_format = config.get("tts_format") or "mp3"
        tts_latency = config.get("tts_latency") or "normal"
        
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
            res = master_supabase.table("Prompts").select("Prompt_Text").eq("id", prompt_id).single().execute()
            data = res.data or {}
            if isinstance(data, dict):
                raw_prompt = data.get("Prompt_Text", "") or ""
            else:
                raw_prompt = str(data)
            
            from zoneinfo import ZoneInfo
            now_br = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y %H:%M:%S")
            
            mutaveis_section = (
                f"\n\n<informações_mutáveis>\n"
                f"Horario atual (brasilia): {now_br}\n"
                f"Número de celular: {phone}\n"
                f"</informações_mutáveis>"
            )
            
            lead_name = (lead.get("Nome") if isinstance(lead, dict) else None) or "Cliente"
            lead_email = (lead.get("Email") if isinstance(lead, dict) else None) or "."
            
            formatted_prompt = (
                str(raw_prompt)
                .replace("{{$now}}", now_br)
                .replace("{{now}}", now_br)
                .replace("{{number}}", phone)
                .replace("{{phone}}", phone)
                .replace("{{sender_phone}}", phone)
                .replace("{{numero_do_lead}}", phone)
                .replace("{{client_id}}", str(client_id))
                .replace("{{lead_name}}", lead_name)
                .replace("{{customer_name}}", lead_name)
                .replace("{{email}}", lead_email)
            ) + mutaveis_section
            
            return {"prompt": formatted_prompt}
            
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
                    if isinstance(content, dict):
                        content = content.get("output") or content.get("text") or str(content)
                    elif not isinstance(content, str):
                        content = str(content)
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
            client_mcp_urls = config.get("mcp_urls")
            if not client_mcp_urls or not isinstance(client_mcp_urls, list):
                env_mcp = os.getenv("SCHEDULE_MCP_URL")
                client_mcp_urls = [env_mcp] if env_mcp else ["https://schedule-github.bkpxmb.easypanel.host/mcp"]

            mcp_api_key = os.getenv("SCHEDULE_MCP_API_KEY") or "mf_sk_2026_pre_call_xK9v3Qm7bR4wT1nZ"

            if client_mcp_urls:
                res = await generate_llm_response_with_mcp(
                    openai_client,
                    system_prompt,
                    chat_history,
                    aggregated_text,
                    mcp_urls=client_mcp_urls,
                    mcp_api_key=mcp_api_key,
                    model=llm_model,
                    temperature=llm_temperature,
                    execution_id=execution_id,
                    tenant_supabase=tenant_supabase
                )
            else:
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
                if tts_provider == "fish":
                    from services.agent import generate_fish_audio
                    try:
                        audio_b64 = await generate_fish_audio(
                            text=output_text, 
                            model_id=voice_id,
                            audio_format=tts_format,
                            latency=tts_latency
                        )
                    except Exception as e:
                        print(f"Fallback para OpenAI TTS devido a erro na Fish Audio: {e}")
                        audio_b64 = await generate_tts_audio(openai_client, output_text, voice="nova")
                else:
                    try:
                        audio_b64 = await generate_tts_audio(openai_client, output_text, voice=voice_id)
                    except Exception as e:
                        print(f"Erro na OpenAI com a voz '{voice_id}': {e}. Usando 'nova' como fallback de segurança.")
                        audio_b64 = await generate_tts_audio(openai_client, output_text, voice="nova")
                    
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

        # Trigger FUP flow safely scheduled after 10 minutes of inactivity (Redis-First: zero Supabase write)
        try:
            redis_pool = ctx.get("redis")
            clean_phone = "".join([c for c in phone if c.isdigit()])
            fup_job_id = f"fup_check_{client_id}_{clean_phone}"
            fup_target_time = datetime.now(timezone.utc) + timedelta(minutes=10)
            if redis_pool:
                # Remove agendamento anterior para garantir 1 único registro no Redis
                await redis_pool.zrem("arq:queue", fup_job_id)
                await redis_pool.delete(f"arq:job:{fup_job_id}")
                await redis_pool.enqueue_job(
                    "process_fup_request",
                    client_id,
                    phone,
                    None,  # execution_id is None; created in Supabase only if 10min inactivity check passes
                    1,
                    _job_id=fup_job_id,
                    _defer_until=fup_target_time
                )
                await redis_pool.set(f"fup_active_job:{client_id}:{clean_phone}", fup_job_id, ex=86400)
            else:
                asyncio.create_task(
                    process_fup_request(
                        ctx=ctx,
                        client_id=client_id,
                        phone=phone,
                        execution_id=None,
                        attempt=1
                    )
                )
        except Exception as fup_err:
            print(f"[FUP Trigger Warning] Failed to schedule FUP in Redis: {fup_err}")

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


# ==========================================
# FUP AGENT WORKFLOW TASKS
# ==========================================

async def execute_scheduled_fup_action(
    ctx: Dict[str, Any],
    client_id: str,
    phone: str,
    action_type: str,
    content: str,
    execution_id: str,
    attempt: int = 1
):
    """
    Executa a ação de FUP previamente agendada no Redis ARQ (_defer_until).
    Ações suportadas: 'agendar_mensagem', 'figurinha', 'ligawhats', 'ligacao'.
    """
    tenant_supabase = ClientDatabaseManager.get_client(client_id)
    config = ClientDatabaseManager.get_client_config(client_id)

    step_name = f"fup_flow_execute_{action_type}"
    
    # 0. Verificação de segurança robusta:
    # a) Se o lead interagiu nos últimos 10 minutos OU após a criação desta execução, cancela a ação de FUP.
    # b) Anti-duplicidade/Cooldown: Se já houve outro FUP disparado nos últimos 30 minutos, cancela.
    # c) Limite de tentativas: Se o lead já recebeu >= quantidade_fup_whats no ciclo atual, cancela.
    try:
        res_lead = tenant_supabase.table("Leads_Mindflow").select('"data ultima msgm"').eq("Número", phone).limit(1).execute()
        lead_last_dt = None
        if res_lead.data and res_lead.data[0].get("data ultima msgm"):
            lead_last_dt = datetime.fromisoformat(res_lead.data[0]["data ultima msgm"])
            if lead_last_dt.tzinfo is None:
                lead_last_dt = lead_last_dt.replace(tzinfo=timezone.utc)

            # a) Se interagiu nos últimos 10 minutos
            if (datetime.now(timezone.utc) - lead_last_dt).total_seconds() < 600:
                print(f"[execute_scheduled_fup_action] Lead {phone} interagiu nos últimos 10 minutos. Cancelando envio agendado de FUP.")
                tenant_supabase.table("workflow_executions").update({
                    "output_data": {"outcome": "cancelled_lead_interacted_recently"},
                    "completed_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", execution_id).execute()
                return {"status": "cancelled", "reason": "lead_interacted_recently"}

        # Consulta histórico de mensagens recentes do lead para validar contagem real e cooldown
        history_query = tenant_supabase.table("n8n_chat_histories")\
            .select("id, created_at, message")\
            .eq("session_id", phone)\
            .order("created_at", desc=True)\
            .limit(20)\
            .execute()

        recent_msgs = history_query.data or []
        fup_msgs_after_lead = []
        last_fup_time = None

        for m in recent_msgs:
            m_time_str = m.get("created_at")
            if not m_time_str:
                continue
            m_dt = datetime.fromisoformat(m_time_str)
            if m_dt.tzinfo is None:
                m_dt = m_dt.replace(tzinfo=timezone.utc)

            # Se a mensagem for anterior à última resposta do lead, para a contagem do ciclo
            if lead_last_dt and m_dt <= lead_last_dt:
                break

            msg_payload = m.get("message") or {}
            content_str = msg_payload.get("content", "")
            add_kwargs = msg_payload.get("additional_kwargs") or {}

            if add_kwargs.get("fup_action") or (isinstance(content_str, str) and content_str.startswith("[Follow-up")):
                fup_msgs_after_lead.append((m_dt, m))
                if last_fup_time is None:
                    last_fup_time = m_dt

        # b) Trava de Idempotência por Tentativa:
        # Se a mesma tentativa (ex: Tentativa 3) já foi executada neste ciclo de inatividade, cancela a duplicata!
        for _, prev_fup in fup_msgs_after_lead:
            prev_kwargs = (prev_fup.get("message") or {}).get("additional_kwargs") or {}
            prev_content = (prev_fup.get("message") or {}).get("content") or ""
            prev_attempt = prev_kwargs.get("attempt")
            if prev_attempt is not None and int(prev_attempt) == int(attempt):
                print(f"[execute_scheduled_fup_action] Tentativa #{attempt} já foi executada para o lead {phone} neste ciclo. Cancelando disparo redundante.")
                tenant_supabase.table("workflow_executions").update({
                    "output_data": {"outcome": f"cancelled_attempt_{attempt}_already_executed"},
                    "completed_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", execution_id).execute()
                return {"status": "cancelled", "reason": f"attempt_{attempt}_already_executed"}
            elif f"Tentativa {attempt}" in str(prev_content):
                print(f"[execute_scheduled_fup_action] Tentativa #{attempt} já identificada no conteúdo para o lead {phone}. Cancelando disparo redundante.")
                tenant_supabase.table("workflow_executions").update({
                    "output_data": {"outcome": f"cancelled_attempt_{attempt}_already_executed"},
                    "completed_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", execution_id).execute()
                return {"status": "cancelled", "reason": f"attempt_{attempt}_already_executed"}

        # Trava anti-duplicidade imediata (caso 2 workers acordem no mesmo segundo):
        # Se um FUP foi gravado há menos de 3 minutos (180s), aborta para evitar colisão milissegundo a milissegundo
        if last_fup_time and (datetime.now(timezone.utc) - last_fup_time).total_seconds() < 180:
            print(f"[execute_scheduled_fup_action] Lead {phone} já recebeu FUP há menos de 3 minutos ({last_fup_time}). Cancelando colisão simultânea.")
            tenant_supabase.table("workflow_executions").update({
                "output_data": {"outcome": "cancelled_fup_immediate_collision", "last_fup_time": last_fup_time.isoformat()},
                "completed_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", execution_id).execute()
            return {"status": "cancelled", "reason": "fup_immediate_collision"}

        # c) Limite real de tentativas do tenant
        max_fup = config.get("quantidade_fup_whats")
        try:
            max_fup = int(max_fup) if max_fup is not None else 3
        except Exception:
            max_fup = 3
        if max_fup <= 0:
            max_fup = 3

        if len(fup_msgs_after_lead) >= max_fup:
            print(f"[execute_scheduled_fup_action] Lead {phone} já recebeu {len(fup_msgs_after_lead)} FUPs (máximo={max_fup}). Cancelando.")
            tenant_supabase.table("workflow_executions").update({
                "output_data": {"outcome": "cancelled_max_fup_reached", "fup_count": len(fup_msgs_after_lead)},
                "completed_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", execution_id).execute()
            return {"status": "cancelled", "reason": "max_fup_reached"}

    except Exception as check_err:
        print(f"[execute_scheduled_fup_action] Aviso ao checar segurança de FUP: {check_err}")

    async def do_execute():
        if action_type == "agendar_mensagem":
            res = await send_text(config, phone, content)
            return {"status": "sent", "type": "text", "res": res}

        elif action_type == "audio":
            # Geração de áudio TTS (OpenAI TTS ou Fish Audio) e envio via Z-API
            voice_id = config.get("voice_id") or "nova"
            tts_provider = config.get("tts_provider") or "openai"
            tts_format = config.get("tts_format") or "mp3"
            tts_latency = config.get("tts_latency") or "normal"
            openai_client = ctx["openai"]

            if tts_provider == "fish":
                from services.agent import generate_fish_audio
                try:
                    audio_b64 = await generate_fish_audio(
                        text=content,
                        model_id=voice_id,
                        audio_format=tts_format,
                        latency=tts_latency
                    )
                except Exception as fish_err:
                    print(f"Fallback para OpenAI TTS devido a erro na Fish Audio: {fish_err}")
                    audio_b64 = await generate_tts_audio(openai_client, content, voice="nova")
            else:
                try:
                    audio_b64 = await generate_tts_audio(openai_client, content, voice=voice_id)
                except Exception as openai_err:
                    print(f"Erro na OpenAI com a voz '{voice_id}': {openai_err}. Usando 'nova' como fallback.")
                    audio_b64 = await generate_tts_audio(openai_client, content, voice="nova")

            res = await send_audio(config, phone, audio_b64)
            return {"status": "sent", "type": "audio", "voice": voice_id, "res": res}

        elif action_type == "figurinha":

            # Envio de figurinha / sticker via Z-API
            instance_id = config.get("zapi_instance_id")
            client_token = config.get("zapi_client_token")
            security_token = config.get("zapi_security_token")
            
            if not instance_id or not client_token:
                raise ValueError("Missing Z-API credentials for sticker dispatch.")
                
            url = f"https://api.z-api.io/instances/{instance_id}/token/{client_token}/send-sticker"
            headers = {"Client-Token": security_token} if security_token else {}
            payload = {"phone": phone, "sticker": content}
            
            async with httpx.AsyncClient(timeout=20.0) as client:
                res = await client.post(url, json=payload, headers=headers)
                res.raise_for_status()
                return {"status": "sent", "type": "sticker", "res": res.json()}
                
        elif action_type == "ligawhats":
            # Disparo de chamada via WhatsApp
            url = config.get("pre_call_processing_url") or "https://call-github.bkpxmb.easypanel.host/call"
            api_token = config.get("mindflow_api_token")
            headers = {"X-MindFlow-Token": api_token} if api_token else {}
            payload = {
                "client_id": client_id,
                "numero": phone,
                "canal": "whats",
                "from_workflow": "fup_flow_ligawhats",
                "execution_id": execution_id
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, json=payload, headers=headers)
                return {"status": "dispatched", "type": "ligawhats", "code": res.status_code}
                
        elif action_type == "ligacao":
            # Disparo de ligação telefônica convencional (Retell / pre_call)
            url = config.get("pre_call_processing_url") or "https://call-github.bkpxmb.easypanel.host/webhook"
            if not url.endswith("/webhook"):
                url = url.rstrip("/") + "/webhook"
            api_token = config.get("mindflow_api_token")
            pre_call_api_key = os.getenv("PRE_CALL_API_KEY", "mf_sk_2026_pre_call_xK9v3Qm7bR4wT1nZ")
            headers = {
                "X-API-Key": pre_call_api_key,
                "Content-Type": "application/json"
            }
            if api_token:
                headers["X-MindFlow-Token"] = api_token
            
            # Recupera dados cadastrais do lead para preencher os campos obrigatórios do pre_call
            try:
                res_lead_call = tenant_supabase.table("Leads_Mindflow").select("Nome, Email").eq("Número", phone).limit(1).execute()
                lead_data_call = res_lead_call.data[0] if res_lead_call.data else {}
            except Exception:
                lead_data_call = {}
                
            lead_name = lead_data_call.get("Nome") or "Cliente"
            lead_email = lead_data_call.get("Email") or f"{phone.replace('+', '')}@mindflow-ia.com"
            agent_id = config.get("agent_id_ligacao_whatsapp") or config.get("agent_id")
            prompt_id = config.get("prompt_id")
            
            payload = {
                "workflow_name": "fup_flow_ligacao",
                "execution_id": execution_id,
                "numero": phone,
                "nome": lead_name,
                "email": lead_email,
                "agent_id": agent_id,
                "Prompt_id": str(prompt_id) if prompt_id else None,
                "contexto": content or "Follow-up de reengajamento via ligação telefônica"
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, json=payload, headers=headers)
                return {"status": "dispatched", "type": "ligacao", "code": res.status_code, "res": res.text[:200]}
        elif action_type in ("nenhuma", "congelar", "cancelar"):
            return {"status": "completed", "outcome": "cadence_exhausted_or_skipped"}
        else:
            raise ValueError(f"Tipo de ação desconhecido: {action_type}")

    await run_step_with_retry(step_name, execution_id, tenant_supabase, do_execute, {"phone": phone, "action": action_type})

    # 1. Registrar ação executada no histórico de chat para memória contextual do LLM
    if action_type not in ("nenhuma", "congelar", "cancelar"):
        try:
            tenant_supabase.table("n8n_chat_histories").insert({
                "session_id": phone,
                "message": {
                    "type": "ai",
                    "content": f"[Follow-up {action_type} - Tentativa {attempt}]: {content}",
                    "additional_kwargs": {
                        "fup_action": action_type,
                        "attempt": attempt,
                        "execution_id": execution_id
                    },
                    "response_metadata": {}
                },
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()
        except Exception as hist_err:
            print(f"[execute_scheduled_fup_action] Aviso ao registrar histórico: {hist_err}")

    # 2. Encadeamento automático da próxima etapa da cadência de FUP
    if action_type not in ("nenhuma", "congelar", "cancelar"):
        max_fup = config.get("quantidade_fup_whats")
        try:
            max_fup = int(max_fup) if max_fup is not None else 3
        except Exception:
            max_fup = 3

        if max_fup <= 0:
            max_fup = 3

        next_attempt = attempt + 1
        if next_attempt <= max_fup:
            redis_pool = ctx.get("redis")
            if redis_pool:
                try:
                    next_exec = tenant_supabase.table("workflow_executions").insert({
                        "workflow_name": "fup_flow",
                        "status": "PENDING",
                        "input_data": {
                            "client_id": client_id,
                            "phone": phone,
                            "trigger": f"fup_cadence_step_{next_attempt}",
                            "previous_action": action_type,
                            "previous_execution_id": execution_id,
                            "attempt": next_attempt
                        }
                    }).execute()

                    if next_exec.data:
                        next_execution_id = next_exec.data[0]["id"]
                        clean_phone = "".join([c for c in phone if c.isdigit()])
                        next_job_id = f"fup_cadence_{client_id}_{clean_phone}_{next_attempt}"
                        await redis_pool.zrem("arq:queue", next_job_id)
                        await redis_pool.delete(f"arq:job:{next_job_id}")
                        await redis_pool.enqueue_job(
                            "process_fup_request",
                            client_id,
                            phone,
                            next_execution_id,
                            next_attempt,
                            _job_id=next_job_id
                        )
                        await redis_pool.set(f"fup_active_job:{client_id}:{clean_phone}", next_job_id, ex=86400)
                        print(f"[execute_scheduled_fup_action] Próxima etapa #{next_attempt} enfileirada no ARQ (Job: {next_job_id}, Exec: {next_execution_id}).")
                except Exception as chain_err:
                    print(f"[execute_scheduled_fup_action] Erro ao encadear próxima etapa #{next_attempt} do FUP: {chain_err}")


async def process_fup_request(
    ctx: Dict[str, Any],
    client_id: str,
    phone: str,
    execution_id: Optional[str] = None,
    attempt: int = 1,
    custom_context: Optional[str] = None
):
    """
    Workflow do Agente de FUP (fup_flow):
    1. Leitura de prompt de FUP e permissões do cliente no Supabase.
    2. Processamento do agente LLM determinístico (mini/micro GPT) com 3 retentativas.
    3. Leitura e processamento de resposta em JSON.
    4. Leitura e validação de permissões de uso na tabela client_configurations.
    5. Transformação de output em agendamento de ação no Redis ARQ (_defer_until).
    """
    tenant_supabase = ClientDatabaseManager.get_client(client_id)
    openai_client = ctx["openai"]
    redis_pool = ctx.get("redis")  # ARQ Pool injetado no startup
    config = ClientDatabaseManager.get_client_config(client_id)

    # 0. Verificação prévia: Se FUP desabilitado para o cliente, sai imediatamente (zero Supabase write)
    if config.get("fup") is False:
        print(f"[process_fup_request] FUP desabilitado para cliente {client_id}. Abortando sem criar execução.")
        return

    # 0.1 Verificação de inatividade prévia: Se o lead interagiu há menos de 10 min, aborta sem poluir o Supabase
    if not custom_context:
        try:
            res_lead_fast = tenant_supabase.table("Leads_Mindflow").select('"data ultima msgm"').eq("Número", phone).limit(1).execute()
            if res_lead_fast.data and res_lead_fast.data[0].get("data ultima msgm"):
                last_dt_fast = datetime.fromisoformat(res_lead_fast.data[0]["data ultima msgm"])
                if last_dt_fast.tzinfo is None:
                    last_dt_fast = last_dt_fast.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_dt_fast).total_seconds() < 600:
                    print(f"[process_fup_request] Lead {phone} interagiu há menos de 10 minutos. Abortando silenciosamente no Redis (Zero Supabase write).")
                    return
        except Exception as fast_check_err:
            print(f"[process_fup_request] Aviso checagem rápida: {fast_check_err}")

    # Cria ou atualiza o registro mestre de execução no Supabase APENAS SE passou na validação
    if not execution_id:
        res_exec = tenant_supabase.table("workflow_executions").insert({
            "workflow_name": "fup_flow",
            "status": "RUNNING",
            "input_data": {"client_id": client_id, "phone": phone, "trigger": "inactivity_timeout_10min", "attempt": attempt},
            "started_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        execution_id = res_exec.data[0]["id"]
    else:
        tenant_supabase.table("workflow_executions").update({
            "status": "RUNNING",
            "started_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", execution_id).execute()

    try:
        # 1. Leitura de config no Master e Prompt no Supabase
        async def fetch_prompt_and_config():
            config = ClientDatabaseManager.get_client_config(client_id)
            prompt_id = config.get("prompt_id")
            
            # Buscar prompt específico do cliente
            prompt_text = ""
            if prompt_id:
                try:
                    res_p = master_supabase.table("Prompts").select("Prompt_Text").eq("id", prompt_id).single().execute()
                    if res_p.data:
                        prompt_text = res_p.data.get("Prompt_Text", "")
                except Exception as p_err:
                    print(f"Não foi possível carregar prompt no master: {p_err}")

            if not prompt_text:
                # Tentar buscar no banco do cliente caso exista tabela Prompts
                try:
                    res_tenant_p = tenant_supabase.table("Prompts").select("Prompt_Text").limit(1).execute()
                    if res_tenant_p.data:
                        prompt_text = res_tenant_p.data[0].get("Prompt_Text", "")
                except Exception:
                    pass

            if not prompt_text:
                prompt_text = (
                    "Você é o Agente de Follow-Up (FUP) oficial. "
                    "Analise o histórico recente com o lead e decida qual o melhor próximo contato "
                    "para retomar a negociação de forma humana, consultiva e educada."
                )

            # Buscar dados do Lead
            res_lead = tenant_supabase.table("Leads_Mindflow").select("*").eq("Número", phone).limit(1).execute()
            lead_info = res_lead.data[0] if res_lead.data else {"Número": phone, "Nome": "Cliente"}

            # Buscar histórico recente de conversa
            history = []
            try:
                res_hist = tenant_supabase.table("n8n_chat_histories")\
                    .select("message, id")\
                    .eq("session_id", phone)\
                    .order("id", desc=True)\
                    .limit(10)\
                    .execute()
                for row in reversed(res_hist.data):
                    msg = row.get("message") or {}
                    msg_type = msg.get("type")
                    content = msg.get("content", "")
                    if isinstance(content, dict):
                        content = content.get("output") or content.get("text") or str(content)
                    role = "user" if msg_type == "human" else "assistant"
                    history.append({"role": role, "content": str(content)})
            except Exception:
                history = []

            return {
                "config": config,
                "prompt_text": prompt_text,
                "lead_info": lead_info,
                "history": history
            }

        prep_data = await run_step_with_retry(
            "fup_flow_fetch_prompt_and_config",
            execution_id,
            tenant_supabase,
            fetch_prompt_and_config,
            {"client_id": client_id, "phone": phone}
        )

        config = prep_data["config"]
        system_prompt = prep_data["prompt_text"]
        lead_info = prep_data["lead_info"]
        chat_history = prep_data["history"]

        # 1.1 Verificação de inatividade e ciclo de FUP:
        # Se o lead conversou nos últimos 10 minutos, OU se já atingiu o limite de FUPs, OU se houve FUP há menos de 30 min, aborta
        if not custom_context:
            async def check_inactivity():
                last_msg_date_str = lead_info.get("data ultima msgm")
                last_dt = None
                if last_msg_date_str:
                    try:
                        last_dt = datetime.fromisoformat(last_msg_date_str)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        diff_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
                        if diff_seconds < 600:
                            raise ValueError("LEAD_INTERACTED_RECENTLY")
                    except ValueError as ve:
                        if str(ve) == "LEAD_INTERACTED_RECENTLY":
                            raise ve
                    except Exception:
                        pass

                # Consulta histórico recente para verificar se já atingiu o limite ou está em cooldown
                try:
                    res_hist_fup = tenant_supabase.table("n8n_chat_histories")\
                        .select("id, created_at, message")\
                        .eq("session_id", phone)\
                        .order("created_at", desc=True)\
                        .limit(20)\
                        .execute()
                    
                    recent_msgs = res_hist_fup.data or []
                    fup_count = 0
                    last_fup_dt = None
                    for m in recent_msgs:
                        m_time_str = m.get("created_at")
                        if not m_time_str:
                            continue
                        m_dt = datetime.fromisoformat(m_time_str)
                        if m_dt.tzinfo is None:
                            m_dt = m_dt.replace(tzinfo=timezone.utc)
                        if last_dt and m_dt <= last_dt:
                            break
                        msg_p = m.get("message") or {}
                        c_str = msg_p.get("content", "")
                        add_k = msg_p.get("additional_kwargs") or {}
                        if add_k.get("fup_action") or (isinstance(c_str, str) and c_str.startswith("[Follow-up")):
                            fup_count += 1
                            if last_fup_dt is None:
                                last_fup_dt = m_dt

                    # Se a tentativa solicitada já foi executada no ciclo atual, aborta
                    for m in recent_msgs:
                        m_time_str = m.get("created_at")
                        if not m_time_str:
                            continue
                        m_dt = datetime.fromisoformat(m_time_str)
                        if m_dt.tzinfo is None:
                            m_dt = m_dt.replace(tzinfo=timezone.utc)
                        if last_dt and m_dt <= last_dt:
                            break
                        msg_p = m.get("message") or {}
                        add_k = msg_p.get("additional_kwargs") or {}
                        c_str = msg_p.get("content", "")
                        prev_att = add_k.get("attempt")
                        if prev_att is not None and int(prev_att) == int(attempt):
                            raise ValueError("ATTEMPT_ALREADY_EXECUTED")
                        elif f"Tentativa {attempt}" in str(c_str):
                            raise ValueError("ATTEMPT_ALREADY_EXECUTED")

                    # Se já enviou o máximo de FUPs configurado
                    max_fup = config.get("quantidade_fup_whats")
                    try:
                        max_fup = int(max_fup) if max_fup is not None else 3
                    except Exception:
                        max_fup = 3
                    if max_fup <= 0:
                        max_fup = 3

                    if fup_count >= max_fup:
                        raise ValueError("MAX_FUP_REACHED")

                    # Trava imediata de 3 minutos para evitar concorrência no mesmo instante
                    if last_fup_dt and (datetime.now(timezone.utc) - last_fup_dt).total_seconds() < 180:
                        raise ValueError("FUP_IMMEDIATE_COLLISION")

                except ValueError as ve:
                    raise ve
                except Exception as e_check:
                    print(f"[check_inactivity] Erro ao checar histórico: {e_check}")

                return {"status": "inactive_confirmed"}

            try:
                await run_step_with_retry(
                    "fup_flow_check_inactivity",
                    execution_id,
                    tenant_supabase,
                    check_inactivity,
                    {"last_msg_time": lead_info.get("data ultima msgm")}
                )
            except ValueError as err:
                err_str = str(err)
                if err_str in ("LEAD_INTERACTED_RECENTLY", "MAX_FUP_REACHED", "ATTEMPT_ALREADY_EXECUTED", "FUP_IMMEDIATE_COLLISION"):
                    outcome_map = {
                        "LEAD_INTERACTED_RECENTLY": "lead_interacted_recently_skipped",
                        "MAX_FUP_REACHED": "max_fup_reached_skipped",
                        "ATTEMPT_ALREADY_EXECUTED": f"attempt_{attempt}_already_executed_skipped",
                        "FUP_IMMEDIATE_COLLISION": "fup_immediate_collision_skipped"
                    }
                    tenant_supabase.table("workflow_executions").update({
                        "status": "SUCCESS",
                        "output_data": {"outcome": outcome_map[err_str]},
                        "completed_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", execution_id).execute()
                    return

        # Adicionar custom_context ao histórico se informado
        if custom_context:
            chat_history.append({"role": "system", "content": f"Contexto adicional: {custom_context}"})

        # 2 e 3. Processamento do agente LLM determinístico com 3 retentativas
        async def call_fup_llm():
            llm_model = config.get("fallback_llm_model") or "gpt-4o-mini"
            return await generate_fup_decision(
                openai_client,
                system_prompt=system_prompt,
                lead_info=lead_info,
                chat_history=chat_history,
                model=llm_model,
                temperature=0.2,
                max_retries=3,
                attempt=attempt
            )

        fup_decision = await run_step_with_retry(
            "fup_flow_llm_agent",
            execution_id,
            tenant_supabase,
            call_fup_llm,
            {"phone": phone, "model": config.get("fallback_llm_model") or "gpt-4o-mini", "attempt": attempt}
        )

        # 4. Leitura e Validação de Permissões na tabela client_configurations
        async def validate_permissions():
            acao = fup_decision.get("acao")
            is_fup_enabled = config.get("fup", True)  # Se True ou não configurado
            if is_fup_enabled is False:
                raise ValueError("FUP_DISABLED_FOR_CLIENT")

            # Validações por tipo de ação
            if acao == "audio" and config.get("can_send_fup_audio") is False:
                print(f"[FUP] Cliente {client_id} com can_send_fup_audio=False. Fallback para 'agendar_mensagem'.")
                fup_decision["acao"] = "agendar_mensagem"

            elif acao == "ligawhats" and not config.get("fup_ligawhats", False):
                print(f"[FUP] Cliente {client_id} não possui permissão para 'ligawhats'. Fallback para 'agendar_mensagem'.")
                fup_decision["acao"] = "agendar_mensagem"

            elif acao == "ligacao" and not config.get("fup_ligacao", False):
                print(f"[FUP] Cliente {client_id} não possui permissão para 'ligacao'. Fallback para 'agendar_mensagem'.")
                fup_decision["acao"] = "agendar_mensagem"

            return {
                "acao_aprovada": fup_decision["acao"],
                "quando_executar": fup_decision.get("quando_executar"),
                "conteudo": fup_decision.get("conteudo")
            }


        try:
            await run_step_with_retry(
                "fup_flow_validate_permissions",
                execution_id,
                tenant_supabase,
                validate_permissions,
                {"decision": fup_decision}
            )
        except ValueError as err:
            if str(err) == "FUP_DISABLED_FOR_CLIENT":
                tenant_supabase.table("workflow_executions").update({
                    "status": "SUCCESS",
                    "output_data": {"outcome": "fup_disabled_for_client"},
                    "completed_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", execution_id).execute()
                return

        # 5. Transformação de output em agendamento de ação no Redis ARQ (_defer_until)
        async def schedule_redis_action():
            if fup_decision.get("acao") in ("nenhuma", "congelar", "cancelar"):
                return {
                    "status": "completed",
                    "action": "nenhuma",
                    "reason": "cadence_exhausted_or_skipped"
                }

            quando_str = fup_decision.get("quando_executar")
            now_dt = datetime.now(timezone.utc)
            target_dt = None
            if quando_str:
                try:
                    # Converte string ISO para datetime com fuso
                    target_dt = datetime.fromisoformat(quando_str)
                    if target_dt.tzinfo is None:
                        # Se não tiver timezone, assume UTC
                        target_dt = target_dt.replace(tzinfo=timezone.utc)
                except Exception as parse_err:
                    print(f"Erro ao converter data de agendamento: {parse_err}. Executará imediatamente.")
                    target_dt = now_dt
            else:
                target_dt = now_dt

            # Se a data calculada for no passado (ex: LLM gerou data antiga), ajusta para agora
            if target_dt < now_dt:
                print(f"[schedule_redis_action] Data {target_dt} está no passado em relação a {now_dt}. Ajustando para agora.")
                target_dt = now_dt

            # Sanitização de Fim de Semana (Regra fup_fds do tenant):
            # Se fup_fds for False e a data agendada cair no Sábado (5) ou Domingo (6),
            # ajusta automaticamente para a próxima Segunda-feira às 09:30 da manhã (horário comercial).
            if not config.get("fup_fds", False):
                # Converte target_dt para o fuso local de São Paulo para checar o dia da semana real
                tz_br = timezone(timedelta(hours=-3))
                target_br = target_dt.astimezone(tz_br)
                if target_br.weekday() in (5, 6):  # 5 = Sábado, 6 = Domingo
                    days_to_monday = 7 - target_br.weekday()
                    target_br_adjusted = target_br.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(days=days_to_monday)
                    target_dt = target_br_adjusted.astimezone(timezone.utc)
                    print(f"[schedule_redis_action] Data caía no fim de semana ({target_br}). fup_fds=False -> Ajustado para Segunda-feira: {target_dt} (BR: {target_br_adjusted}).")

            clean_phone = "".join([c for c in phone if c.isdigit()])
            action_job_id = f"fup_action_{client_id}_{clean_phone}"

            # Remove qualquer ação de FUP anterior ainda pendente no Redis para este lead
            await redis_pool.zrem("arq:queue", action_job_id)
            await redis_pool.delete(f"arq:job:{action_job_id}")

            # Enfileirar tarefa no ARQ com _defer_until e _job_id determinístico
            job = await redis_pool.enqueue_job(
                "execute_scheduled_fup_action",
                client_id,
                phone,
                fup_decision["acao"],
                fup_decision.get("conteudo") or "",
                execution_id,
                attempt,
                _job_id=action_job_id,
                _defer_until=target_dt
            )
            job_id = action_job_id
            await redis_pool.set(f"fup_active_job:{client_id}:{clean_phone}", action_job_id, ex=86400)
            return {
                "status": "scheduled",
                "job_id": job_id,
                "scheduled_for": target_dt.isoformat(),
                "action": fup_decision["acao"],
                "attempt": attempt
            }

        schedule_res = await run_step_with_retry(
            "fup_flow_schedule_action",
            execution_id,
            tenant_supabase,
            schedule_redis_action,
            {"target_time": fup_decision.get("quando_executar")}
        )

        # Finalizar master como SUCCESS
        tenant_supabase.table("workflow_executions").update({
            "status": "SUCCESS",
            "output_data": _clean_null_bytes({
                "decision": fup_decision,
                "schedule": schedule_res
            }),
            "completed_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", execution_id).execute()

    except Exception as e:
        safe_err = _clean_null_bytes(str(e))
        tenant_supabase.table("workflow_executions").update({
            "status": "FAILED",
            "error_details": safe_err,
            "completed_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", execution_id).execute()
        raise e


# Startup / Shutdown Hooks
async def startup(ctx):
    openai_key = os.getenv("OPENAI_API_KEY") or os.getenv("Openai_api_key")
    if not openai_key:
        raise ValueError("OPENAI_API_KEY (or Openai_api_key) must be set in the environment")
    ctx["openai"] = AsyncOpenAI(api_key=openai_key)

    # Injetar pool do Redis no contexto para agendamento de jobs futuros
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    from arq import create_pool
    ctx["redis"] = await create_pool(RedisSettings.from_dsn(redis_url))

async def shutdown(ctx):
    if "openai" in ctx:
        await ctx["openai"].close()
    if "redis" in ctx and ctx["redis"]:
        await ctx["redis"].close()

# ARQ Worker Settings
class WorkerSettings:
    functions = [
        process_whatsapp_response,
        process_fup_request,
        execute_scheduled_fup_action
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://localhost:6379"))

