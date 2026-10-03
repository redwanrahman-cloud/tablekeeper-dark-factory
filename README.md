# Tablekeeper — Dark Factory

Tablekeeper is the Tablekeeper-track result produced by a three-seat BAND factory for the WeAreDevelopers × BAND AI Hackathon. The repository preserves the complete chronological implementation, review, rejection, correction, and acceptance trail.

## Start here

- [`FACTORY.md`](FACTORY.md) explains the reusable factory, seat ownership, measured time/cost, and recovery loop.
- [`mandates/`](mandates/) contains the generic mandate for each BAND seat.
- [`room.json`](room.json) is the unchanged full-session BAND export.
- `stage-1/` through `stage-4/` are complete, independently buildable services. Each stage carries the preceding behavior forward and adds only that stage's specification.
- Each stage's `RUN.md` is its operational and reproduction guide.

## Team and track

- **Track:** Tablekeeper
- **Seats:** Architect Coordinator, Implementer, Independent Verifier
- **Harness/model:** Codex / `gpt-6.1-sol` for all three seats
- **Final accepted revision:** `8b39e95c58cb37aaca12b0ee8986cf4fceef9595`

## What the factory delivered

The final service is a dependency-free Python HTTP application with a responsive browser UI. Across four stages it implements reservations, authentication, combined-table booking, immutable/effective-dated policies, booking history, recurring reservations, atomic seating repair, and recurring amendments. It preserves idempotency receipts and imports real snapshots from each prior stage.

The final independent run passed the organizer's unchanged Stage 1–4 harness **158/158** (120 + 25 + 7 + 6), plus **133/133** independent API groups, **46/46** independent desktop/mobile browser groups, and **3/3** populated prior-stage migrations. Exact commands and operating constraints are documented in [`stage-4/RUN.md`](stage-4/RUN.md).

## Run Stage 4

```sh
cd stage-4
docker build -t tablekeeper-stage4 .
docker run --rm --name tablekeeper-stage4 -e PORT=8080 -p 8080:8080 tablekeeper-stage4
```

Then open <http://localhost:8080/>. Read [`stage-4/RUN.md`](stage-4/RUN.md) before testing: a fresh process intentionally has empty in-memory state, and `POST /_test/reset` loads fixtures.

## Evidence notes

- History is chronological and unsquashed; the Stage 4 rejected candidate (`1c62b1a`) and its correction (`8b39e95`) remain visible.
- The service is intentionally single-process and ephemeral. Export/import transfers a consistent snapshot.
- Protected historical Stage 3 retains a disclosed `4.2306:1` small-label contrast finding. Stage 4 corrects it to `5.2358:1`; history was not rewritten.
- `room.json` is the full BAND download required by the event and includes the reciprocal seat-to-seat handoffs used by the eligibility check.

