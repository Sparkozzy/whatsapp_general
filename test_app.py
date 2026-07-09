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
        "phone": "5548996027108",
        "message": "Olá",
        "type": "TEXT"
    }

    response = client.post("/webhook/whatsapp/crm/cliente-teste", json=payload, headers=headers)
    
    assert response.status_code == 200
    res_json = response.json()
    assert res_json["status"] == "discarded"
    
    # Ensure it didn't trigger enqueue as it's not the winning thread
    mock_arq.enqueue_job.assert_not_called()
