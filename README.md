# Heartbeat Backend

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

## Try it without hardware

In a second terminal, push fake beats so you can watch the database fill up
(and, once wired, the dashboard update):

```bash
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

That's the only change needed on the classifier side.

## API

| Method | Path           | Purpose                                  |
|--------|----------------|------------------------------------------|
| POST   | `/api/beats`   | Ingest one classified beat               |
| GET    | `/api/history` | Recent beats (`?limit=`, `?type=`)       |
| GET    | `/api/latest`  | Most recent beat                         |
| GET    | `/api/stats`   | Summary counts for dashboard cards       |
| WS     | `/ws`          | Live stream of each new beat             |

## Notes

- Storage is SQLite (`heartbeat.db`), created automatically on first run. No
  separate database server needed.
- This is an educational/research project, **not** a medical device. Don't use
  its output for diagnosis.
