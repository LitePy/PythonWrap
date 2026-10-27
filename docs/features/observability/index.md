# Observability

Find out how long calls take, how much they allocate, what they were called with, who called them, and how often they fail. python-wrap has no dependencies for any of this and never configures logging for you.

<div class="grid cards" markdown>

-   :material-text-box-outline:{ .lg .middle } **[Logging](logging.md)**

    ---

    Configure logging, then use `log_args`, `log_return`, `trace` and `log_context` to see what was called.

-   :material-clipboard-text-clock-outline:{ .lg .middle } **[Audit](audit.md)**

    ---

    Structured records of who called what, with which arguments, and how it ended.

-   :material-timer-outline:{ .lg .middle } **[Timing](timing.md)**

    ---

    `timing`, `benchmark`, `measure_memory` and `profile`.

-   :material-chart-line:{ .lg .middle } **[Metrics and events](metrics.md)**

    ---

    `count_calls` statistics, plus the event stream you can forward to Prometheus, OpenTelemetry or statsd.

</div>

## Which one?

| Question | Use |
| --- | --- |
| What was this called with, and what did it return? | [Logging](logging.md) (`log_args`, `log_return`) |
| Who is calling this function? | [Logging](logging.md#trace) (`trace`) |
| Who did what, for compliance or an audit trail? | [Audit](audit.md) |
| How slow is it? How much memory does it allocate? | [Timing](timing.md) |
| How often is it called, what is the error rate, what is p95? | [Metrics](metrics.md) (`count_calls`) |
| Feed retries, cache hits and circuit changes to Prometheus or OpenTelemetry | [Events](metrics.md#events) |

## See also

- [Project-wide configuration](../configuration.md) for setting redaction names once.
- [API reference](../../api/logs.md)
