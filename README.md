# Heartbeat Backend

[![CI](https://github.com/jeremy749/heartbeat-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/jeremy749/heartbeat-backend/actions/workflows/ci.yml)

FastAPI server that connects the ECG device + AI classifier to the React dashboard.

## Where it fits

```
ESP32 firmware (MIT_BIH_Arduino.ino)
   │  serial: "BEAT:v0,v1,...,v199"
   ▼
realtime_classifier.py  (runs the ECGNet PyTorch model)
   │  HTTP POST /api/beats   (via device_bridge.post_beat)
   ▼
THIS BACKEND  ── SQLite (heartbeat.db) ── stores every beat
   │  GET /api/history, /api/latest, /api/stats   +   WebSocket /ws (live push)
   ▼
React frontend (heartbeat-frontend)
```

The firmware and classifier you already have. The frontend you already have
(currently running on simulated data). This backend is the missing layer that
ties them together.

## Run it

```bash
cd heartbeat-backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs for interactive API docs.

On startup the server prints the **device ingest key** — the shared secret a
device or classifier must send to post beats:

```
[heartbeat] device ingest key (generated, stored in the db): 3f9a...
```

Set `DEVICE_API_KEY` to pin it to a value you choose (do this in deployment);
otherwise one is generated on first run and reused from then on. See
[`.env.example`](.env.example) for every setting and its default.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite covers sign-in and session expiry, the login throttle, who may post a
beat and whose account it lands on, per-user WebSocket delivery, the alert
engine, timestamp handling, and opening an older database in place. One file,
`tests/test_websocket_live.py`, runs the app under a real uvicorn server: the
close code a browser sees differs from what `TestClient` reports, and that
difference is the difference between the dashboard signing out and reconnecting
forever.

## Try it without hardware

In a second terminal, push fake beats so you can watch the database fill up
(and, once wired, the dashboard update):

```bash
export HEARTBEAT_DEVICE_KEY=<the key printed above>   # Windows: set HEARTBEAT_DEVICE_KEY=...
python device_bridge.py --demo
```

## Wire in the real classifier

In `realtime_classifier.py`, after the line that classifies a beat, add:

```python
from device_bridge import post_beat   # at the top

# ...inside the BEAT handling block, after classify_beat(...):
cls, conf, all_probs = classify_beat(samples, model, classes)
print_beat(beat_count, cls, conf, all_probs)
post_beat(cls, conf, all_probs)        # <-- sends it to the dashboard
```

That's the only change needed on the classifier side. `post_beat` reads the
ingest key from `HEARTBEAT_DEVICE_KEY`, so export that in the environment the
classifier runs in (or pass `key=` explicitly).

## API

| Method | Path           | Purpose                                  | Auth                     |
|--------|----------------|------------------------------------------|--------------------------|
| POST   | `/api/login`   | Sign in / sign up, returns a token        | —                        |
| POST   | `/api/beats`   | Ingest one classified beat                | device key **or** token  |
| GET    | `/api/history` | Recent beats (`?limit=`, `?type=`)        | token                    |
| GET    | `/api/latest`  | Most recent beat                          | token                    |
| GET    | `/api/stats`   | Summary counts for dashboard cards        | token                    |
| WS     | `/ws?token=`   | Live stream of *your own* new beats       | token                    |

Read endpoints take the token as `Authorization: Bearer <token>`, or as
`?token=` so plain download links (CSV, PDF) work. `/api/beats` takes the device
key as an `X-Device-Key` header.

Two ways to post a beat:

- **Device key** — a trusted recorder. It may name the account it is recording
  for, via `patient` (by name) or `user_id` (must already exist).
- **Session token** — the beat is filed under *that* user regardless of any
  `patient`/`user_id` in the payload, so one signed-in user cannot write into
  another's history.

## Notes

- Storage is SQLite (`heartbeat.db`), created automatically on first run. No
  separate database server needed. It runs in WAL mode, so a reader is never
  blocked behind a writer.
- Session tokens expire after `SESSION_TTL_HOURS` (30 days by default) and
  expired rows are purged at startup.
- Repeated sign-in failures for the same name and address are throttled. The
  counts live in memory, so they reset on restart and are per-process - enough
  to slow guessing on a single instance, not a hard guarantee behind several
  workers.
- Timestamps are normalized to UTC on the way in, so `since`/`until` filters
  compare correctly whatever offset the device sends.
- The WebSocket is per-user: a beat is pushed only to sockets belonging to the
  account that owns it.
- This is an educational/research project, **not** a medical device. Don't use
  its output for diagnosis.
