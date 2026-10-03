# Stage 1 HTTP service

From this directory, build and start with no additional setup:

```sh
docker build -t tablekeeper-stage1 . && docker run --rm --name tablekeeper-stage1 -e PORT=8080 -p 8080:8080 tablekeeper-stage1
```

The service listens on `0.0.0.0`, defaults to port 8080, and becomes ready at
`GET /health`. A fresh process has empty state. Load restaurant/user fixtures with
`POST /_test/reset`. Runtime has no external dependencies or network calls.
State is in memory and is intentionally lost at restart. Export/import can transfer
a consistent snapshot, including sessions and retry receipts; treat exports as private.

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
