import json
import base64
from typing import List, Dict, Any

async def generate_llm_response(
    openai_client,
    system_prompt: str,
    chat_history: List[Dict[str, str]],
    user_message: str,
    model: str = "gpt-4",
    temperature: float = 0.8,
    fallback_model: str = "gpt-4o-mini",
    fallback_temperature: float = 0.7
) -> Dict[str, Any]:
    """
    Generates structured response from OpenAI (type: text/audio, output: message content).
    Attempts primary model and falls back to fallback_model if it fails.
    """
    # Append structured output instructions to prompt
    json_instructions = (
        "\n\nVocê deve obrigatoriamente responder em formato JSON válido contendo exatamente as seguintes chaves:\n"
        "{\n"
        '  "type": "texto" ou "audio",\n'
        '  "output": "A mensagem de resposta a ser enviada ao usuário"\n'
        "}"
    )
    
    messages = [
        {"role": "system", "content": system_prompt + json_instructions}
    ]
    
    # Append conversation history (limited to last 10 turns)
    for msg in chat_history[-10:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
        
    messages.append({"role": "user", "content": user_message})

    try:
        response = await openai_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"}
        )
    except Exception as primary_error:
        # Fallback to fallback_model
        print(f"Primary model {model} failed: {primary_error}. Falling back to {fallback_model}.")
        response = await openai_client.chat.completions.create(
            model=fallback_model,
            messages=messages,
            temperature=fallback_temperature,
            response_format={"type": "json_object"}
        )
    
    try:
        content = response.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        # Fallback in case of parse error
        return {
            "type": "texto",
            "output": response.choices[0].message.content or ""
        }

async def format_text_response(openai_client, text_to_format: str) -> List[str]:
    """
    Uses OpenAI to format/split a response text into natural, humanized chunks (max 240 chars each).
    """
    system_message = (
        "Você é um assistente especializado em formatação de mensagens para WhatsApp.\n"
        "Sua tarefa é receber uma mensagem do usuário e dividi-la em mensagens curtas, naturais e humanizadas.\n"
        "Regras:\n"
        "- Divida as mensagens em parágrafos que façam sentido de forma independente.\n"
        "- Cada parte não deve ser excessivamente longa (de preferência menor que 240 caracteres).\n"
        "- Não gere mensagens vazias.\n"
        "- Adicione quebras de linhas (\\n\\n) após pontos finais dentro de cada bloco.\n"
        "- Use apenas um asterisco '*' para negrito (exemplo: *negrito*), nunca dois.\n"
        "Responda apenas em formato JSON com o seguinte schema:\n"
        "{\n"
        '  "mensagens": ["Mensagem 1", "Mensagem 2", "Mensagem 3"]\n'
        "}"
    )

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": f"Formate a mensagem:\n{text_to_format}"}
        ],
        temperature=0.3,
        response_format={"type": "json_object"}
    )

    try:
        content = response.choices[0].message.content
        data = json.loads(content)
        return data.get("mensagens", [text_to_format])
    except Exception:
        # Fallback to single text if JSON fails
        return [text_to_format]

async def generate_tts_audio(openai_client, text: str) -> str:
    """
    Generates speech audio from text using OpenAI TTS, returning base64 encoding.
    """
    response = await openai_client.audio.speech.create(
        model="tts-1-hd",
        voice="nova",
        input=text
    )
    # Read binary bytes
    audio_bytes = response.content
    return base64.b64encode(audio_bytes).decode("utf-8")
