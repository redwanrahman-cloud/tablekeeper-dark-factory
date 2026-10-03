# Factory

## Purpose

This factory turns a supplied specification into an auditable delivery through three deliberately separated responsibilities: coordination, implementation, and independent verification. The mandates are problem-agnostic; another team can reuse them for a different track by supplying the new authoritative specification and repository paths.

## Seats and ownership

All seats ran in BAND with **Codex / `gpt-6.1-sol`**.

1. **Architect Coordinator** reads the complete requirements, splits ownership, sends self-contained handoffs, maintains the decision trail, and accepts only committed work with independent evidence.
2. **Implementer** owns one bounded change at a time, preserves unrelated history, implements specification behavior beyond visible checks, runs focused checks, and commits cohesive revisions.
3. **Independent Verifier** does not edit implementation. It reads the requirements independently, tests a clean committed revision through public behavior and observed state, and returns an explicit accepted/rejected/blocked disposition with reproduction evidence.

The reusable operating contract is in [`mandates/`](mandates/). Seat filenames match the BAND room identities.

## Operating loop

For each stage:

1. The coordinator reads the full specification and gives the implementer and verifier complete five-part handoffs: task, constraints, acceptance criteria, paths, and required evidence.
2. The implementer inspects the accepted predecessor, implements the stage in a new self-contained folder, exercises boundaries and failure paths, and commits without rewriting history.
3. The verifier starts from the exact commit, performs clean container and official-harness runs, then adds adversarial API/browser/migration checks not supplied by the organizer.
4. A rejection returns to the implementer with expected/actual behavior, governing requirement, severity, and reproduction steps. The implementer reproduces first, corrects the invariant, commits a successor, and requests a clean re-review.
5. The coordinator records the disposition, revision, commands, timing, measured usage, residual limitations, and only then advances the accepted baseline.

The human's stage dispatch is the only human input during a run. Decisions, corrections, and acceptance are exchanged among seats in the BAND room preserved as [`room.json`](room.json).

## Design choices and trade-offs

### Complete, immutable stage folders

Each `stage-N/` is independently buildable and runnable. Later work never mutates an earlier accepted folder. This makes progress and regressions judge-verifiable, at the cost of deliberate source duplication.

### Specification-first, checks-second

The coordinator and verifier read the authoritative specification and harness behavior before accepting work. Visible checks are treated as a floor, not the design. Independent suites target concurrency, retry identity, malformed input, boundary values, atomic state replacement, UI recovery, accessibility, and real prior-stage imports.

### Minimal runtime

The service uses Python's standard library, bundled HTML/CSS/JavaScript, in-memory state, and one process-wide lock. Runtime has no package installation, CDN, or external network dependency. The lock makes mutations and receipt commits serializable and easy to audit; the trade-off is a single-process ceiling rather than horizontal persistence.

### Evidence before claims

Every accepted stage names an exact commit and reproducible commands. The final verification used a detached clean clone, no-cache image builds, non-root UID/GID 65534, custom ports, outbound-disabled networking, separate test containers, and 2 CPU / 2 GiB limits. Failure artifacts were retained rather than overwritten.

## How the factory catches and recovers from bad work

The verifier rejected Stage 4 candidate `1c62b1a0aeea547fd22725609a3c6cd31819a653` after finding that a failed current-reservation read could cause an unchanged successful retry to display obsolete historical seating. The failure reproduced in four desktop/mobile abort/503 cases.

The implementer first reproduced the unchanged checker, then corrected the underlying authority rule in successor `8b39e95c58cb37aaca12b0ee8986cf4fceef9595`: confirmation now uses the current owner list as a fallback; if both authoritative reads fail, it keeps the confirmed reference and form/retry state but explicitly reports that current seating is unavailable. Historical receipt seating is never presented as current. The verifier then reran the full clean suite plus six dual-read recovery and four delayed-response selection checks before acceptance.

Earlier development failures—calendar edges, opaque identifiers, fixture membership, responsive long labels, selected-state contrast, migration expectations, occupied ports, and evidence-parser/report recovery—also remain in the chronological room and Git trails. The process fixes causes in successor commits; it never amends or squashes the rejected evidence away.

## Measured result, time, and model spend

Final Stage 4 acceptance at `8b39e95` recorded:

- Organizer harness: **158/158** across Stages 1–4.
- Independent API: **133/133 groups** / 3,532 requests.
- Independent browser: **46/46 groups** across desktop and mobile.
- Submitted API/browser/migration checks: **37/37**, **16/16**, **3/3**.
- Final clean execution: **207.726857 seconds**; organizer wrapper: **41.111 seconds**.
- Full human dispatch to recovered final report: **1 hour 58 minutes 2.921 seconds**, including transport and report recovery.

The final BAND usage capture reported catalog-estimated equivalents (not provider billing):

- Architect Coordinator: **$14.8244888**
- Implementer: **$12.6270968**
- Independent Verifier: **$14.0882740**
- Total cumulative observed: **$41.5398596**

The late Stage 4 interval delta was **$12.2349232**, but it omits initial dispatch/recovery and may overlap earlier-stage review, so it is not represented as an exact Stage 4-only price. Earlier stage captures in the room likewise state their timestamp and attribution limits.

## Reuse

1. Create one BAND seat for each mandate and keep the displayed seat names aligned with mandate filenames.
2. Give the coordinator the new authoritative specification, repository, evaluation commands, and evidence destination.
3. Let the coordinator dispatch complete handoffs; do not steer the run after dispatch.
4. Require chronological commits and clean independent dispositions at every milestone.
5. Preserve failures, corrections, exact commands, and measured limits.
6. Download the full BAND session at the end and run the organizer's offline eligibility check from a fresh clone.

This separation works because no seat can both author and independently accept the same implementation, while the coordinator cannot advance on an uncommitted or unverified claim.

## Known limitations

- State is intentionally ephemeral and single-process; export/import is the durability boundary.
- Seating optimization is bounded to the specification's six tables, four declared pairs, and six considered bookings.
- Coverage is finite for arbitrary thread/network interleavings, string/resource sizes, and every browser/font/assistive-technology combination.
- Historical Stage 3 has the disclosed `4.2306:1` enabled 11 px policy-label contrast result; Stage 4 passes the same probe at `5.2358:1`. History was intentionally preserved.
- BAND's catalog estimates are not invoices, and per-stage cost isolation is imperfect where activities overlap.
