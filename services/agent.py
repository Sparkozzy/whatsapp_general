import json
import base64
import io
import httpx
from typing import List, Dict, Any, Optional
from pypdf import PdfReader
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession



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

async def generate_tts_audio(openai_client, text: str, voice: str = "nova") -> str:
    """
    Generates speech audio from text using OpenAI TTS, returning base64 encoding.
    """
    response = await openai_client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text
    )
    # Read binary bytes
    audio_bytes = response.content
    return base64.b64encode(audio_bytes).decode("utf-8")

async def analyze_image(openai_client, image_url: str, caption: Optional[str] = None) -> str:
    """
    Analyzes/describes an image using GPT-4o-mini Vision API.
    Returns a text description (including OCR and visual analysis) to be buffered.
    """
    system_message = (
        "Você é um assistente de IA especializado em visão computacional e transcrição de imagens (OCR).\n"
        "Sua tarefa é analisar a imagem fornecida e descrever seu conteúdo de forma clara, precisa e objetiva.\n"
        "Regras:\n"
        "- Identifique e descreva elementos chave: textos (faça a leitura/OCR completa de documentos ou telas), "
        "comprovantes de pagamento, valores, datas, nomes, produtos, tabelas ou objetos visuais relevantes.\n"
        "- Responda apenas com a descrição objetiva do conteúdo da imagem em português.\n"
        "- Seja direto e conciso, focando no que é importante para o atendimento de um agente comercial/jurídico.\n"
    )

    messages = [
        {"role": "system", "content": system_message},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Analise a imagem a seguir e descreva detalhadamente todo o seu conteúdo (textos/OCR, dados, comprovantes, objetos):"},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
        }
    ]

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=600,
            temperature=0.2
        )
        description = response.choices[0].message.content or "Imagem recebida (sem descrição legível disponível)."
    except Exception as e:
        print(f"Failed to analyze image with vision model: {e}")
        description = "Imagem enviada pelo usuário (falha ao extrair detalhes visuais)."

    if caption and caption.strip():
        return f"[Imagem: {description} | Legenda enviada com a foto: {caption.strip()}]"
    else:
        return f"[Imagem: {description}]"

async def generate_llm_response_with_mcp(
    openai_client,
    system_prompt: str,
    chat_history: List[Dict[str, str]],
    user_message: str,
    mcp_url: str,
    mcp_api_key: str,
    model: str = "gpt-4o",
    temperature: float = 0.8,
) -> Dict[str, Any]:
    """
    Generates a response from OpenAI with MCP Tool Calling support.
    Connects to the given MCP SSE URL, fetches tools, and loops until the agent finishes.
    """
    json_instructions = (
        "\n\nQuando você terminar de usar ferramentas ou quiser enviar uma mensagem final ao usuário, "
        "você deve obrigatoriamente responder em formato JSON válido contendo exatamente as seguintes chaves:\n"
        "{\n"
        '  "type": "texto" ou "audio",\n'
        '  "output": "A mensagem de resposta a ser enviada ao usuário"\n'
        "}"
    )
    
    messages = [{"role": "system", "content": system_prompt + json_instructions}]
    
    for msg in chat_history[-10:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
        
    messages.append({"role": "user", "content": user_message})

    # Prepare SSE headers
    headers = {}
    if mcp_api_key:
        headers["Authorization"] = f"Bearer {mcp_api_key}"

    try:
        async with sse_client(mcp_url, headers=headers) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                
                # Convert MCP tools to OpenAI format
                openai_tools = []
                for tool in tools_result.tools:
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema
                        }
                    })

                while True:
                    kwargs = {
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "response_format": {"type": "json_object"}
                    }
                    if openai_tools:
                        kwargs["tools"] = openai_tools
                        kwargs["tool_choice"] = "auto"
                        # OpenAI might complain if response_format is JSON and tools are used in some old API versions,
                        # but GPT-4o supports JSON mode with tool calling.

                    response = await openai_client.chat.completions.create(**kwargs)
                    msg = response.choices[0].message

                    if msg.tool_calls:
                        # Append the assistant's tool call message
                        messages.append(msg.model_dump(exclude_none=True))
                        
                        for tool_call in msg.tool_calls:
                            try:
                                args = json.loads(tool_call.function.arguments)
                                print(f"Calling MCP tool {tool_call.function.name} with args: {args}")
                                result = await session.call_tool(tool_call.function.name, arguments=args)
                                # Result content is usually a list of TextContent objects
                                result_text = "\n".join([c.text for c in result.content if c.type == "text"])
                            except Exception as tool_err:
                                result_text = f"Error calling tool: {tool_err}"
                                print(result_text)

                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result_text
                            })
                    else:
                        # Finished using tools, returned the final response
                        content = msg.content
                        try:
                            return json.loads(content)
                        except json.JSONDecodeError:
                            return {
                                "type": "texto",
                                "output": content or ""
                            }
    except Exception as e:
        print(f"Error in MCP loop: {e}. Falling back to normal response.")
        return await generate_llm_response(
            openai_client, system_prompt, chat_history, user_message, model=model, temperature=temperature
        )


async def summarize_pdf_first_page(
    openai_client,
    file_url: str,
    file_name: Optional[str] = None,
    caption: Optional[str] = None
) -> str:
    """
    Downloads PDF, extracts text from page 1 using pypdf, and uses gpt-4o-mini to generate
    document classification and brief summary for the main agent.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(file_url, follow_redirects=True, timeout=15.0)
            response.raise_for_status()
            pdf_bytes = response.content

        reader = PdfReader(io.BytesIO(pdf_bytes))
        if len(reader.pages) == 0:
            return f"[Documento PDF recebido: {file_name or 'arquivo.pdf'} | PDF sem páginas legíveis]"

        # Extract text ONLY from page 1 (index 0)
        first_page_text = (reader.pages[0].extract_text() or "").strip()

        if not first_page_text:
            return (
                f"[Documento PDF recebido: {file_name or 'arquivo.pdf'} | "
                f"Primeira página vazia ou sem camada de texto legível (imagem escaneada)]"
            )

        # Truncate text if excessively long (max 3000 chars for page 1)
        truncated_text = first_page_text[:3000]

        system_prompt = (
            "Você é um assistente encarregado de analisar a primeira página de um documento em PDF recebido via WhatsApp.\n"
            "Sua tarefa é:\n"
            "1. Identificar o tipo do documento (ex: CNH, RG, Fatura, Contrato, Comprovante de Residência, Holerite, etc.).\n"
            "2. Fazer um resumo curto, preciso e objetivo das informações e dados principais presentes APENAS nesta primeira página (ex: nomes, datas, valores, assunto).\n\n"
            "Formato de resposta desejado:\n"
            "Tipo: <TIPO DO DOCUMENTO>\n"
            "Resumo: <RESUMO CONCISO DA PÁGINA 1>"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Texto extraído da primeira página do PDF ({file_name or 'documento.pdf'}):\n\n{truncated_text}"}
        ]

        if openai_client:
            llm_res = await openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.2,
                max_tokens=300
            )
            summary_result = llm_res.choices[0].message.content or "Não foi possível resumir a página."
        else:
            summary_result = f"Texto da 1ª página: {truncated_text[:200]}..."

        output_parts = [
            f"[Documento PDF Recebido (Primeira Página Extraída) | Arquivo: {file_name or 'documento.pdf'}]",
            summary_result
        ]
        if caption and caption.strip():
            output_parts.append(f"Legenda enviada pelo usuário com o PDF: {caption.strip()}")

        return "\n\n".join(output_parts)

    except Exception as e:
        print(f"Error summarizing PDF first page: {e}")
        fallback_parts = [f"[Documento PDF recebido | Arquivo: {file_name or 'documento.pdf'}]"]
        if caption and caption.strip():
            fallback_parts.append(f"Legenda enviada: {caption.strip()}")
        return " | ".join(fallback_parts)

