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
- **DCB "given" events for a spec only need to exist as `Decision` classes**,
  not as fully-built commands from other slices — GWT/integration seeding uses
  raw `TaggedEvent`s constructed directly, bypassing whatever slice would
  normally emit them. A slice whose specs reference not-yet-built upstream
  commands is not actually blocked by them.
- **A two-entity consistency boundary needs a `Sequence[Selector]`, not one
  `Selector` with both tags**, whenever one invariant only depends on one of
  the two entities (e.g. "does the course exist" vs. "is this student
  subscribed to it"). A single dual-tag selector would only ever see events
  tagged with *both*, silently missing single-entity events like the other
  entity's creation. The emitted event still carries every tag from every
  selector, so each one independently sees it.
- **GWT's `given().when()` rejects any given event outside the slice's own
  consistency boundary** (`AssertionError: Consistency boundary wouldn't have
  selected: ...`) — this includes a spec scenario that arranges a *different*
  entity's history to prove isolation (e.g. another student's subscription to
  the same course). That case has to move to the integration suite, seeded via
  `app.events.append`; write the acceptance test for the same invariant using
  only boundary-compliant history instead.
- **A single `Selector` is enough for a one-entity invariant that must
  aggregate across a many-relationship** (e.g. "reject if new capacity <
  current subscription count" on a course with many student subscriptions).
  `Selector` matches by tag *intersection*, not equality, so
  `Selector(types=[...several event types...], tags=[f"course:{course_id}"])`
  still selects `StudentSubscribed`/`StudentUnsubscribed` events tagged
  `[course:X, student:Y]` — the extra `student:Y` tag doesn't exclude them.
  Reach for `Sequence[Selector]` only when the invariant genuinely spans two
  *independent* entities (see the two-entity boundary note above), not
  whenever a second tag merely happens to be present on some events.
- **`Selector(tags=[])` means "every event of these types, everywhere", not
  "matches nothing".** Both `eventsourcing.dcb.gwt.selector_matches` and the
  real recorder's tag rule are "all the item tags are in the event tags" —
  vacuously true for an empty list. This is the right (and only) boundary for a
  genuinely global, multi-entity read model (e.g. a catalogue listing every
  entity of a kind) — model it as `self.entries: dict[str, EntryState] = {}`
  keyed by entity id, and justify `tags=[]` in the projection's docstring per
  `.build-kit/CLAUDE.md`'s own note on this case. Don't confuse this with the
  *write*-side warning in `CLAUDE.md` about an empty selector as an *append
  condition* (which does mean "fail if anything exists") — that's a different
  operation (`append`'s optimistic-concurrency check) from a *read*-side
  `consistency_boundary()` filter.
- **A slice folder can collide when two board slices share the same title.**
  `load-slice` derives the on-disk folder name from the slice title, so two
  board slices both titled e.g. "Course Catalogue" (one live, one a stale
  Blocked/duplicate column) land in the *same* folder — and whichever was
  written last wins in `<folder>/slice.json`, silently holding the wrong
  slice's full definition even though `index.json`'s per-slice inline
  `definition` stays correct for both. This is a different failure mode from
  the now-familiar "slice.json truncated to a bare stub" corruption — it's a
  content *mismatch* (right shape, wrong slice id), so `git checkout --` won't
  fix it; check `<folder>/slice.json`'s own `"id"` field against the specific
  slice id you picked from `index.json` before trusting it, and if they
  disagree, rewrite the file from `index.json`'s inline `definition` for the
  id you actually picked.
