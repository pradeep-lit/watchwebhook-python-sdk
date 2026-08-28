# WatchWebhook Python SDK

The SDK captures inbound webhook requests, application-defined events, and
exceptions from a Python backend, then sends each item to a WatchWebhook
backend. It is framework-neutral: adapters for Flask, Django, FastAPI, or
another framework can convert their request objects into ordinary Python
values without adding framework dependencies to this package.

## Installation

```bash
pip install watchwebhook-python-sdk
```

## Quick start

```python
from watchwebhook import WatchWebhook

watchwebhook = WatchWebhook(
    api_key="whk_live_...",
    project_id="orders-api",
)

# The SDK records the request but does not consume it or run business logic.
event = watchwebhook.start_webhook(
    source="stripe",
    method=request.method,
    url=request.url,
    headers=request.headers,
    query=request.args,
    body=request.get_data(),
)

try:
    body = request.get_data()
    parsed = parse_stripe_event(body)
    process_order(parsed)

    # One terminal result is required for each started webhook.
    event.success(status_code=200)
except Exception as exc:
    event.fail(error=exc, status_code=500)
    raise

watchwebhook.capture_event(
    "order.fulfilled",
    {"order_id": order.id, "amount": order.total},
)
```

For a self-hosted backend, change only the endpoint:

```python
watchwebhook = WatchWebhook(
    api_key="whk_local_...",
    project_id="orders-api",
    base_url="http://localhost:8000",
)
```

## API behavior

- Events are sent synchronously as `POST /v1/events` JSON requests.
- `start_webhook()` is local setup only. It returns a handle that must be
  completed with exactly one `event.success()` or `event.fail(...)`. The
  terminal call sends the webhook record, including the application's result.
  The original request body remains available for business processing.
- Successful webhook records include only routing and outcome fields
  (`method`, `url`, `source`, metadata, and status). Failed records include
  the full captured body, query, redacted headers, and error details for
  debugging. This limits routine successful traffic while preserving failure
  diagnostics.
- Each envelope contains a UUID, UTC occurrence time, project ID, event kind,
  and schema version (`"1"`). This gives the future multi-tenant backend a
  stable routing and evolution boundary.
- Authentication uses `Authorization: Bearer <api_key>`. The project ID is a
  separate envelope field rather than caller-controlled payload metadata.
- `Authorization`, `Cookie`, `Set-Cookie`, and proxy authorization headers are
  replaced with `[REDACTED]` before transmission.
- Bytes that are valid UTF-8 are sent as text. Other bytes are base64 encoded.
  Other Python values are converted to JSON-safe values; unknown objects become
  strings instead of making observability break the application.
- Delivery uses a five-second timeout and no implicit retries or background
  worker. This keeps lifecycle and duplicate-event semantics explicit.
- Delivery failures return `DeliveryResult(delivered=False)` by default, so
  monitoring cannot replace an application's original exception. Set
  `fail_silently=False` to raise `DeliveryError` when delivery must be part of
  the caller's error handling.

The initial package intentionally does not include framework middleware,
queues, retries, or global configuration. Those choices affect request
lifecycle and deployment behavior, so they should be added with a concrete
integration contract rather than hidden in the core client.
