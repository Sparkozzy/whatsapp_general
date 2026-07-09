# Documentação de Workflow - whatsapp_flow

## Alvo (Objetivo)

Migrar o fluxo de WhatsApp atualmente hospedado no n8n para um serviço em Python FastAPI assíncrono e resiliente. O fluxo deve ser multi-tenant (compatível com `client_configurations`), permitindo que a mesma instância de API sirva a múltiplos clientes de forma isolada, processando mensagens recebidas, gerenciando blacklists, CRM de leads, buffers de mensagens (debouncing), geração de respostas via IA (texto ou áudio/TTS) e disparando respostas pelo Z-API.

---

## Passos do Workflow (workflow_steps)

Os seguintes passos (nós) formam o workflow `whatsapp_flow`:

### 1. `whatsapp_flow_received_message`
- **Descrição**: Recepção e validação do payload do webhook vindo do Z-API ou CRM.
- **Lógica**: Identifica o `client_id` na URL e a origem da requisição pelo endpoint acessado:
  - `/webhook/whatsapp/zapi/{client_id}` para Z-API.
  - `/webhook/whatsapp/crm/{client_id}` para o CRM.
  Valida o token enviado no header `X-MindFlow-Token` (ou `Authorization`) comparando com o campo `mindflow_api_token` na tabela `client_configurations` do Supabase Master. Se o token não for ativo/válido para o `client_id`, rejeita com `401 Unauthorized`.
  Em seguida, verifica se o tipo de evento é `MESSAGE_RECEIVED` (ignora os demais), sanitiza o número de telefone (`+55DDXXXXXXXXX`) e cria a execução mestre (`workflow_executions`) com status `PENDING` no banco do cliente.

### 2. `whatsapp_flow_blacklist_check`
- **Descrição**: Consulta a tabela `Blacklist_Mindflow` para verificar se o remetente está bloqueado.
- **Lógica**: Se o número constar na blacklist, o workflow registra o encerramento com sucesso (com resultado de negócio correspondente) e interrompe a execução.

### 3. `whatsapp_flow_lead_check`
- **Descrição**: Procura o remetente na tabela `Leads_Mindflow`.
- **Lógica**: Retorna os dados do lead caso ele já exista na base de dados.

### 4. `whatsapp_flow_lead_create`
- **Descrição**: Criação de lead na tabela `Leads_Mindflow` caso não exista.
- **Lógica**: Insere uma nova linha com o número de telefone do lead.

### 5. `whatsapp_flow_update_last_msg_time`
- **Descrição**: Atualização da data e hora da última mensagem do lead.
- **Lógica**: Atualiza a coluna `data ultima msgm` na tabela `Leads_Mindflow` para a data atual em UTC.

### 6. `whatsapp_flow_process_media`
- **Descrição**: Processamento e classificação do conteúdo da mensagem recebida.
- **Lógica**: Classifica o tipo da mensagem (TEXTO, ÁUDIO, IMAGEM, PDF, STICKER, VÍDEO). Em caso de áudio, chama a API da OpenAI (Whisper) para transcrever o áudio em texto público. Retorna o texto extraído/processado. Se for outro tipo de mídia (imagem, pdf, sticker, vídeo), retorna erro nesta etapa inicial.

### 7. `whatsapp_flow_buffer_message`
- **Descrição**: Persistência da mensagem na lista temporária no Redis para de-bouncing.
- **Lógica**: Adiciona a mensagem recebida ao final da lista `whatsapp_buffer:{client_id}:{phone_number}` no Redis e atualiza o TTL para 3600 segundos.

### 8. `whatsapp_flow_schedule_process`
- **Descrição**: Gerenciamento do de-bouncing utilizando snapshots.
- **Lógica**: Captura o snapshot inicial (Pré-Espera) lendo todas as mensagens atualmente na lista do Redis. Aguarda 20 segundos assíncronos (`asyncio.sleep`).

### 9. `whatsapp_flow_process_buffer`
- **Descrição**: Comparação do de-bouncing e envio para o Worker.
- **Lógica**: Após 20 segundos, captura o snapshot final (Pós-Espera). Compara se `pre_messages == post_messages`.
  - Se iguais, limpa o buffer deletando a chave no Redis, agrupa as mensagens em um único texto consolidado com quebra de linha `\n` e enfileira a tarefa assíncrona no ARQ Worker respondendo `202 Accepted`.
  - Se diferentes, interrompe silenciosamente a execução atual (pois uma nova execução posterior processará o novo lote de mensagens).

### 10. `whatsapp_flow_fetch_prompt`
- **Descrição**: Busca as diretrizes/prompts de sistema para o agente.
- **Lógica**: Lê o `prompt_id` associado ao cliente em `client_configurations` (Supabase Master) e faz a consulta ao banco do cliente na tabela `Prompts` pelo ID correspondente.

### 11. `whatsapp_flow_llm_response`
- **Descrição**: Geração da resposta contextualizada utilizando LLM (GPT-4o-mini / GPT-4).
- **Lógica**: Envia o histórico de conversa do usuário (salvo em `n8n_chat_histories` ou `postgres_chat_memory`), as informações do lead e o prompt do agente para a OpenAI. Retorna um JSON estruturado com os campos `type` ("texto" ou "audio") e `output` (a mensagem de resposta).

### 12. `whatsapp_flow_format_text_response`
- **Descrição**: Divisão de mensagens de texto longas para humanização (se `type == "texto"`).
- **Lógica**: Chama um LLM auxiliar para formatar a resposta em uma lista de mensagens naturais mais curtas (máximo 240 caracteres por linha, respeitando regras de quebra e negrito).

### 13. `whatsapp_flow_send_messages`
- **Descrição**: Envio das mensagens formatadas via Z-API.
- **Lógica**: Itera a lista de mensagens de texto e envia ao Z-API utilizando `httpx.AsyncClient()` com espaçamento (delay de digitação de 1.5s).

### 14. `whatsapp_flow_tts_generation`
- **Descrição**: Geração de arquivo de áudio falado utilizando OpenAI TTS (se `type == "audio"`).
- **Lógica**: Envia o texto de resposta para a API de Text-to-Speech (`tts-1-hd`, voz `nova`) e obtém o arquivo binário correspondente.

### 15. `whatsapp_flow_send_audio`
- **Descrição**: Envio do áudio convertido via Z-API.
- **Lógica**: Converte o áudio binário para base64 e envia ao endpoint `send-audio` da instância do Z-API do cliente.
