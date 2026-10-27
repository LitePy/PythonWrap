# Security policy

python-wrap is a library of decorators that you wrap around your own code. It has no
runtime dependencies, opens no sockets and reads no files, but a defect in a decorator
such as `timeout`, `rate_limit` or `validate_args` can still weaken the code around it.
Reports are taken seriously and answered.

## Reporting a vulnerability

**Do not open a public issue.**

Report privately by either route:

- **Preferred:** [open a private advisory on GitHub](https://github.com/LitePy/PythonWrap/security/advisories/new).
- **Email:** <pythondeveloper6@gmail.com>, with `python-wrap security` in the subject.

Please include the version of python-wrap and Python, what an attacker gains, and the
smallest reproduction you can manage.

## What to expect

| | |
| --- | --- |
| Acknowledgement | within **3 working days** |
| Initial assessment | within **7 days** |
| Fix or a dated plan | within **30 days** for high and critical, best effort otherwise |
| Public disclosure | after a fix ships, coordinated with you |

This is a small project maintained by volunteers. If you have not heard back in a week,
please chase: it means the message was missed, not ignored. You will be credited in the
advisory and changelog if you wish. There is no bug bounty.

## Supported versions

Security fixes are released as a new patch of the latest minor version. Older minors are
not backported.

| Version | Supported |
| --- | --- |
| 0.2.x | yes |
| 0.1.x | no |

Supported Python versions: 3.10 to 3.14.

## Scope

In scope: a decorator that fails to enforce what its documentation promises in a way
that has security impact (a rate limit or circuit breaker that can be bypassed, a cache
that leaks results between callers that should not share them, a validation decorator
that accepts what it documents as rejected, a denial of service from unbounded memory
growth in a documented-bounded structure).

Out of scope: behaviour documented as such, and findings from an automated scanner with
no demonstrated impact.
