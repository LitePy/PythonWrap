"""Strict runtime validation and the multi-process doctor check.

``enforce_types`` checks ``TypedDict`` arguments (required keys present, each present key
the right type). By default an annotation it cannot check is accepted; ``strict=True``
raises ``UnsupportedAnnotationError`` for it instead. The doctor's ``multiprocess=True``
mode flags per-process limits (PW012) that every worker would enforce separately.

Run: python examples/strict_validation.py
"""

from __future__ import annotations

from typing import Protocol, TypedDict

from python_wrap import ArgumentTypeError, CircuitBreaker, enforce_types, policy, timeout
from python_wrap.doctor import explain
from python_wrap.validation import UnsupportedAnnotationError


class Movie(TypedDict):
    title: str
    year: int


class Greeter(Protocol):  # not @runtime_checkable: cannot be checked at runtime
    def greet(self) -> str: ...


@enforce_types(strict=True)
def title_of(movie: Movie) -> str:
    return movie["title"]


def main() -> None:
    assert title_of({"title": "Up", "year": 2009}) == "Up"
    for bad in ({"title": "Up"}, {"title": "Up", "year": "2009"}):
        try:
            title_of(bad)  # type: ignore[typeddict-item]
        except ArgumentTypeError as exc:
            print(f"rejected: {exc}")
        else:
            raise AssertionError(f"{bad} should have been rejected")

    # Default mode accepts what it cannot check; strict mode refuses at decoration time.
    @enforce_types
    def lenient(who: Greeter) -> str:
        return who.greet()

    assert callable(lenient)
    try:

        @enforce_types(strict=True)
        def strict(who: Greeter) -> str:
            return who.greet()

    except UnsupportedAnnotationError as exc:
        assert exc.parameter == "who"
        print(f"strict refused: {exc}")
    else:
        raise AssertionError("expected UnsupportedAnnotationError")

    # Doctor: a breaker with process-local state under a process pool.
    stack = policy(breaker=CircuitBreaker(name="vendor"), timeout=timeout(2))
    assert "PW012" not in [finding.code for finding in explain(stack).findings]
    report = explain(stack, multiprocess=True)
    assert [finding.code for finding in report.findings] == ["PW012"]
    print(report)


if __name__ == "__main__":
    main()
