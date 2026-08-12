# Agent Notes

Compressed, reusable learnings from building slices in this project. Slice-specific
detail belongs in `progress.txt`, not here.

- **Unrelated git histories.** If a target branch (e.g. `main`) shares no
  merge-base with the branch actually tracking the live board state, do not
  force it to fast-forward — that silently discards whatever history it held.
  Merge feature branches into the branch with the live lineage instead, and
  surface the `main` question to a human rather than resolving it unilaterally.
- **Lock files must be regenerated as a full batch, even the first time.**
  Running `hatch -e <env> run ...` ad hoc during development creates only the
  lock files for the envs you happened to invoke, leaving the rest missing or
  stale against the shared `pip-compile-constraint`. Before committing, run the
  full `rm -rf requirements* && hatch env prune && hatch env show --json | jq
  -r 'keys[] | select(startswith("hatch-") | not)' | xargs -I{} sh -c 'hatch
  env create "{}"'` sequence once, so every environment's lock file is
  consistent, then re-run the scoped tests against the regenerated envs.
- **`from __future__ import annotations` + `TYPE_CHECKING` guard is the fix for
  most `TCxxx` ruff findings** on imports used only in a return-type or local
  annotation. The two exceptions in `CLAUDE.md`'s FastAPI/Pydantic gotchas
  (`Request` in a dependency factory, and any type that appears as a Pydantic
  field) must stay as real runtime imports with an inline `# noqa` instead —
  moving them under `TYPE_CHECKING` breaks FastAPI's runtime introspection.
- **A long dotted import path across several package levels overflows the
  88-char line limit even when wrapped in parens** (`from a.b.c.d.e.routes
  import (`). Import the parent module and reference the attribute
  (`from a.b.c.d import routes as x_routes`, then `x_routes.router`) instead of
  fighting the wrap.
- **Not every read model has a "found"/404 concept.** Per the `build-state-view`
  skill's read-model complexity guide, a count/aggregate view over a single
  entity is always well-defined at zero (no prior events → count 0), so it
  should return 200, not 404 — check the specifications' `given: []` scenarios
  before defaulting to the presence-check template.
