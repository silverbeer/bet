# Migrations

Forward-only, numbered SQL applied in order by `bet.database.migrator`.

    NNNN_lower_snake_case.sql

Rules the harness enforces, so they are not conventions to remember:

- A file that does not match the naming pattern is an error, not something
  skipped. A migration silently ignored because of a typo is a schema change
  that never happened.
- Two files may not share a version number.
- **An applied migration must never be edited.** The runner stores a checksum
  and refuses to run when a file no longer matches what was applied — at that
  point the database no longer matches the code that claims to have built it.
  Write a new forward migration instead.
- A new migration numbered *below* the highest applied version is refused,
  because a forward-only runner would skip it forever.

There is no down-migration. Reversing a schema change is a new forward
migration: an automated rollback against a warehouse of irreplaceable financial
history is a way to lose it.

`control.migration` itself is created by the runner, not by a migration here —
it has to exist before the runner can tell what has been applied.
