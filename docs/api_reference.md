# API Reference

Esta documentação descreve os endpoints expostos pelo serviço **MindFlow WhatsApp Multi-tenant API** para recepção de webhooks de mensagens e integração com fluxos de execução.

## Informações Gerais
* **Base URL**: `https://whatsapp-general-github.bkpxmb.easypanel.host`
* **Autenticação**: Requerida para todos os endpoints de webhook. Pode ser fornecida de três formas:
  * Via Header personalizado: `X-MindFlow-Token: <token>`
  * Via Header de autorização padrão: `Authorization: Bearer <token>`
  * Via Query Parameter na URL: `?token=<token>` (Útil para CRMs que não suportam configuração de headers customizados)
  
> [!NOTE]
> O `<token>` é validado dinamicamente no banco de dados Master a partir da tabela `client_configurations` usando o campo `mindflow_api_token` para o `client_id` fornecido na rota.

---

## Endpoints

### 1. Webhook Z-API
Recebe eventos de mensagens recebidas diretamente da API da Z-API.

* **Método**: `POST`
* **Rota**: `/webhook/whatsapp/zapi/{client_id}`
* **Parâmetros de Rota**:
  * `client_id` (string): Identificador único do cliente tenant.

#### Headers Exemplo
```http
X-MindFlow-Token: seu_token_aqui
Content-Type: application/json
```

#### Corpo da Requisição (Payload JSON)
A API aceita mensagens do tipo `TEXT` e `AUDIO`. Outros formatos retornarão erro `400 Bad Request`.

##### Exemplo de Mensagem de Texto:
```json
{
  "instanceId": "3B90A...",
  "eventType": "MESSAGE_RECEIVED",
  "content": {
    "type": "TEXT",
    "text": "Olá, gostaria de saber mais sobre os planos.",
    "details": {
      "from": "5548996027108"
    }
  }
}
```

##### Exemplo de Mensagem de Áudio:
```json
{
  "instanceId": "3B90A...",
  "eventType": "MESSAGE_RECEIVED",
  "content": {
    "type": "AUDIO",
    "text": null,
    "details": {
      "from": "5548996027108",
      "file": {
        "publicUrl": "https://z-api.io/media/audio.mp3"
      }
    }
  }
}
```

#### Respostas
* **`200 OK` (Mensagem Enfileirada)**: Ocorre quando a mensagem foi adicionada com sucesso ao buffer do Redis para debounce.
  ```json
  {
    "status": "accepted",
    "message": "Message buffered and execution scheduled.",
    "execution_id": "c7a8b9c0-..."
  }
  ```
* **`200 OK` (Thread Descartada)**: Ocorre se novas mensagens do mesmo remetente chegarem durante a janela de debounce de 20 segundos.
  ```json
  {
    "status": "discarded",
    "message": "New messages arrived. Thread discarded."
  }
  ```
* **`401 Unauthorized`**: Token de autenticação ausente ou inválido.
* **`404 Not Found`**: Tenant `client_id` não encontrado nas configurações do banco Master.

---

### 2. Webhook CRM
Recebe payloads de mensagens vindas de integrações customizadas com CRM.

* **Método**: `POST`
* **Rota**: `/webhook/whatsapp/crm/{client_id}`
* **Parâmetros de Rota**:
  * `client_id` (string): Identificador único do cliente tenant.

#### Headers Exemplo
```http
Authorization: Bearer seu_token_aqui
Content-Type: application/json
```

#### Corpo da Requisição (Payload JSON)

##### Exemplo de Mensagem de Texto (CRM):
```json
{
  "type": "TEXT",
  "text": "Oi",
  "direction": "FROM_HUB",
  "details": {
    "to": "+5551996506656",
    "from": "+5548996027108"
  }
}
```

##### Exemplo de Mensagem de Áudio (CRM):
```json
{
  "type": "AUDIO",
  "direction": "FROM_HUB",
  "details": {
    "to": "+5551996506656",
    "from": "+5548996027108",
    "file": {
      "url": "https://meucrm.com/storage/audio.mp3"
    }
  }
}
```

#### Respostas
Possui a mesma estrutura de respostas (`200 OK`, `401`, `404`) do Webhook Z-API.

---

## Fluxo Interno de Debounce (Buffer do Redis)
1. Quando uma mensagem bate em qualquer um dos webhooks, o número de telefone é higienizado para o formato E.164 (`+55DDXXXXXXXXX`).
2. O conteúdo da mensagem é inserido em uma lista do Redis sob a chave `whatsapp_buffer:{client_id}:{phone}` com TTL de 1 hora.
3. A requisição aguarda de forma assíncrona (**debounce de 20 segundos**).
4. Após os 20 segundos, a API verifica se novas mensagens chegaram.
   * Se nenhuma mensagem nova chegou, a execução atual ganha a prioridade: o buffer é limpo, as mensagens são concatenadas, e um job do worker ARQ é enfileirado (`process_whatsapp_response`) junto ao registro correspondente em `workflow_executions` com o status `PENDING`.
   * Se novas mensagens chegaram, a thread antiga descarta a si mesma para dar lugar ao novo fluxo concorrente que consolidará todas as mensagens.
