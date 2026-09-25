# Documentação de Workflow — Agente de FUP (`fup_flow`)

## 1. Alvo (Objetivo)

Automatizar e orquestrar as decisões de Follow-up (FUP) para leads em atendimento no WhatsApp, utilizando um agente LLM determinístico (mini/micro GPT) com saída estrita em JSON. O fluxo avalia o histórico do lead, o prompt configurado para o cliente no Supabase, valida as permissões de uso cadastradas na tabela `client_configurations` do Supabase Master e converte a ação recomendada em um **agendamento persistente no ARQ Redis (`_defer_until`)**, garantindo rastreabilidade total (EDW) em `workflow_executions` e `workflow_step_executions`.

---

## 2. Ações Possíveis de Saída (JSON do Agente FUP)

O agente de IA retorna um JSON estruturado obrigatoriamente em uma das 4 ações homologadas:
1. `agendar_mensagem`: Agendamento de mensagem de texto conversacional para reengajamento do lead.
2. `figurinha`: Envio de figurinha (sticker) de follow-up/reação.
3. `ligawhats`: Disparo de chamada de voz/áudio via WhatsApp.
4. `ligacao`: Disparo de ligação telefônica convencional (integração Retell AI / `pre_call_processing`).

### Formato do JSON do Agente (Contrato Estrito)

```json
{
  "acao": "agendar_mensagem",
  "quando_executar": "2026-09-25T15:30:00-03:00",
  "conteudo": "Olá {{ customer_name }}, conseguiu avaliar a proposta que combinamos?",
  "justificativa": "Lead parou de responder há mais de 4 horas em horário comercial."
}
```

---

## 3. Passos do Workflow (workflow_steps)

Os passos seguem a convenção de nomenclatura `{workflow_name}_{OQF}` e são executados com `run_step_with_retry` (com até 3 retentativas e backoff exponencial):

### 1. `fup_flow_fetch_prompt_and_config`
- **Descrição**: Leitura das configurações do cliente e do template de prompt de FUP no Supabase.
- **Lógica**:
  - Lê a linha do cliente em `client_configurations` no Supabase Master buscando `prompt_id`, flags de permissão (`fup`, `fup_ligawhats`, `fup_ligacao`, `quantidade_fup_whats`, `fup_fds`) e credenciais do banco isolado do cliente.
  - Com o `prompt_id`, busca o `Prompt_Text` na tabela `Prompts` (Supabase Master / Cliente).
  - Recupera os dados cadastrais do lead na tabela `Leads_Mindflow` e o histórico recente em `n8n_chat_histories`.

### 2. `fup_flow_llm_agent`
- **Descrição**: Processamento da IA determinística mini/micro (`gpt-4o-mini` com temperature baixa ou `gpt-4.1-mini`).
- **Lógica**:
  - Injeta o prompt mestre do cliente, histórico e variáveis (`customer_name`, `now`, etc.).
  - Solicita explicitamente o JSON estruturado (`response_format={"type": "json_object"}`).
  - Conta com mecanismo de **três retentativas automáticas** caso a resposta não seja um JSON válido ou viole os campos obrigatórios (`acao`, `quando_executar`, `conteudo`).

### 3. `fup_flow_parse_and_validate_output`
- **Descrição**: Leitura, deserialização e validação sintática do JSON retornado pelo LLM via Pydantic (`FupAgentDecision`).
- **Lógica**:
  - Valida se `acao` pertence ao conjunto `{"agendar_mensagem", "figurinha", "ligawhats", "ligacao"}`.
  - Valida se `quando_executar` contém timezone offset (obrigatório pelas convenções).

### 4. `fup_flow_validate_permissions`
- **Descrição**: Validação das regras de negócio e permissões de uso do cliente em `client_configurations`.
- **Lógica**:
  - Se `fup == False`: Aborta o agendamento com status `SKIPPED` / `fup_disabled_for_client`.
  - Se `acao == "ligawhats"` e `fup_ligawhats == False`: Bloqueia a ação e efetua fallback para `agendar_mensagem` ou aborta.
  - Se `acao == "ligacao"` e `fup_ligacao == False`: Bloqueia a ação ou faz fallback.
  - Valida se o dia agendado é final de semana e `fup_fds == False`.
  - Valida limite de tentativas (`quantidade_fup_whats`) contra histórico de FUPs já executados para o lead.

### 5. `fup_flow_schedule_action`
- **Descrição**: Transformação da decisão em agendamento persistente no Redis via ARQ (`_defer_until`).
- **Lógica**:
  - Converte o horário `quando_executar` para datetime UTC.
  - Enfileira a task de execução no ARQ (`enqueue_job("execute_scheduled_fup_action", ..., _defer_until=exec_time_utc)`).
  - Registra a confirmação com o job ID do Redis em `workflow_step_executions`.
