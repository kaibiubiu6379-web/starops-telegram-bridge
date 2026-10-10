import json
import os
import unittest
from unittest.mock import patch

import app as bridge


class FakeResponse:
    def raise_for_status(self):
        pass

    @staticmethod
    def json():
        return {"ok": True}


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = bridge.app.test_client()
        self.env = patch.dict(
            os.environ,
            {
                "WEBHOOK_SHARED_SECRET": "test-shared-secret",
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "test-chat",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_redacts_sensitive_fields_from_log_payload(self):
        payload = {
            "report": "normal detail",
            "bot_token": "do-not-log",
            "nested": {"clientSecret": "also-do-not-log"},
            "note": "password=hunter2",
        }
        self.assertEqual(
            bridge._redact_log_value(payload),
            {
                "report": "normal detail",
                "bot_token": "[REDACTED]",
                "nested": {"clientSecret": "[REDACTED]"},
                "note": "password=[REDACTED]",
            },
        )

    def test_redacts_auth_and_cookie_headers(self):
        with bridge.app.test_request_context(
            "/webhook",
            headers={
                "X-Webhook-Token": "do-not-log",
                "Cookie": "session=do-not-log",
                "User-Agent": "test-client",
            },
        ):
            safe_headers = bridge._safe_request_headers()
        self.assertEqual(safe_headers["X-Webhook-Token"], "[REDACTED]")
        self.assertEqual(safe_headers["Cookie"], "[REDACTED]")
        self.assertEqual(safe_headers["User-Agent"], "test-client")

    def test_requires_shared_secret(self):
        response = self.client.post("/webhook", json={"report": "test"})
        self.assertEqual(response.status_code, 401)

    @patch.object(bridge.requests, "post", return_value=FakeResponse())
    def test_allows_request_without_token_when_auth_disabled(self, post):
        with patch.dict(os.environ, {"WEBHOOK_AUTH_ENABLED": "false"}):
            response = self.client.post("/webhook", json={"report": "test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_count, 1)

    @patch.object(bridge.requests, "post", return_value=FakeResponse())
    def test_forwards_short_json_report(self, post):
        payload = {"report": "完整报告", "id": "event-1"}
        response = self.client.post(
            "/webhook",
            json=payload,
            headers={"X-Webhook-Token": "test-shared-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.args[0], "https://api.telegram.org/bottest-token/sendMessage")
        self.assertEqual(json.loads(post.call_args.kwargs["json"]["text"]), payload)

    @patch.object(bridge.requests, "post", return_value=FakeResponse())
    def test_forwards_long_report_as_complete_document(self, post):
        body = "报告正文" * 1200
        response = self.client.post(
            "/webhook",
            data=body,
            content_type="text/plain",
            headers={"X-Webhook-Token": "test-shared-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(post.call_args.args[0].endswith("/sendDocument"))
        uploaded = post.call_args.kwargs["files"]["document"][1].decode("utf-8")
        self.assertEqual(uploaded, body)


if __name__ == "__main__":
    unittest.main()
