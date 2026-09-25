import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

# Mock environmental vars before importing app
import os
os.environ["MASTER_SUPABASE_URL"] = "https://mock-master.supabase.co"
os.environ["MASTER_SUPABASE_SERVICE_KEY"] = "mock-service-key"
os.environ["REDIS_URL"] = "redis://localhost:6379"
os.environ["OPENAI_API_KEY"] = "mock-openai-key"

from main import app, sanitize_phone
from database import ClientDatabaseManager

client = TestClient(app)

@pytest.fixture
def mock_client_config():
    with patch.object(ClientDatabaseManager, "get_client_config") as mock_get:
        mock_get.return_value = {
            "client_id": "cliente-teste",
            "mindflow_api_token": "valid-mindflow-token",
            "zapi_instance_id": "123",
            "zapi_client_token": "client-token",
            "zapi_security_token": "security-token",
            "prompt_id": 1
        }
        yield mock_get

@pytest.fixture
def mock_supabase_client():
    with patch.object(ClientDatabaseManager, "get_client") as mock_db:
        mock_supabase = MagicMock()
        mock_table = MagicMock()
        mock_insert = MagicMock()
        mock_execute = MagicMock()
        
        mock_execute.return_value.data = [{"id": "mock-exec-uuid"}]
        mock_insert.return_value.execute = mock_execute
        mock_table.insert = mock_insert
        mock_supabase.table.return_value = mock_table
        
        mock_db.return_value = mock_supabase
        yield mock_supabase

def test_sanitize_phone():
    assert sanitize_phone("5548996027108") == "+5548996027108"
    assert sanitize_phone("+55 (48) 99602-7108") == "+5548996027108"
    assert sanitize_phone("48996027108") == "+5548996027108"

def test_unauthorized_access(mock_client_config):
    # No token header
    response = client.post("/webhook/whatsapp/zapi/cliente-teste", json={})
    assert response.status_code == 401
    
    # Invalid token header
    headers = {"X-MindFlow-Token": "invalid-token"}
    response = client.post("/webhook/whatsapp/zapi/cliente-teste", json={}, headers=headers)
    assert response.status_code == 401

    # Invalid token query param
    response = client.post("/webhook/whatsapp/zapi/cliente-teste?token=invalid-token", json={})
    assert response.status_code == 401

@patch("main.redis_client", new_callable=AsyncMock)
@patch("main.arq_pool", new_callable=AsyncMock)
def test_query_token_success(mock_arq, mock_redis, mock_client_config, mock_supabase_client):
    mock_redis.lrange.side_effect = [
        ["Olá"],
        ["Olá"]
    ]
    payload = {
        "eventType": "MESSAGE_RECEIVED",
        "content": {
            "type": "TEXT",
            "text": "Olá",
            "direction": "FROM_HUB",
            "details": {
                "to": "+5551996506656",
                "from": "+5548996027108"
            }
        }
    }
    response = client.post("/webhook/whatsapp/crm/cliente-teste?token=valid-mindflow-token", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


@patch("main.redis_client", new_callable=AsyncMock)
@patch("main.arq_pool", new_callable=AsyncMock)
def test_zapi_webhook_success(mock_arq, mock_redis, mock_client_config, mock_supabase_client):
    # Setup Redis lrange return values (strings are safe unicode literals in Python 3)
    mock_redis.lrange.side_effect = [
        ["Olá"], # Pre-wait
        ["Olá"]  # Post-wait
    ]

    headers = {"X-MindFlow-Token": "valid-mindflow-token"}
    payload = {
        "instanceId": "instance-123",
        "eventType": "MESSAGE_RECEIVED",
        "content": {
            "type": "TEXT",
            "text": "Olá",
            "details": {
                "from": "5548996027108"
            }
        }
    }

    response = client.post("/webhook/whatsapp/zapi/cliente-teste", json=payload, headers=headers)
    
    assert response.status_code == 200
    res_json = response.json()
    assert res_json["status"] == "accepted"
    assert res_json["execution_id"] == "mock-exec-uuid"
    
    # Verify Redis list push was called
    mock_redis.rpush.assert_called_once()
    # Verify task was enqueued on arq
    mock_arq.enqueue_job.assert_called_once_with(
        "process_whatsapp_response",
        "cliente-teste",
        "+5548996027108",
        "Olá",
        "mock-exec-uuid"
    )

@patch("main.analyze_image", new_callable=AsyncMock)
@patch("main.redis_client", new_callable=AsyncMock)
@patch("main.arq_pool", new_callable=AsyncMock)
def test_zapi_webhook_image_success(mock_arq, mock_redis, mock_analyze_image, mock_client_config, mock_supabase_client):
    mock_analyze_image.return_value = "[Imagem: Foto do documento de identidade | Legenda enviada com a foto: Segue foto]"
    mock_redis.lrange.side_effect = [
        ["[Imagem: Foto do documento de identidade | Legenda enviada com a foto: Segue foto]"],
        ["[Imagem: Foto do documento de identidade | Legenda enviada com a foto: Segue foto]"]
    ]

    headers = {"X-MindFlow-Token": "valid-mindflow-token"}
    payload = {
        "instanceId": "instance-123",
        "eventType": "MESSAGE_RECEIVED",
        "content": {
            "type": "IMAGE",
            "text": "Segue foto",
            "details": {
                "from": "5548996027108",
                "file": {
                    "publicUrl": "https://example.com/image.jpg"
                }
            }
        }
    }

    response = client.post("/webhook/whatsapp/zapi/cliente-teste", json=payload, headers=headers)
    assert response.status_code == 200
    res_json = response.json()
    assert res_json["status"] == "accepted"
    mock_analyze_image.assert_called_once()


@patch("main.summarize_pdf_first_page", new_callable=AsyncMock)
@patch("main.redis_client", new_callable=AsyncMock)
@patch("main.arq_pool", new_callable=AsyncMock)
def test_zapi_webhook_pdf_document_success(mock_arq, mock_redis, mock_summarize_pdf, mock_client_config, mock_supabase_client):
    mock_summarize_pdf.return_value = "[Documento PDF Recebido | Tipo: CNH | Resumo: Carteira de Habilitação de João]"
    mock_redis.lrange.side_effect = [
        ["[Documento PDF Recebido | Tipo: CNH | Resumo: Carteira de Habilitação de João]"],
        ["[Documento PDF Recebido | Tipo: CNH | Resumo: Carteira de Habilitação de João]"]
    ]

    headers = {"X-MindFlow-Token": "valid-mindflow-token"}
    payload = {
        "instanceId": "instance-123",
        "eventType": "MESSAGE_RECEIVED",
        "content": {
            "type": "DOCUMENT",
            "text": "Segue contrato",
            "details": {
                "from": "5548996027108",
                "file": {
                    "publicUrl": "https://example.com/contrato.pdf",
                    "fileName": "contrato.pdf"
                }
            }
        }
    }

    response = client.post("/webhook/whatsapp/zapi/cliente-teste", json=payload, headers=headers)
    assert response.status_code == 200
    res_json = response.json()
    assert res_json["status"] == "accepted"
    assert res_json["execution_id"] == "mock-exec-uuid"
    mock_summarize_pdf.assert_called_once()





@patch("main.redis_client", new_callable=AsyncMock)
@patch("main.arq_pool", new_callable=AsyncMock)
def test_crm_webhook_success(mock_arq, mock_redis, mock_client_config, mock_supabase_client):
    # Setup Redis to simulate different snapshots (non-winning thread)
    mock_redis.lrange.side_effect = [
        ["Olá"], # Pre-wait
        ["Olá", "Tudo bem?"] # Post-wait (new message arrived during sleep)
    ]

    headers = {"X-MindFlow-Token": "valid-mindflow-token"}
    payload = {
        "eventType": "MESSAGE_RECEIVED",
        "content": {
            "type": "TEXT",
            "text": "Olá",
            "direction": "FROM_HUB",
            "details": {
                "to": "+5551996506656",
                "from": "+5548996027108"
            }
        }
    }

    response = client.post("/webhook/whatsapp/crm/cliente-teste", json=payload, headers=headers)
    
    assert response.status_code == 200
    res_json = response.json()
    assert res_json["status"] == "discarded"
    
    # Ensure it didn't trigger enqueue as it's not the winning thread
    mock_arq.enqueue_job.assert_not_called()

@patch("main.redis_client", new_callable=AsyncMock)
@patch("main.arq_pool", new_callable=AsyncMock)
def test_crm_webhook_ignored_direction(mock_arq, mock_redis, mock_client_config, mock_supabase_client):
    headers = {"X-MindFlow-Token": "valid-mindflow-token"}
    payload = {
        "eventType": "MESSAGE_RECEIVED",
        "content": {
            "type": "TEXT",
            "text": "Olá",
            "direction": "TO_HUB",
            "details": {
                "to": "+5548996027108",
                "from": "+5551996506656"
            }
        }
    }

    response = client.post("/webhook/whatsapp/crm/cliente-teste", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


from services.agent import generate_tts_audio
from worker import process_whatsapp_response

@pytest.mark.asyncio
async def test_generate_tts_audio_custom_voice():
    mock_openai = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = b"audio-data"
    mock_openai.audio.speech.create.return_value = mock_response

    res = await generate_tts_audio(mock_openai, "olá", voice="alloy")
    
    assert res is not None
    mock_openai.audio.speech.create.assert_called_once_with(
        model="tts-1",
        voice="alloy",
        input="olá"
    )

@pytest.mark.asyncio
@patch("worker.ClientDatabaseManager")
@patch("worker.master_supabase")
@patch("worker.generate_llm_response", new_callable=AsyncMock)
@patch("worker.generate_tts_audio", new_callable=AsyncMock)
@patch("worker.send_audio", new_callable=AsyncMock)
@patch("worker.run_step_with_retry", new_callable=AsyncMock)
async def test_process_whatsapp_response_custom_voice(
    mock_run_step, mock_send_audio, mock_generate_tts, mock_generate_llm, mock_master_supabase, mock_db_mgr
):
    # Mock tenant config returning a custom voice_id
    mock_db_mgr.get_client_config.return_value = {
        "client_id": "cliente-teste",
        "prompt_id": 123,
        "voice_id": "shimmer"
    }
    
    # Mock tenant supabase client
    mock_supabase = MagicMock()
    mock_db_mgr.get_client.return_value = mock_supabase
    
    # Mock routing for different tables in tenant DB
    def mock_table_routing(table_name):
        table_mock = MagicMock()
        execute_mock = MagicMock()
        
        if table_name == "Blacklist_Mindflow":
            execute_mock.return_value.data = []  # Not blacklisted
        elif table_name == "Leads_Mindflow":
            execute_mock.return_value.data = [{"id": 1, "Número": "+5548996027108"}]
        else:
            execute_mock.return_value.data = [{"id": "mock-id"}]
            
        table_mock.select.return_value.eq.return_value.execute = execute_mock
        table_mock.select.return_value.eq.return_value.order.return_value.limit.return_value.execute = execute_mock
        table_mock.insert.return_value.execute = execute_mock
        table_mock.update.return_value.eq.return_value.execute = execute_mock
        return table_mock

    mock_supabase.table.side_effect = mock_table_routing

    # Mock master_supabase for prompt fetching
    mock_prompt_execute = MagicMock()
    mock_prompt_execute.data = {"Prompt_Text": "System Prompt"}
    mock_master_supabase.table.return_value.select.return_value.eq.return_value.single.return_value.execute = mock_prompt_execute
    
    # Mock LLM response to trigger audio generation
    mock_generate_llm.return_value = {
        "type": "audio",
        "output": "Resposta em áudio"
    }
    
    # Mock TTS generation output
    mock_generate_tts.return_value = "mock-audio-b64"
    
    # Mock run_step_with_retry behavior to execute the actual functions
    async def side_effect_run_step(step_name, execution_id, tenant_db, func, *args, **kwargs):
        return await func()
    mock_run_step.side_effect = side_effect_run_step

    ctx = {"openai": AsyncMock()}
    
    await process_whatsapp_response(ctx, "cliente-teste", "+5548996027108", "Olá", "mock-exec-123")
    
    # Verify that get_client_config was fetched
    mock_db_mgr.get_client_config.assert_called_once_with("cliente-teste")
    
    # Verify generate_tts_audio was called with the custom voice_id "shimmer"
    mock_generate_tts.assert_called_once_with(ctx["openai"], "Resposta em áudio", voice="shimmer")


# ==========================================
# TESTES DE FUP (FOLLOW-UP AGENT)
# ==========================================

from services.agent import generate_fup_decision
from worker import process_fup_request, execute_scheduled_fup_action

@pytest.mark.asyncio
async def test_generate_fup_decision_success():
    """
    Testa se o agente LLM determinístico gera a resposta estrita em JSON
    com uma das 4 ações e formato válido.
    """
    mock_openai = AsyncMock()
    mock_completion = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = (
        '{"acao": "agendar_mensagem", "quando_executar": "2026-09-25T15:00:00-03:00", '
        '"conteudo": "Olá, tudo bem?", "justificativa": "Lead inativo há 4 horas"}'
    )
    mock_completion.choices = [mock_choice]
    mock_openai.chat.completions.create.return_value = mock_completion

    lead_info = {"Nome": "Carlos", "Número": "+5548996027108", "Etapa CRM": "Qualificação"}
    chat_history = [{"role": "user", "content": "Me manda mais detalhes?"}]

    res = await generate_fup_decision(
        mock_openai,
        system_prompt="Você é um agente de follow-up",
        lead_info=lead_info,
        chat_history=chat_history
    )

    assert res["acao"] == "agendar_mensagem"
    assert res["quando_executar"] == "2026-09-25T15:00:00-03:00"
    assert res["conteudo"] == "Olá, tudo bem?"


@pytest.mark.asyncio
async def test_generate_fup_decision_retry_on_invalid_json():
    """
    Testa se o agente executa até 3 retentativas caso a LLM gere JSON inválido
    ou ação não homologada, recuperando com sucesso na tentativa subsequente.
    """
    mock_openai = AsyncMock()
    
    mock_fail_choice = MagicMock()
    mock_fail_choice.message.content = '{"acao": "acao_invalida", "quando_executar": "hoje"}'
    mock_fail_completion = MagicMock()
    mock_fail_completion.choices = [mock_fail_choice]

    mock_ok_choice = MagicMock()
    mock_ok_choice.message.content = '{"acao": "figurinha", "quando_executar": "2026-09-25T16:00:00-03:00", "conteudo": "sticker_1"}'
    mock_ok_completion = MagicMock()
    mock_ok_completion.choices = [mock_ok_choice]

    mock_openai.chat.completions.create.side_effect = [mock_fail_completion, mock_ok_completion]

    res = await generate_fup_decision(
        mock_openai,
        system_prompt="Prompt",
        lead_info={"Nome": "Ana"},
        chat_history=[]
    )

    assert res["acao"] == "figurinha"
    assert mock_openai.chat.completions.create.call_count == 2


@patch("main.process_fup_request", new_callable=AsyncMock)
def test_fup_webhook_endpoint(mock_process, mock_client_config, mock_supabase_client):
    """
    Testa o endpoint de recepção de FUP POST /webhook/whatsapp/fup/{client_id}
    """
    payload = {
        "client_id": "cliente-teste",
        "phone": "+5548996027108",
        "custom_context": "Lead pediu proposta ontem"
    }
    headers = {"X-MindFlow-Token": "valid-mindflow-token"}

    response = client.post("/webhook/whatsapp/fup/cliente-teste", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert "execution_id" in response.json()



@pytest.mark.asyncio
@patch("worker.ClientDatabaseManager")
@patch("worker.master_supabase")
@patch("worker.run_step_with_retry")
@patch("worker.generate_fup_decision")
async def test_process_fup_request_scheduling(
    mock_generate_fup,
    mock_run_step,
    mock_master_supabase,
    mock_db_mgr
):
    """
    Testa o ciclo completo do worker de FUP:
    leitura de prompt, LLM determinístico, validação de permissões e agendamento Redis ARQ.
    """
    mock_supabase = MagicMock()
    mock_db_mgr.get_client.return_value = mock_supabase
    mock_db_mgr.get_client_config.return_value = {
        "client_id": "cliente-teste",
        "fup": True,
        "fup_ligawhats": True,
        "fup_ligacao": True,
        "prompt_id": 1
    }

    mock_generate_fup.return_value = {
        "acao": "agendar_mensagem",
        "quando_executar": "2026-09-25T18:00:00-03:00",
        "conteudo": "Oi, conseguiu ver?",
        "justificativa": "Follow-up padrão"
    }

    async def side_effect_run_step(step_name, execution_id, tenant_db, func, *args, **kwargs):
        return await func()
    mock_run_step.side_effect = side_effect_run_step

    mock_redis_pool = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "test-job-uuid-123"
    mock_redis_pool.enqueue_job.return_value = mock_job

    ctx = {
        "openai": AsyncMock(),
        "redis": mock_redis_pool
    }

    await process_fup_request(ctx, "cliente-teste", "+5548996027108", "mock-fup-exec-123")

    # Verifica se a task foi enfileirada no Redis com _defer_until
    mock_redis_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_redis_pool.enqueue_job.call_args
    assert args[0] == "execute_scheduled_fup_action"
    assert args[1] == "cliente-teste"
    assert args[2] == "+5548996027108"
    assert args[3] == "agendar_mensagem"
    assert "_defer_until" in kwargs


@pytest.mark.asyncio
@patch("worker.ClientDatabaseManager")
@patch("worker.run_step_with_retry")
@patch("worker.send_audio")
@patch("worker.generate_tts_audio")
async def test_execute_scheduled_fup_audio(
    mock_generate_tts,
    mock_send_audio,
    mock_run_step,
    mock_db_mgr
):
    """
    Testa a execução agendada da ação 'audio', verificando a síntese de voz (TTS)
    e envio via Z-API.
    """
    mock_supabase = MagicMock()
    mock_db_mgr.get_client.return_value = mock_supabase
    mock_db_mgr.get_client_config.return_value = {
        "client_id": "cliente-teste",
        "voice_id": "nova",
        "tts_provider": "openai"
    }

    mock_generate_tts.return_value = "mocked-audio-b64-content"
    mock_send_audio.return_value = {"status": "sent"}

    async def side_effect_run_step(step_name, execution_id, tenant_db, func, *args, **kwargs):
        return await func()
    mock_run_step.side_effect = side_effect_run_step

    ctx = {"openai": AsyncMock()}

    await execute_scheduled_fup_action(
        ctx=ctx,
        client_id="cliente-teste",
        phone="+5548996027108",
        action_type="audio",
        content="Olá, este é um áudio de follow up!",
        execution_id="mock-exec-audio-123"
    )

    mock_generate_tts.assert_called_once_with(ctx["openai"], "Olá, este é um áudio de follow up!", voice="nova")
    mock_send_audio.assert_called_once()




