import httpx
import io
from typing import Dict, Any

async def send_text(client_config: Dict[str, Any], to_phone: str, message: str) -> Dict[str, Any]:
    """
    Sends a text message using Z-API.
    """
    instance_id = client_config.get("zapi_instance_id")
    client_token = client_config.get("zapi_client_token")
    security_token = client_config.get("zapi_security_token")

    if not instance_id or not client_token:
        raise ValueError("Missing Z-API instance_id or client_token in configuration.")

    url = f"https://api.z-api.io/instances/{instance_id}/token/{client_token}/send-text"
    headers = {}
    if security_token:
        headers["Client-Token"] = security_token

    payload = {
        "phone": to_phone,
        "message": message,
        "delayTyping": 3
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

async def send_audio(client_config: Dict[str, Any], to_phone: str, audio_base64: str) -> Dict[str, Any]:
    """
    Sends a base64 encoded audio message using Z-API.
    """
    instance_id = client_config.get("zapi_instance_id")
    client_token = client_config.get("zapi_client_token")
    security_token = client_config.get("zapi_security_token")

    if not instance_id or not client_token:
        raise ValueError("Missing Z-API instance_id or client_token in configuration.")

    url = f"https://api.z-api.io/instances/{instance_id}/token/{client_token}/send-audio"
    headers = {}
    if security_token:
        headers["Client-Token"] = security_token

    payload = {
        "phone": to_phone,
        "audio": f"data:audio/mpeg;base64,{audio_base64}",
        "delayTyping": 10,
        "viewOnce": False,
        "waveform": True
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

async def transcribe_audio(audio_url: str, openai_client) -> str:
    """
    Downloads audio file from URL and transcribes it using OpenAI Whisper.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(audio_url)
        response.raise_for_status()
        audio_bytes = response.content

    # Load into memory file
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "audio.mp3"

    # Transcribe via OpenAI Async API (Note: OpenAI client uses async if initialized properly or wrapped)
    # We call it synchronously if client is synchronous, or asynchronously if it's the async client.
    # In worker.py, we will define an AsyncOpenAI client.
    transcription = await openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
        language="pt"
    )
    return transcription.text
