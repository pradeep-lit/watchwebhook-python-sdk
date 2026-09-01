from __future__ import annotations

import json
import sys
import traceback as traceback_module
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from threading import Lock
from time import monotonic_ns
from types import TracebackType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .models import (
    CapturedEvent,
    CapturedException,
    CapturedWebhook,
    DeliveryResult,
    EventEnvelope,
    EventKind,
    WebhookOutcome,
    encode_webhook_body,
    to_json_safe,
    utc_now,
)

try:
    SDK_VERSION = version("watchwebhook-python-sdk")
except PackageNotFoundError:
    SDK_VERSION = "0.1.0"

DEFAULT_BASE_URL = "https://api.watchwebhook.com"
DEFAULT_TIMEOUT = 5.0
_REDACTED = "[REDACTED]"
_DEFAULT_SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "set-cookie", "proxy-authorization"}
)


class WatchWebhookError(Exception):
    """Base class for errors raised by the WatchWebhook SDK."""


class ConfigurationError(WatchWebhookError, ValueError):
    """Raised when client configuration is invalid."""


class DeliveryError(WatchWebhookError):
    """Raised when delivery fails and ``fail_silently`` is disabled."""

    def __init__(self, message: str, *, result: DeliveryResult) -> None:
        super().__init__(message)
        self.result = result


class WebhookAlreadyFinishedError(WatchWebhookError, RuntimeError):
    """Raised when a webhook result is reported more than once."""


class WebhookEvent:

    def __init__(
        self,
        client: WatchWebhook,
        *,
        event_id: str,
        payload: CapturedWebhook,
        occurred_at: datetime,
        started_at_ns: int,
    ) -> None:
        self._client = client
        self._event_id = event_id
        self._payload = payload
        self._occurred_at = occurred_at
        self._started_at_ns = started_at_ns
        self._finished = False
        self._lock = Lock()

    @property
    def event_id(self) -> str:
        """Return the stable ID used for this webhook capture."""
        return self._event_id

    def success(self, *, status_code: int = 200) -> DeliveryResult:
        """Report that application processing completed successfully."""
        finished_at_ns = monotonic_ns()
        status_code = _validate_status_code(status_code)
        return self._finish(
            outcome="success",
            status_code=status_code,
            error=None,
            finished_at_ns=finished_at_ns,
        )

    def fail(
        self,
        *,
        error: BaseException | str,
        status_code: int = 500,
    ) -> DeliveryResult:
        """Report a processing failure while leaving the original error untouched."""
        finished_at_ns = monotonic_ns()
        status_code = _validate_status_code(status_code)
        if isinstance(error, BaseException):
            error_payload = self._client._exception_payload(error)
        elif isinstance(error, str) and error:
            error_payload = {"message": error}
        else:
            raise TypeError("error must be a non-empty string or BaseException")
        return self._finish(
            outcome="failure",
            status_code=status_code,
            error=error_payload,
            finished_at_ns=finished_at_ns,
        )

    def _finish(
        self,
        *,
        outcome: WebhookOutcome,
        status_code: int,
        error: dict[str, Any] | None,
        finished_at_ns: int,
    ) -> DeliveryResult:
        with self._lock:
            if self._finished:
                raise WebhookAlreadyFinishedError(
                    f"webhook {self._event_id} has already been finished"
                )
            time_took_ms = round(
                (finished_at_ns - self._started_at_ns) / 1_000_000,
                3,
            )
            self._finished = True

        payload = replace(
            self._payload,
            outcome=outcome,
            status_code=status_code,
            error=error,
            time_took_ms=time_took_ms,
        )
        return self._client._deliver(
            "webhook",
            payload,
            event_id=self._event_id,
            occurred_at=self._occurred_at,
        )


def _validate_status_code(status_code: int) -> int:
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise ValueError("status_code must be an integer")
    if not 100 <= status_code <= 599:
        raise ValueError("status_code must be between 100 and 599")
    return status_code


class WatchWebhook:
    """Capture webhook traffic, application events, and exceptions.

    The client is deliberately framework-neutral. Framework adapters can pass
    ordinary Python values from Flask, Django, FastAPI, or another backend.
    Webhooks use ``start_webhook()`` followed by exactly one ``success()`` or
    ``fail()`` call. Terminal reporting sends one request to
    ``POST {base_url}/v1/events``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        project_id: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        fail_silently: bool = True,
        sensitive_headers: set[str] | frozenset[str] | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ConfigurationError("api_key must not be empty")
        if not project_id or not project_id.strip():
            raise ConfigurationError("project_id must not be empty")
        if timeout <= 0:
            raise ConfigurationError("timeout must be greater than zero")

        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("base_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ConfigurationError("base_url must not contain a query or fragment")

        self.api_key = api_key
        self.project_id = project_id
        self.base_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self.timeout = timeout
        self.fail_silently = fail_silently
        self._sensitive_headers = frozenset(
            header.lower() for header in (sensitive_headers or _DEFAULT_SENSITIVE_HEADERS)
        )

    def start_webhook(
        self,
        *,
        source: str | None = None,
        method: str = "POST",
        url: str | None = None,
        headers: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> WebhookEvent:
        """Start a local webhook capture without sending a network request.

        The returned handle must be completed with exactly one ``success`` or
        ``fail`` call. The original request body remains available to the
        caller for parsing and business processing.
        """
        if not method or not method.strip():
            raise ValueError("method must not be empty")
        if url is not None and not url.strip():
            raise ValueError("url must not be empty when provided")
        received_at = occurred_at or utc_now()

        encoded_body, body_encoding = encode_webhook_body(body)
        payload = CapturedWebhook(
            method=method.upper(),
            url=url,
            headers=self._headers(headers),
            query=to_json_safe(dict(query or {})),
            body=encoded_body,
            body_encoding=body_encoding,
            source=source,
            metadata=to_json_safe(dict(metadata or {})),
            received_at=received_at,
            time_took_ms=None,
            status_code=None,
            error=None,
            outcome="pending",
        )
        return WebhookEvent(
            self,
            event_id=str(uuid4()),
            payload=payload,
            occurred_at=received_at,
            started_at_ns=monotonic_ns(),
        )

    def capture_event(
        self,
        name: str,
        data: Any = None,
        *,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> DeliveryResult:
        """Capture an application-defined event."""
        if not name or not name.strip():
            raise ValueError("name must not be empty")
        payload = CapturedEvent(
            name=name,
            data=to_json_safe(data),
            metadata=to_json_safe(dict(metadata or {})),
        )
        return self._deliver("event", payload, occurred_at=occurred_at)

    def capture_exception(
        self,
        exception: BaseException | None = None,
        *,
        context: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> DeliveryResult:
        """Capture an exception, including its current traceback when available.

        Call this inside an ``except`` block without an argument to use the
        active exception from ``sys.exc_info()``. The exception is never raised
        or modified by this method.
        """
        _, active_value, active_traceback = sys.exc_info()
        captured = exception if exception is not None else active_value
        if captured is None:
            raise ValueError("exception must be provided outside an except block")

        details = self._exception_payload(
            captured, active_traceback if captured is active_value else None
        )
        payload = CapturedException(
            exception_type=details["exception_type"],
            exception_module=details["exception_module"],
            message=details["message"],
            traceback=details["traceback"],
            context=to_json_safe(dict(context or {})),
            metadata=to_json_safe(dict(metadata or {})),
        )
        return self._deliver("exception", payload, occurred_at=occurred_at)

    def _deliver(
        self,
        kind: EventKind,
        payload: CapturedWebhook | CapturedEvent | CapturedException,
        *,
        event_id: str | None = None,
        occurred_at: datetime | None,
    ) -> DeliveryResult:
        envelope = EventEnvelope(
            id=event_id or str(uuid4()),
            kind=kind,
            occurred_at=occurred_at or utc_now(),
            project_id=self.project_id,
            payload=payload,
        )
        event_id = envelope.id
        body = json.dumps(
            envelope.to_dict(), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        request = Request(
            self._events_url(),
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"watchwebhook-python-sdk/{SDK_VERSION}",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                status_code = response.status
                response.read()
        except HTTPError as error:
            status_code = error.code
            error.close()
            result = DeliveryResult(
                event_id=event_id,
                delivered=False,
                status_code=status_code,
                error=f"server returned HTTP {status_code}",
            )
            return self._handle_delivery_failure(result)
        except (OSError, URLError, TimeoutError) as error:
            result = DeliveryResult(
                event_id=event_id,
                delivered=False,
                error=f"{type(error).__name__}: {error}",
            )
            return self._handle_delivery_failure(result)

        if not 200 <= status_code < 300:
            result = DeliveryResult(
                event_id=event_id,
                delivered=False,
                status_code=status_code,
                error=f"server returned HTTP {status_code}",
            )
            return self._handle_delivery_failure(result)
        return DeliveryResult(event_id=event_id, delivered=True, status_code=status_code)

    def _handle_delivery_failure(self, result: DeliveryResult) -> DeliveryResult:
        if not self.fail_silently:
            raise DeliveryError(result.error or "event delivery failed", result=result)
        return result

    def _events_url(self) -> str:
        return f"{self.base_url}/v1/events"

    def _headers(self, headers: Mapping[str, Any] | None) -> dict[str, str]:
        return {
            str(key): _REDACTED if str(key).lower() in self._sensitive_headers else str(value)
            for key, value in (headers or {}).items()
        }

    def _exception_payload(
        self,
        exception: BaseException,
        traceback_value: TracebackType | None = None,
    ) -> dict[str, Any]:
        traceback_value = traceback_value or exception.__traceback__
        return {
            "exception_type": type(exception).__qualname__,
            "exception_module": type(exception).__module__,
            "message": str(exception),
            "traceback": self._format_traceback(
                type(exception), exception, traceback_value
            ),
        }

    @staticmethod
    def _format_traceback(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback_value: TracebackType | None,
    ) -> str:
        if traceback_value is None:
            return "".join(
                traceback_module.format_exception_only(exception_type, exception)
            ).strip()
        return "".join(
            traceback_module.format_exception(exception_type, exception, traceback_value)
        ).strip()
