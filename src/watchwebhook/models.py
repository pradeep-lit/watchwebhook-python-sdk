from __future__ import annotations

import base64
import math
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID

EventKind = Literal["webhook", "event", "exception"]
WebhookOutcome = Literal["pending", "success", "failure"]

_MAX_JSON_DEPTH = 20


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def format_datetime(value: datetime) -> str:
    """Format a datetime as ISO 8601, using ``Z`` for UTC."""
    rendered = value.isoformat()
    if rendered.endswith("+00:00"):
        return f"{rendered[:-6]}Z"
    return rendered


def to_json_safe(value: Any, *, _seen: set[int] | None = None, _depth: int = 0) -> Any:
    """Convert common Python values into data accepted by strict JSON encoders.

    Unknown objects become strings. Cycles and very deeply nested values are
    replaced with descriptive markers rather than breaking event delivery.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return to_json_safe(value.value, _seen=_seen, _depth=_depth)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "encoding": "base64",
            "data": base64.b64encode(bytes(value)).decode("ascii"),
        }

    if _depth >= _MAX_JSON_DEPTH:
        return "<maximum depth reached>"

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "<circular reference>"

    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            return {
                str(key): to_json_safe(item, _seen=seen, _depth=_depth + 1)
                for key, item in value.items()
            }
        if is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: to_json_safe(
                    getattr(value, field.name), _seen=seen, _depth=_depth + 1
                )
                for field in fields(value)
            }
        if isinstance(value, Set):
            ordered = sorted(value, key=lambda item: (type(item).__qualname__, repr(item)))
            return [
                to_json_safe(item, _seen=seen, _depth=_depth + 1)
                for item in ordered
            ]
        if isinstance(value, Sequence):
            return [
                to_json_safe(item, _seen=seen, _depth=_depth + 1)
                for item in value
            ]
        try:
            return str(value)
        except Exception:
            return f"<unserializable {type(value).__qualname__}>"
    finally:
        seen.remove(identity)


def encode_webhook_body(body: Any) -> tuple[Any, str | None]:
    """Return a JSON-safe body and the representation used for it."""
    if body is None:
        return None, None
    if isinstance(body, (bytes, bytearray, memoryview)):
        raw = bytes(body)
        try:
            return raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            return base64.b64encode(raw).decode("ascii"), "base64"
    if isinstance(body, str):
        return body, "utf-8"
    return to_json_safe(body), "json"


@dataclass(frozen=True, slots=True)
class CapturedWebhook:
    method: str
    url: str | None
    headers: dict[str, str]
    query: dict[str, Any]
    body: Any
    body_encoding: str | None
    source: str | None
    metadata: dict[str, Any]
    received_at: datetime
    time_took_ms: float | None
    outcome: WebhookOutcome
    status_code: int | None
    error: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method": self.method,
            "url": self.url,
            "source": self.source,
            "metadata": self.metadata,
            "outcome": self.outcome,
            "status_code": self.status_code,
            "received_at": format_datetime(self.received_at),
            "time_took_ms": self.time_took_ms,
        }
        if self.outcome == "failure":
            payload.update(
                {
                    "headers": self.headers,
                    "query": self.query,
                    "body": self.body,
                    "body_encoding": self.body_encoding,
                    "error": self.error,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class CapturedEvent:
    name: str
    data: Any
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "data": self.data, "metadata": self.metadata}


@dataclass(frozen=True, slots=True)
class CapturedException:
    exception_type: str
    exception_module: str
    message: str
    traceback: str
    context: dict[str, Any]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "exception_type": self.exception_type,
            "exception_module": self.exception_module,
            "message": self.message,
            "traceback": self.traceback,
            "context": self.context,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    id: str
    kind: EventKind
    occurred_at: datetime
    project_id: str
    payload: CapturedWebhook | CapturedEvent | CapturedException
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind,
            "occurred_at": format_datetime(self.occurred_at),
            "project_id": self.project_id,
            "payload": self.payload.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    event_id: str
    delivered: bool
    status_code: int | None = None
    error: str | None = None
