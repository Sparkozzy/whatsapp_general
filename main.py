import os
import asyncio
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Header, status
from arq import create_pool
from arq.connections import RedisSettings
import redis.asyncio as aioredis

from database import ClientDatabaseManager
from schemas import ZApiWebhookPayload, CrmWebhookPayload, NormalizedMessage
from services.whatsapp import transcribe_audio

# Global Redis clients
redis_client = None
arq_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, arq_pool
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = aioredis.from_url(redis_url)
    arq_pool = await create_pool(RedisSettings.from_dsn(redis_url))
    yield
    if redis_client:
        await redis_client.close()
    if arq_pool:
        await arq_pool.close()

app = FastAPI(title="MindFlow WhatsApp Multi-tenant API", lifespan=lifespan)

# Token security dependency
async def verify_mindflow_token(
    client_id: str,
    x_mindflow_token: Optional[str] = Header(None, alias="X-MindFlow-Token"),
    authorization: Optional[str] = Header(None)
):
    # Try to extract bearer token if X-MindFlow-Token header is missing
    token = x_mindflow_token
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MindFlow security token is missing."
        )

    try:
        config = ClientDatabaseManager.get_client_config(client_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )

    expected_token = config.get("mindflow_api_token")
    if not expected_token or token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid security token."
        )

def sanitize_phone(phone: str) -> str:
    """
    Sanitizes phone number to E.164 format (+55DDXXXXXXXXX).
    """
    # Remove non-digits
    digits = "".join([c for c in phone if c.isdigit()])
    if not digits.startswith("55"):
        digits = "55" + digits
    return f"+{digits}"

async def handle_normalized_message(msg: NormalizedMessage):
    """
    Implements Redis Inbound Debounce and triggers background task.
    """
    global redis_client, arq_pool
    key = f"whatsapp_buffer:{msg.client_id}:{msg.phone}"
    
    # 1. Add message text to Redis list
    await redis_client.rpush(key, msg.text)
    await redis_client.expire(key, 3600)  # 1 hour TTL
    
    # 2. Get initial snapshot (Pre-Wait)
    pre_messages = await redis_client.lrange(key, 0, -1)
    
    # 3. Wait 20 seconds assynchronously (Debounce)
    await asyncio.sleep(20)
    
    # 4. Get final snapshot (Post-Wait)
    post_messages = await redis_client.lrange(key, 0, -1)
    
    # 5. Check if this is the winning execution thread
    if pre_messages == post_messages:
        # Clear buffer
        await redis_client.delete(key)
        
        # Aggregate messages
        aggregated_text = "\n".join([m.decode("utf-8") if isinstance(m, bytes) else m for m in post_messages])
        
        # Get tenant Supabase client
        tenant_supabase = ClientDatabaseManager.get_client(msg.client_id)
        
        # Create master record in workflow_executions
        # Storing the original incoming payload in input_data for error logging as requested
        res = tenant_supabase.table("workflow_executions").insert({
            "workflow_name": "whatsapp_flow",
            "status": "PENDING",
            "input_data": msg.raw_payload
        }).execute()
        
        execution_id = res.data[0]["id"]
        
        # Enqueue arq worker job
        await arq_pool.enqueue_job(
            "process_whatsapp_response",
            msg.client_id,
            msg.phone,
            aggregated_text,
            execution_id
        )
        return {
            "status": "accepted",
            "message": "Message buffered and execution scheduled.",
            "execution_id": execution_id
        }
    else:
        return {
            "status": "discarded",
            "message": "New messages arrived. Thread discarded."
        }

@app.post("/webhook/whatsapp/zapi/{client_id}")
async def zapi_webhook(
    client_id: str,
    payload: ZApiWebhookPayload,
    _ = Depends(verify_mindflow_token)
):
    if payload.eventType != "MESSAGE_RECEIVED":
        return {"status": "ignored", "reason": "Not a MESSAGE_RECEIVED event."}

    content_type = payload.content.type.upper()
    phone = sanitize_phone(payload.content.details.sender_from)
    
    # Extract message or media details
    text = ""
    audio_url = None
    if content_type == "TEXT":
        text = payload.content.text or ""
    elif content_type == "AUDIO":
        # Extract audio URL
        if payload.content.details.file:
            audio_url = payload.content.details.file.publicUrl
            # We don't transcribe here as it's blocking; we store the URL or pass it
            text = "[Mensagem de Áudio]"
    else:
        # Ignore or error other medias
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported media type: {content_type}"
        )

    msg = NormalizedMessage(
        client_id=client_id,
        phone=phone,
        text=text,
        type="audio" if content_type == "AUDIO" else "texto",
        audio_url=audio_url,
        raw_payload=payload.model_dump()
    )
    
    res = await handle_normalized_message(msg)
    return res

@app.post("/webhook/whatsapp/crm/{client_id}")
async def crm_webhook(
    client_id: str,
    payload: CrmWebhookPayload,
    _ = Depends(verify_mindflow_token)
):
    content_type = payload.type.upper()
    phone = sanitize_phone(payload.phone)

    text = ""
    audio_url = None
    if content_type == "TEXT":
        text = payload.message or ""
    elif content_type == "AUDIO":
        audio_url = payload.audio_url
        text = "[Mensagem de Áudio]"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported media type: {content_type}"
        )

    msg = NormalizedMessage(
        client_id=client_id,
        phone=phone,
        text=text,
        type="audio" if content_type == "AUDIO" else "texto",
        audio_url=audio_url,
        raw_payload=payload.model_dump()
    )

    res = await handle_normalized_message(msg)
    return res
