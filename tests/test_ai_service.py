"""Unit tests for the ai_service module."""
import json
from unittest.mock import MagicMock, patch

import pytest


class TestGetAiResponse:
    def _mock_openai_response(self, payload: dict) -> MagicMock:
        """Build a mock that mimics the OpenAI chat completion response shape."""
        message = MagicMock()
        message.content = json.dumps(payload)
        choice = MagicMock()
        choice.message = message
        completion = MagicMock()
        completion.choices = [choice]
        return completion

    def _reload_svc(self, env: dict):
        import importlib
        import edge_agent.ai_service as svc
        with patch.dict("os.environ", env, clear=True), patch("dotenv.load_dotenv"):
            importlib.reload(svc)
        return svc

    def test_returns_dict_on_success(self):
        payload = {"p_true": 0.72, "bull_thesis": ["Strong momentum"], "disconfirming_evidence": []}
        completion = self._mock_openai_response(payload)

        svc = self._reload_svc({"OPEN_ROUTER_API_KEY": "test-key"})
        svc._client = MagicMock()
        svc._client.chat.completions.create.return_value = completion

        result = svc.get_ai_response("test prompt", task_type="complex")
        assert result == payload

    def test_returns_none_on_invalid_json(self):
        message = MagicMock()
        message.content = "not valid json {{{"
        choice = MagicMock()
        choice.message = message
        completion = MagicMock()
        completion.choices = [choice]

        svc = self._reload_svc({"OPEN_ROUTER_API_KEY": "test-key"})
        svc._client = MagicMock()
        svc._client.chat.completions.create.return_value = completion

        result = svc.get_ai_response("test prompt")
        assert result is None

    def test_returns_none_on_api_status_error(self):
        from openai import APIStatusError

        svc = self._reload_svc({"OPEN_ROUTER_API_KEY": "test-key"})
        svc._client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 429
        svc._client.chat.completions.create.side_effect = APIStatusError(
            "Rate limit exceeded", response=mock_response, body={}
        )

        result = svc.get_ai_response("test prompt")
        assert result is None

    def test_returns_none_on_api_connection_error(self):
        from openai import APIConnectionError

        svc = self._reload_svc({"OPEN_ROUTER_API_KEY": "test-key"})
        svc._client = MagicMock()
        mock_request = MagicMock()
        svc._client.chat.completions.create.side_effect = APIConnectionError(
            message="Connection refused", request=mock_request
        )

        result = svc.get_ai_response("test prompt")
        assert result is None

    def test_returns_none_when_client_not_initialized(self):
        svc = self._reload_svc({})
        assert svc._client is None
        result = svc.get_ai_response("test prompt")
        assert result is None
