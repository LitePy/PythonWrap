# Circuit breaker alerts

Tell a chat channel when a dependency trips its breaker, without adding network calls to the request path.
`WebhookAlerter` posts to a Slack, Microsoft Teams or generic JSON webhook.

```python
from python_wrap import CircuitBreaker
from python_wrap.alerts import WebhookAlerter

sent = []
alerter = WebhookAlerter(
    "https://chat.example.com/hooks/abc",
    format="teams",
    sender=lambda url, body, headers: sent.append(body),  # tests; omit in production
    context={"service": "billing", "env": "prod"},
)
breaker = CircuitBreaker(failure_threshold=1, name="payments", on_state_change=alerter)
breaker.force_open()

assert alerter.flush(2)
assert len(sent) == 1
```

## Wiring

- As a listener: `CircuitBreaker(..., on_state_change=alerter)` alerts for that breaker.
- Process-wide: `alerter.install()` subscribes to every `circuit.state_changed` event (including shared-backend breakers) and returns the alerter. `alerter.close()` unsubscribes.

Pick one per breaker; using both sends the same transition twice (the cooldown hides the duplicate alert, but not a recovery message).

## Behaviour

| Option | Default | Meaning |
| --- | --- | --- |
| `format` | `"json"` | `"json"` plain dict, `"slack"` `{"text": ...}`, `"teams"` Adaptive Card message |
| `on` | `("open",)` | States that trigger an alert |
| `cooldown` | `300` | Seconds between alerts for the same breaker |
| `include_recovery` | `True` | Send "closed again" after an alert was sent for that breaker |
| `timeout` | `5` | Network timeout of the built-in sender |
| `context` | `None` | Static key/values (service, environment) added to each message |

Delivery runs on a daemon thread, so the call that tripped the breaker never waits. A failed delivery is logged
(with only the host of the URL and the exception type) and never raised. `flush(timeout)` waits for queued sends;
use it in tests and before a short-lived process exits.

## What is sent

Only the breaker name, state, a UTC timestamp and your `context`. Exception messages, URLs and credentials are never
included, since failures often carry connection strings or tokens. Keep secrets out of `context` as well.
