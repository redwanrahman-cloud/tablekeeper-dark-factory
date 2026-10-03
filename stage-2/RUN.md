# Stage 2 restaurant booking

From this directory, build and start with no additional setup:

```sh
docker build -t tablekeeper-stage2 . && docker run --rm --name tablekeeper-stage2 -e PORT=8080 -p 8080:8080 tablekeeper-stage2
```

The service listens on `0.0.0.0`, defaults to port 8080, and becomes ready at
`GET /health`. A fresh process has empty state. Load restaurant/user fixtures with
`POST /_test/reset`. Runtime has no external dependencies or network calls.
State is in memory and is intentionally lost at restart. Export/import can transfer
a consistent snapshot, including sessions and retry receipts; treat exports as private.

Open `http://localhost:8080/` to browse and book, `/signup` or `/login` to sign in,
and `/lookup` to find or cancel a booking. The fresh process has no restaurants;
reset fixtures provide the restaurant configuration. Browser HTML, CSS and JavaScript
are included in the image and use no CDN or external requests. Browser sessions persist
in this origin's local storage. An unchanged booking form retains its request body and
idempotency key, including after an uncertain response or server state import.

Stage 1 exports from this team's accepted service are supported. Single-table records
gain `table_ids` on import; identities, timestamps, tokens and original retry responses
remain intact. Approved pairs share occupancy on both members. All mutations, snapshots
and reads remain serialized under the same lock.

Run the focused regression suite with Python 3.12+:

```sh
python -m unittest discover -s tests -v
```

Every state operation runs under one process-wide lock. Validation constructs new
records before committing, and batch conflict detection checks the complete proposed
occupancy. A successful operation and its retry receipt commit in the same critical
section. The threaded HTTP server supports concurrent clients while this lock makes
state changes serializable. Passwords use salted scrypt. Timezone resolution uses
the image's IANA zoneinfo database, chooses the first repeated occurrence, rejects
nonexistent times, and computes durations on the UTC timeline.
