# Correctness

Keep bad data out and keep objects consistent when something goes wrong halfway.

<div class="grid cards" markdown>

-   :material-shield-check-outline:{ .lg .middle } **[Validation](validation.md)**

    ---

    `validate_args`, `check_type` and `enforce_types`: reject bad input with an error that names the parameter.

-   :material-undo-variant:{ .lg .middle } **[State safety](state.md)**

    ---

    `revert_on_failure`, `Transaction` and `transactional`: undo changes when a call fails.

</div>

## Which one?

| You want to | Use |
| --- | --- |
| Check a business rule (`amount > 0`, not empty) | [`validate_args`](validation.md#validate_args) |
| Check that an argument is an `int` or `str` | [`check_type`](validation.md#check_type) |
| Make your type hints true at runtime | [`enforce_types`](validation.md#enforce_types) |
| Restore an object's attributes if a method fails | [`revert_on_failure`](state.md#revert_on_failure) |
| Undo side effects (files, payments) when a step fails | [`transactional`](state.md#transactional) |

## See also

- [Stacking decorators](../../get-started/stacking.md)
- [Errors](../../get-started/errors.md)

## Batch jobs

[Batch outcomes and checkpoints](batch-processing.md) record per-item failures and
support explicit resumption without promising exactly-once external writes.
