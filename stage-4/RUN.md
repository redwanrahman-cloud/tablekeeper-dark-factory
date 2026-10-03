# Stage 4 seating repairs and recurring amendments

From this directory, build and start with no additional setup:

```sh
docker build -t tablekeeper-stage4 . && docker run --rm --name tablekeeper-stage4 -e PORT=8080 -p 8080:8080 tablekeeper-stage4
```

The service listens on `0.0.0.0`, defaults to port 8080, and becomes ready at
`GET /health`. A fresh process has empty state. Load restaurant/user fixtures with
`POST /_test/reset`. Runtime has no external dependencies or network calls.
State is in memory and is intentionally lost at restart. Export/import can transfer
a consistent snapshot, including sessions and retry receipts; treat exports as private.

Open `http://localhost:8080/` to browse and book, `/signup` or `/login` to sign in,
and `/lookup` to find or cancel a booking, view accepted terms/history, change its
date/seating/party size, or adopt it as recurring visits. The fresh process has no restaurants;
reset fixtures provide the restaurant configuration. Browser HTML, CSS and JavaScript
are included in the image and use no CDN or external requests. Browser sessions persist
in this origin's local storage. An unchanged booking form retains its request body and
idempotency key, including after an uncertain response or server state import.

Stage 1, Stage 2 and Stage 3 exports from this team's services are supported. Single-table records
gain `table_ids` on import; identities, timestamps, tokens and original retry responses
remain intact. Approved pairs share occupancy on both members. All mutations, snapshots
and reads remain serialized under the same lock.

Manager IDs in restaurant fixtures authorize immutable, effective-dated policy publication.
Bookings retain accepted terms and revisions; a real amendment adopts its new date's
policy, while a no-op keeps the existing agreement. Histories are owner-only. Recurring
adoption creates all occurrences atomically, preserving the anchor. Individual changes
and collective moves track permanent diner exceptions and agreement revisions.
Older imports gain a policy-0 revision-1 history baseline without inventing past changes
or cancellation timestamps unavailable in the source export. Original receipts stay intact.

Run the focused regression suite with Python 3.12+:

```sh
python -m unittest discover -s tests -v
```

The optional browser regression script needs Playwright with Chromium and httpx in
the testing environment, never in the application image. With running Stage 4 and
the accepted Stage 2 containers:

```sh
python tests/browser_checks.py --base-url http://localhost:8080 --previous-base-url http://localhost:8081 --out /tmp/tablekeeper-browser-checks
python tests/browser_stage4.py --base-url http://localhost:8080 --out /tmp/tablekeeper-stage4-browser
```

It checks desktop/375px combined booking, replay, lookup/cancel, out-of-order search,
lost-response retry, stale availability, a populated preceding-stage migration,
policy terms, explanations, history, recurring recovery, amendments and long labels. Screenshots
and a summary go to the specified output directory. No credentials or exports are written.

Managers can preview a closure with `POST /restaurants/{id}/replans` and atomically
apply it with `POST /restaurants/{id}/replans/{plan_id}/apply`. Both require a manager
token and an idempotency key. Planning supports six tables, four declared pairs and
six considered bookings. It optimizes changed bookings, unused seats under each
booking's accepted capacities, then fixture option ranks in reference order.
Previews retain occupancy and histories; any intervening write at the same
restaurant invalidates the plan. Applied closures exclude affected seats from booking
and availability. Seating repairs preserve accepted terms, dates and diner exception
flags. Lookup shows reassignment history; a booking form retry reads current seating
while retaining its original request and retry key.
If the individual reservation read fails, confirmation tries the current owner
list. If neither authoritative read is available, it retains the confirmed reference,
clearly reports that current seating cannot be loaded, and offers an unchanged-form
retry or lookup. Historical receipt seating is never presented as current state.

Owners amend recurring visits with `POST /series/{id}/amend`, supplying a series
revision, starting index and local clock time. Eligible visits retain their original
scheduled dates and current seats; cancelled visits and diner exceptions are excluded.
Validation and conflict checks precede every commit. A whole amendment or repair
increments the restaurant and affected series counters once; no-op and replay writes
preserve counters. Imports recover Stage 3 scheduled dates from immutable adoption
receipts, including after individual moves or cancellations.

With this team's Stage 1–3 services running on their own ports, exercise populated
cross-stage transfers without writing any export to disk:

```sh
python tests/migration_checks.py --base-url http://localhost:8080 --stage1-url http://localhost:8081 --stage2-url http://localhost:8082 --stage3-url http://localhost:8083
```

The image runs as UID/GID 65534, with no package installation or outbound requests
at runtime. A custom port and resource-constrained start works as follows:

```sh
docker run --rm --name tablekeeper-stage4 -e PORT=9090 -p 9090:9090 --cpus=2 --memory=2g tablekeeper-stage4
```

The threaded server accepts up to 50 concurrent clients. Required ordinary requests
must finish within 5 seconds and test control requests within 10 seconds; readiness
must occur within 60 seconds. Six-booking planning uses bounded exhaustive search
with safe objective pruning. All application dependencies and IANA timezone data are
bundled in the pinned Python image; runtime networking may be disabled or restricted
to an internal Docker network. Disk state is ephemeral.

Every state operation runs under one process-wide lock. Validation constructs new
records before committing, and batch conflict detection checks the complete proposed
occupancy. A successful operation and its retry receipt commit in the same critical
section. The threaded HTTP server supports concurrent clients while this lock makes
state changes serializable. Passwords use salted scrypt. Timezone resolution uses
the image's IANA zoneinfo database, chooses the first repeated occurrence, rejects
nonexistent times, and computes durations on the UTC timeline.
