from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from watchwebhook import (
    ConfigurationError,
    DeliveryError,
    WatchWebhook,
    WebhookAlreadyFinishedError,
    to_json_safe,
)


class _Receiver(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    status = 202
    ready = threading.Event()

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        type(self).requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(body),
            }
        )
        type(self).ready.set()
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class SDKBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _Receiver.requests = []
        _Receiver.status = 202
        _Receiver.ready.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/collector/"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join()

    def setUp(self) -> None:
        _Receiver.requests.clear()
        _Receiver.status = 202
        _Receiver.ready.clear()

    def _client(self, **kwargs: Any) -> WatchWebhook:
        return WatchWebhook(
            api_key="test-secret",
            project_id="orders",
            base_url=self.base_url,
            timeout=1,
            **kwargs,
        )

    def test_webhook_is_authenticated_redacted_and_json_safe(self) -> None:
        event = self._client().start_webhook(
            method="post",
            url="https://orders.example/hook",
            headers={"Authorization": "secret", "X-Request-ID": 7},
            query={"attempt": 2},
            body=b"{\"ok\":true}",
            source="stripe",
            metadata={"received_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        )

        self.assertFalse(_Receiver.ready.is_set())
        result = event.success(status_code=200)

        self.assertTrue(_Receiver.ready.wait(1))
        request = _Receiver.requests[0]
        envelope = request["body"]
        self.assertEqual(result.status_code, 202)
        self.assertTrue(result.delivered)
        self.assertEqual(request["path"], "/collector/v1/events")
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-secret")
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(envelope["schema_version"], "1")
        self.assertEqual(envelope["kind"], "webhook")
        self.assertEqual(envelope["project_id"], "orders")
        self.assertEqual(envelope["payload"]["method"], "POST")
        self.assertEqual(envelope["payload"]["outcome"], "success")
        self.assertEqual(envelope["payload"]["status_code"], 200)
        self.assertNotIn("error", envelope["payload"])
        self.assertNotIn("headers", envelope["payload"])
        self.assertNotIn("query", envelope["payload"])
        self.assertNotIn("body", envelope["payload"])
        self.assertEqual(envelope["payload"]["metadata"]["received_at"], "2026-01-01T00:00:00Z")
    def test_binary_body_is_base64_encoded(self) -> None:
        event = self._client().start_webhook(
            method="PUT",
            url="https://example.test/hook",
            body=b"\xff\x00",
        )
        event.fail(error="invalid payload")

        self.assertTrue(_Receiver.ready.wait(1))
        payload = _Receiver.requests[0]["body"]["payload"]
        self.assertEqual(payload["body"], "/wA=")
        self.assertEqual(payload["body_encoding"], "base64")


    def test_custom_event_uses_supplied_time(self) -> None:
        result = self._client().capture_event(
            "order.fulfilled",
            {"order_id": 123},
            occurred_at=datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        )

        self.assertTrue(result.delivered)
        self.assertTrue(_Receiver.ready.wait(1))
        envelope = _Receiver.requests[0]["body"]
        self.assertEqual(envelope["kind"], "event")
        self.assertEqual(envelope["occurred_at"], "2026-02-03T04:05:06Z")
        self.assertEqual(envelope["payload"]["data"], {"order_id": 123})

    def test_exception_inside_except_keeps_original_traceback(self) -> None:
        client = self._client()
        try:
            raise RuntimeError("database unavailable")
        except RuntimeError:
            result = client.capture_exception(context={"operation": "save"})

        self.assertTrue(result.delivered)
        self.assertTrue(_Receiver.ready.wait(1))
        payload = _Receiver.requests[0]["body"]["payload"]
        self.assertEqual(payload["exception_type"], "RuntimeError")
        self.assertEqual(payload["message"], "database unavailable")
        self.assertIn("RuntimeError: database unavailable", payload["traceback"])
        self.assertEqual(payload["context"], {"operation": "save"})

    def test_webhook_failure_records_exception_and_result(self) -> None:
        event = self._client().start_webhook(
            source="stripe",
            headers={"Authorization": "secret", "X-Request-ID": 7},
            query={"attempt": 2},
            body=b"{\"ok\":true}",
        )
        try:
            raise RuntimeError("payment failed")
        except RuntimeError as exc:
            result = event.fail(error=exc, status_code=422)

        self.assertTrue(result.delivered)
        self.assertTrue(_Receiver.ready.wait(1))
        payload = _Receiver.requests[0]["body"]["payload"]
        self.assertEqual(payload["outcome"], "failure")
        self.assertEqual(payload["status_code"], 422)
        self.assertEqual(payload["error"]["exception_type"], "RuntimeError")
        self.assertEqual(payload["error"]["message"], "payment failed")
        self.assertEqual(payload["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(payload["headers"]["X-Request-ID"], "7")
        self.assertEqual(payload["query"], {"attempt": 2})
        self.assertEqual(payload["body"], '{"ok":true}')
        self.assertEqual(payload["body_encoding"], "utf-8")

    def test_webhook_can_only_be_finished_once(self) -> None:
        event = self._client().start_webhook()
        event.success()

        with self.assertRaises(WebhookAlreadyFinishedError):
            event.fail(error="too late")

        self.assertEqual(len(_Receiver.requests), 1)


    def test_delivery_failure_is_non_fatal_by_default(self) -> None:
        _Receiver.status = 503
        result = self._client().capture_event("failed.delivery")

        self.assertFalse(result.delivered)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.error, "server returned HTTP 503")

    def test_delivery_failure_can_raise(self) -> None:
        _Receiver.status = 503
        with self.assertRaises(DeliveryError) as raised:
            self._client(fail_silently=False).capture_event("required.delivery")

        self.assertFalse(raised.exception.result.delivered)
        self.assertEqual(raised.exception.result.status_code, 503)

    def test_configuration_and_capture_validation(self) -> None:
        with self.assertRaises(ConfigurationError):
            WatchWebhook(api_key="", project_id="orders")
        with self.assertRaises(ConfigurationError):
            WatchWebhook(api_key="key", project_id="orders", base_url="localhost")
        with self.assertRaises(ValueError):
            self._client().capture_event("")
        with self.assertRaises(ValueError):
            self._client().capture_exception()
        with self.assertRaises(ValueError):
            self._client().start_webhook(method="")

    def test_json_safe_handles_cycles_and_non_finite_numbers(self) -> None:
        values: dict[str, Any] = {"number": float("inf")}
        values["self"] = values

        self.assertEqual(to_json_safe(values), {"number": "Infinity", "self": "<circular reference>"})


if __name__ == "__main__":
    unittest.main()
