"""Verify shared rejection preserves accounting and public errors."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from api.main import reject_request
from usage.tracking import TokenUsage


class RejectRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_once_and_preserves_response(self):
        with patch("api.main.record_request_usage", new_callable=AsyncMock) as record:
            response = await reject_request(
                virtual_key_id=7, provider=None, model="unknown", model_route=None,
                started_at=1.0, status="model_not_found", status_code=404,
                message="Unknown model", error_type="invalid_request_error",
                code="model_not_found", cache_status="miss",
            )
        record.assert_awaited_once_with(
            7, None, "unknown", None, TokenUsage(), 1.0,
            "model_not_found", 404, cache_status="miss",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.body), {"error": {
            "message": "Unknown model", "type": "invalid_request_error",
            "code": "model_not_found",
        }})

    async def test_database_failure_does_not_replace_error(self):
        with patch("api.main.create_usage_log_record", new_callable=AsyncMock,
                   side_effect=RuntimeError("fixture failure")):
            with self.assertLogs("api.main", level="ERROR"):
                response = await reject_request(
                    virtual_key_id=7, provider=None, model=None, model_route=None,
                    started_at=1.0, status="invalid_request", status_code=400,
                    message="Invalid JSON", error_type="invalid_request_error",
                    code="invalid_json",
                )
        self.assertEqual(response.status_code, 400)
