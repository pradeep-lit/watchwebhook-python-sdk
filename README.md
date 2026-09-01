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
from flask import Flask, request

from watchwebhook import WatchWebhook

app = Flask(__name__)

watchwebhook = WatchWebhook(
    api_key="whk_live_...",
    project_id="orders-api",
)


@app.post("/webhooks/stripe")
def receive_stripe_webhook():
    # Read the body once. Flask caches it, so both WatchWebhook and your
    # business code receive the same bytes.
    body = request.get_data(cache=True)

    # This only creates a local lifecycle handle. No API request is sent yet.
    event = watchwebhook.start_webhook(
        source="stripe",
        method=request.method,
        url=request.url,
        headers=request.headers,
        query=request.args,
        body=body,
    )

    try:
        # These are functions from your application. Verify the provider's
        # signature while parsing before trusting or processing the payload.
        stripe_event = parse_and_verify_stripe_event(
            body=body,
            signature=request.headers.get("Stripe-Signature"),
        )
        process_payment_event(stripe_event)
    except Exception as exc:
        # Sends the full body, query, redacted headers, traceback,
        # received_at, and time_took_ms. Re-raise so Flask keeps your normal
        # error handling and Stripe can retry the webhook.
        event.fail(error=exc, status_code=500)
        raise

    # Sends only the reduced success payload plus received_at and time_took_ms.
    event.success(status_code=200)
    return {"received": True}, 200
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
- `received_at` is the UTC time captured by `start_webhook()`.
  `time_took_ms` is the elapsed business-processing time until `success()` or
  `fail()` is called. Duration uses Python's monotonic clock, so system clock
  corrections cannot make it jump backward.
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
