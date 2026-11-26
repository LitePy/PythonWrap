"""Soak test: drive a resilience policy against a simulated flaky dependency for a while.

Runs many concurrent callers through a policy (retry + breaker + timeout + budget) while
the fake dependency cycles through healthy, degraded (errors and latency) and down
phases, then prints a JSON report: calls, successes, give-ups, breaker rejections,
retries per call, latency percentiles, peak memory and thread count. Use it to

* check a policy before a production pilot (are retries capped during an outage? does
  the breaker open and recover?), and
* catch leaks: run it for minutes with ``--duration`` and compare ``rss_growth_kb``.

    python benchmarks/soak.py                       # 20 s, 16 threads
    python benchmarks/soak.py --duration 600 --threads 64 --json > soak.json
    python benchmarks/soak.py --processes 4 --redis-url redis://localhost:6379/0

With ``--processes N --redis-url URL`` every process shares one Redis circuit breaker and
one Redis rate limiter (``--limit`` calls per second across all processes) and the report
adds up the processes. That mode needs the ``redis`` package.

Standard library only. Real time is used (not ``fake_clock``): this is a soak, not a test.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Any

try:
    import python_wrap  # noqa: F401
except ImportError:  # running from a checkout without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from python_wrap import CircuitBreaker, CircuitOpenError, RetryBudget, policy, retry, timeout
from python_wrap.errors import RateLimitExceeded
from python_wrap.retry import attempt_history


class FlakyDependency:
    """Cycles healthy -> degraded -> down -> healthy every ``period`` seconds."""

    def __init__(self, period: float, seed: int) -> None:
        self.period = period
        self.started = time.monotonic()
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.calls = 0

    def phase(self) -> str:
        position = ((time.monotonic() - self.started) / self.period) % 3
        return ("healthy", "degraded", "down")[int(position)]

    def __call__(self) -> str:
        with self.lock:
            self.calls += 1
            roll = self.rng.random()
        phase = self.phase()
        if phase == "down":
            raise ConnectionError("dependency down")
        if phase == "degraded":
            if roll < 0.3:
                raise ConnectionError("intermittent")
            time.sleep(0.002 + roll * 0.01)
        return "ok"


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q / 100 * len(ordered)))]


def run(
    duration: float,
    threads: int,
    period: float,
    seed: int,
    redis_url: str | None = None,
    limit: int = 1000,
    run_id: str = "local",
) -> dict[str, Any]:
    dependency = FlakyDependency(period, seed)
    budget = RetryBudget(ratio=0.2, min_per_second=5)
    layers: dict[str, Any] = {}
    backend = None
    if redis_url is not None:
        import redis

        from python_wrap.concurrency import RateLimiter
        from python_wrap.quotas import RedisWindow
        from python_wrap.shared import RedisBreakerState

        client = redis.Redis.from_url(redis_url)
        backend = RedisBreakerState(client, prefix=f"python-wrap:soak:{run_id}:")
        window = RedisWindow(client, f"python-wrap:soak:{run_id}:limit", calls=limit, period=1)
        layers["limiter"] = RateLimiter(calls=limit, period=1, backend=window)
    breaker = CircuitBreaker(
        failure_threshold=10, recovery_timeout=1.0, name="soak", backend=backend
    )
    protected = policy(
        name="soak",
        breaker=breaker,
        retry=retry(max_attempts=3, delay=0.01, max_delay=0.05, on=ConnectionError, budget=budget),
        timeout=timeout(0.5, use_signals=False),
        **layers,
    )(dependency)

    stats = {"calls": 0, "ok": 0, "gave_up": 0, "rejected": 0, "limited": 0, "attempts": 0}
    latencies: list[float] = []
    lock = threading.Lock()
    stop = time.monotonic() + duration

    def worker() -> None:
        while time.monotonic() < stop:
            began = time.perf_counter()
            outcome, attempts = "ok", 1
            try:
                protected()
            except CircuitOpenError:
                outcome, attempts = "rejected", 0
            except RateLimitExceeded:
                outcome, attempts = "limited", 0
            except ConnectionError as exc:
                outcome, attempts = "gave_up", len(attempt_history(exc)) or 1
            elapsed = time.perf_counter() - began
            with lock:
                stats["calls"] += 1
                stats[outcome] += 1
                stats["attempts"] += attempts
                if len(latencies) < 200_000:
                    latencies.append(elapsed)
            if outcome in ("rejected", "limited"):
                time.sleep(0.005)

    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    pool = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    calls = max(stats["calls"], 1)
    return {
        "duration_s": duration,
        "threads": threads,
        **stats,
        "dependency_calls": dependency.calls,
        "amplification": round(dependency.calls / calls, 3),
        "retry_budget": vars(budget.stats) if hasattr(budget.stats, "__dict__") else None,
        "breaker": {"state": breaker.state.value, **_breaker_stats(breaker)},
        "latency_ms": {
            "p50": round(percentile(latencies, 50) * 1000, 3),
            "p95": round(percentile(latencies, 95) * 1000, 3),
            "p99": round(percentile(latencies, 99) * 1000, 3),
        },
        "traced_memory_growth_kb": round((current - baseline) / 1024, 1),
        "traced_memory_peak_kb": round(peak / 1024, 1),
        "threads_alive_after": threading.active_count(),
    }


def _breaker_stats(breaker: CircuitBreaker) -> dict[str, Any]:
    stats = breaker.stats
    return {"failures": stats.failures, "successes": stats.successes, "rejected": stats.rejected}


def _run_one(options: dict[str, Any]) -> dict[str, Any]:
    return run(**options)


def run_processes(processes: int, redis_url: str, limit: int, **options: Any) -> dict[str, Any]:
    """Run the soak in ``processes`` processes sharing one Redis breaker and limiter."""
    import multiprocessing
    import uuid

    run_id = uuid.uuid4().hex[:8]
    jobs = [
        {**options, "seed": options["seed"] + i, "redis_url": redis_url, "limit": limit,
         "run_id": run_id}
        for i in range(processes)
    ]  # fmt: skip
    with multiprocessing.get_context("spawn").Pool(processes) as pool:
        reports = pool.map(_run_one, jobs)
    total = {
        key: sum(r[key] for r in reports)
        for key in ("calls", "ok", "gave_up", "rejected", "limited", "attempts", "dependency_calls")
    }
    calls = max(total["calls"], 1)
    return {
        "processes": processes,
        **total,
        "amplification": round(total["dependency_calls"] / calls, 3),
        "admitted_per_second": round((calls - total["limited"]) / options["duration"], 1),
        "limit_per_second": limit,
        "breaker_states": [r["breaker"]["state"] for r in reports],
        "traced_memory_growth_kb": [r["traced_memory_growth_kb"] for r in reports],
        "latency_p99_ms": max(r["latency_ms"]["p99"] for r in reports),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--duration", type=float, default=20.0, help="seconds to run")
    parser.add_argument("--threads", type=int, default=16, help="concurrent callers")
    parser.add_argument("--period", type=float, default=2.0, help="seconds per phase")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="print only the JSON report")
    parser.add_argument("--processes", type=int, default=1, help="processes (needs --redis-url)")
    parser.add_argument("--redis-url", default=None, help="share breaker and limiter in Redis")
    parser.add_argument("--limit", type=int, default=1000, help="shared calls per second")
    args = parser.parse_args(argv)
    if args.processes > 1:
        if not args.redis_url:
            parser.error("--processes needs --redis-url")
        report = run_processes(
            args.processes, args.redis_url, args.limit,
            duration=args.duration, threads=args.threads, period=args.period, seed=args.seed,
        )  # fmt: skip
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["amplification"] <= 2.0 else 1
    report = run(args.duration, args.threads, args.period, args.seed, args.redis_url, args.limit)
    if not args.json:
        print(f"soak: {args.duration:g}s, {args.threads} threads, phase {args.period:g}s")
    print(json.dumps(report, indent=2, default=str))
    # Retries must stay bounded during the outage phases: with a 20 % budget and 3
    # attempts the dependency should never see more than ~2x the logical calls.
    return 0 if report["amplification"] <= 2.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
