import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json
from services.agent import generate_llm_response_with_mcp

@pytest.mark.asyncio
async def test_generate_llm_response_with_mcp_returns_intermediate_steps():
    """
    TDD Test 1: generate_llm_response_with_mcp must return intermediate tool steps
    (assistant tool_calls and tool outputs) alongside the final response.
    """
    mock_openai = MagicMock()
    
    # 1st response from LLM: calls a tool
    tool_call_obj = MagicMock()
    tool_call_obj.id = "call_test_123"
    tool_call_obj.function.name = "getUserByPhone"
    tool_call_obj.function.arguments = json.dumps({"phone": "5551989408593"})
    
    msg_with_tool = MagicMock()
    msg_with_tool.tool_calls = [tool_call_obj]
    msg_with_tool.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [{
            "id": "call_test_123",
            "type": "function",
            "function": {
                "name": "getUserByPhone",
                "arguments": json.dumps({"phone": "5551989408593"})
            }
        }]
    }
    
    # 2nd response from LLM: final response JSON
    msg_final = MagicMock()
    msg_final.tool_calls = None
    msg_final.content = json.dumps({"type": "texto", "output": "Olá, Matheus! Localizei seu cadastro."})
    
    resp_1 = MagicMock()
    resp_1.choices = [MagicMock(message=msg_with_tool)]
    
    resp_2 = MagicMock()
    resp_2.choices = [MagicMock(message=msg_final)]
    
    mock_openai.chat.completions.create = AsyncMock(side_effect=[resp_1, resp_2])
    
    # Mock MCP session
    mock_tool_obj = MagicMock()
    mock_tool_obj.name = "getUserByPhone"
    mock_tool_obj.description = "Search user by phone"
    mock_tool_obj.inputSchema = {"type": "object"}
    
    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[mock_tool_obj]))
    
    tool_content_item = MagicMock()
    tool_content_item.type = "text"
    tool_content_item.text = json.dumps({"singleUserId": 5938, "singleAccountId": 1006, "length": 1})
    mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[tool_content_item]))
    
    mock_sse_ctx = MagicMock()
    mock_sse_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    mock_sse_ctx.__aexit__ = AsyncMock(return_value=None)
    
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
    
    with patch("services.agent.sse_client", return_value=mock_sse_ctx), \
         patch("services.agent.ClientSession", return_value=mock_session_ctx):
        
        result = await generate_llm_response_with_mcp(
            mock_openai,
            system_prompt="System prompt test",
            chat_history=[],
            user_message="Boa tarde",
            mcp_urls=["https://mock-mcp.test/sse"],
            model="gpt-4o"
        )
        
        # Must return dict with response and intermediate_steps
        assert "response" in result or "type" in result
        # Check intermediate_steps structure
        intermediate_steps = result.get("intermediate_steps")
        assert intermediate_steps is not None, "generate_llm_response_with_mcp must return intermediate_steps"
        assert len(intermediate_steps) == 2
        
        # Step 1: assistant tool call
        assert intermediate_steps[0]["role"] == "assistant"
        assert intermediate_steps[0]["tool_calls"][0]["id"] == "call_test_123"
        
        # Step 2: tool response
        assert intermediate_steps[1]["role"] == "tool"
        assert intermediate_steps[1]["tool_call_id"] == "call_test_123"
        assert "5938" in intermediate_steps[1]["content"]


def test_load_chat_history_with_tools():
    """
    TDD Test 2: worker.load_chat_history must reconstitute tool messages,
    assistant tool_calls, and human messages into OpenAI-compatible format.
    """
    from worker import parse_chat_history_rows
    
    raw_rows = [
        {
            "id": 1,
            "message": {
                "type": "human",
                "content": "Boa tarde"
            }
        },
        {
            "id": 2,
            "message": {
                "type": "ai",
                "content": "",
                "additional_kwargs": {
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "getUserByPhone", "arguments": "{\"phone\":\"5551989408593\"}"}
                    }]
                }
            }
        },
        {
            "id": 3,
            "message": {
                "type": "tool",
                "name": "getUserByPhone",
                "tool_call_id": "call_abc",
                "content": "{\"singleUserId\": 5938, \"singleAccountId\": 1006}"
            }
        },
        {
            "id": 4,
            "message": {
                "type": "ai",
                "content": "Olá, Matheus! Localizei seu cadastro no Residenz Platz."
            }
        }
    ]
    
    history = parse_chat_history_rows(raw_rows)
    assert len(history) == 4
    
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Boa tarde"
    
    assert history[1]["role"] == "assistant"
    assert history[1]["tool_calls"][0]["id"] == "call_abc"
    
    assert history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "call_abc"
    assert "5938" in history[2]["content"]
    
    assert history[3]["role"] == "assistant"
    assert "Residenz Platz" in history[3]["content"]


def test_prepare_chat_memory_records():
    """
    TDD Test 3: worker.prepare_chat_memory_records must create proper records to insert
    including human message, intermediate tool steps, and final ai message.
    """
    from worker import prepare_chat_memory_records
    
    phone = "+5551989408593"
    user_text = "Boa tarde"
    output_text = "Olá, Matheus!"
    intermediate_steps = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_abc",
                "type": "function",
                "function": {"name": "getUserByPhone", "arguments": "{\"phone\":\"5551989408593\"}"}
            }]
        },
        {
            "role": "tool",
            "name": "getUserByPhone",
            "tool_call_id": "call_abc",
            "content": "{\"singleUserId\": 5938, \"singleAccountId\": 1006}"
        }
    ]
    
    records = prepare_chat_memory_records(phone, user_text, output_text, intermediate_steps)
    
    assert len(records) == 4
    assert records[0]["session_id"] == phone
    assert records[0]["message"]["type"] == "human"
    assert records[0]["message"]["content"] == user_text
    
    assert records[1]["session_id"] == phone
    assert records[1]["message"]["type"] == "ai"
    assert records[1]["message"]["additional_kwargs"]["tool_calls"][0]["id"] == "call_abc"
    
    assert records[2]["session_id"] == phone
    assert records[2]["message"]["type"] == "tool"
    assert records[2]["message"]["tool_call_id"] == "call_abc"
    assert records[2]["message"]["content"] == "{\"singleUserId\": 5938, \"singleAccountId\": 1006}"
    
    assert records[3]["session_id"] == phone
    assert records[3]["message"]["type"] == "ai"
    assert records[3]["message"]["content"] == output_text
