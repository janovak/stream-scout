# Independent Plan Critique: Batched Poller Datastore I/O

**Date**: 2026-08-30
**Method**: Independent read-only rubber-duck review
**Verdict after remediation**: **GO**

## Review Scope

The reviewer independently compared:

- `spec.md` and both completed requirements checklists;
- `plan.md`, `research.md`, `data-model.md`,
  `contracts/poller-batch-contract.md`, and `quickstart.md`;
- the project constitution;
- the current poller, desired-set store, reconciler, test adapters, and
  stream-monitoring test suite;
- the current operations ramp record and spec-004 post-merge ramp findings;
- pinned psycopg2/redis behavior, production Compose values, Dockerfile
  boundaries, and generated agent context.

The review explicitly challenged SQL paging, Redis consistency/error
semantics, duplicate handling, lifecycle ordering, failure gates, empty and
poison batches, connection reuse, operation counting, performance fixtures,
reconciler sampling, cold-start backoff, production configuration freeze, and
scope exclusions.

## Findings and Dispositions

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| C-001 | Blocking | No blocking design, coverage, feasibility, or scope issue was found. | No change required. |
| C-002 | Non-blocking | Auto-generated `CLAUDE.md` named nonexistent root `src/`/`tests/` paths and a ruff command not present in the dev requirements. | **Accepted.** Replaced with the real `services/stream-monitoring` layout and existing venv/pytest command. |
| C-003 | Non-blocking | The draft named `test_support.py` but did not specify the fake Redis surface needed to model `MGET`, non-transactional refresh, element-level response errors, and unknown acknowledgements. | **Accepted.** Added an explicit test-adapter contract to `plan.md` and `poller-batch-contract.md`. |
| C-004 | Suggestion | The two `%s` placeholder levels in `execute_values` could be confused. | **Accepted.** Added the exact outer `VALUES %s`, per-row `template="(%s, %s, NOW())"`, two-tuple row, and `page_size=len(rows)` shape. |
| C-005 | Stylistic | Feature-prefixed validation driver names differ from the generic names already under `phase5/`. | **No change.** Prefixing avoids collision with spec-004's existing `phase5/driver.py`; these files remain validation-only. |

## Confirmed Strengths

The review confirmed that the design:

1. Counts `cursor.execute()` calls inside `execute_values`, so its default
   100-row paging cannot hide five statements at 500 rows or nine at 900.
2. Uses one `MGET`, not a non-transactional read pipeline, for a consistent
   pre-refresh current-plus-departed snapshot.
3. Separates non-fatal metadata availability from fatal online-state and
   desired-publication gates without reporting stale metadata as success.
4. Preserves lifecycle meaning, real offline broadcaster IDs, atomic desired
   publication, and recovery from partial/unknown refresh outcomes.
5. Defines exact-eligible calibrated fixtures, direct in-process reconciler
   pass timestamps, scheduler overlap evidence, and unchanged-policy cold
   backoff validation.
6. Keeps production 150/300/120 configuration and every dependency, schema,
   scheduler, EventSub, Flink, and feature-005 boundary unchanged.

## Final Assessment

After applying C-002 through C-004, the Stage 2 artifacts are internally
consistent, implementable with the pinned clients, constitution-compliant,
and complete for the feature specification. No service code or later Spec Kit
stage is required to close the plan review.
