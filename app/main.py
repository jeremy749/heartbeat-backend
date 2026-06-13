"""
Heartbeat backend — FastAPI server.

Role in the system:
    ESP32 firmware (MIT_BIH_Arduino.ino)
        --> serial --> realtime_classifier.py  (runs the ECGNet model)
            --> HTTP POST /api/beats --> THIS SERVER (stores + broadcasts)
                --> GET /api/* + WebSocket /ws --> React frontend

Run it:
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8000

Interactive API docs are served at http://localhost:8000/docs
"""

from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from . import alerts
from . import database as db
from .schemas import (
    ABNORMAL_CLASSES,
    CLASS_NAMES,
    BeatIn,
    BeatOut,
    StatsOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Heartbeat ECG Backend",
    description="Receives classified ECG beats and serves them to the dashboard.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow the Vite dev server (and a couple of common ports) to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:4173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── WebSocket connection manager (live push to the dashboard) ─────────────────
class ConnectionManager:
    def __init__(self) -> None:
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(message, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _resolve_class(beat: BeatIn) -> tuple[str, str]:
    """Return (class_code, human_name) from whichever field the caller provided."""
    if beat.class_code:
        code = beat.class_code.strip().upper()[:1]
        return code, CLASS_NAMES.get(code, "Unclassified")
    if beat.classification:
        name = beat.classification.strip().title()
        # Reverse-lookup the code from the name.
        for code, n in CLASS_NAMES.items():
            if n.lower() == name.lower():
                return code, n
        return "Q", name
    return "Q", "Unclassified"


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/", tags=["health"])
def root():
    return {"status": "ok", "service": "heartbeat-backend"}


@app.post("/api/beats", response_model=BeatOut, tags=["beats"])
async def ingest_beat(beat: BeatIn):
    """Ingest one classified beat from the classifier/device, store and broadcast it."""
    code, name = _resolve_class(beat)
    is_abnormal = code in ABNORMAL_CLASSES
    patient = (beat.patient or db.DEFAULT_PATIENT).strip() or db.DEFAULT_PATIENT

    # Compute the alert level here, on the server, so it's the single source of
    # truth. Uses this patient's recent beats so runs of abnormal beats count.
    recent = db.get_recent(patient, alerts.THRESHOLDS["window"])
    alert_level, alert_detail = alerts.evaluate_alert(
        name, beat.confidence, beat.flags, recent
    )

    stored = db.insert_beat(
        patient=patient,
        class_code=code,
        classification=name,
        confidence=beat.confidence,
        is_abnormal=is_abnormal,
        alert_level=alert_level,
        alert_detail=alert_detail,
        bpm=beat.bpm,
        flags=beat.flags or [],
        probabilities=beat.probabilities or {},
        samples=beat.samples,
        recorded_at=beat.recorded_at,
    )

    # Broadcast the stored beat plus the raw sample window (if any) so the
    # dashboard can draw the actual beat morphology in real time. Samples are
    # intentionally kept out of /api/history responses to keep them lightweight.
    await manager.broadcast({"type": "beat", "data": stored, "samples": beat.samples})
    return stored


@app.get("/api/history", response_model=List[BeatOut], tags=["beats"])
def history(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    patient: Optional[str] = Query(None, description="Filter by patient, or 'All'"),
    type: Optional[str] = Query(None, description="Filter by classification name, or 'All'"),
    abnormal_only: bool = Query(False),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    since: Optional[str] = Query(None, description="ISO timestamp lower bound"),
    until: Optional[str] = Query(None, description="ISO timestamp upper bound"),
):
    """Beats newest-first with optional filters and pagination."""
    return db.get_history(
        limit=limit,
        offset=offset,
        patient=patient,
        class_filter=type,
        abnormal_only=abnormal_only,
        min_confidence=min_confidence,
        since=since,
        until=until,
    )


@app.get("/api/latest", response_model=Optional[BeatOut], tags=["beats"])
def latest(patient: Optional[str] = Query(None)):
    """The single most recent beat (or null if none yet)."""
    return db.get_latest(patient)


@app.get("/api/stats", response_model=StatsOut, tags=["beats"])
def stats(patient: Optional[str] = Query(None)):
    """Summary counts for the dashboard cards."""
    return db.get_stats(patient)


@app.get("/api/trends", tags=["beats"])
def trends(patient: Optional[str] = Query(None), points: int = Query(60, ge=5, le=500)):
    """Heart-rate series and class/alert distributions for the Trends view."""
    return db.get_trends(patient, points)


@app.get("/api/patients", tags=["beats"])
def patients():
    """Distinct patient names seen so far."""
    return db.list_patients()


@app.get("/api/export.csv", tags=["beats"])
def export_csv(
    patient: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    abnormal_only: bool = Query(False),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
):
    """Download the (filtered) reading history as a CSV spreadsheet."""
    rows = db.get_history(
        limit=100000,
        patient=patient,
        class_filter=type,
        abnormal_only=abnormal_only,
        min_confidence=min_confidence,
        since=since,
        until=until,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "patient", "recorded_at", "classification", "confidence",
         "is_abnormal", "alert_level", "bpm", "flags"]
    )
    for r in rows:
        writer.writerow([
            r["id"], r["patient"], r["recorded_at"], r["classification"],
            f"{r['confidence']:.4f}", int(r["is_abnormal"]), r["alert_level"],
            r["bpm"] if r["bpm"] is not None else "", "|".join(r["flags"]),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=heartbeat_history.csv"},
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Frontend connects here to receive each new beat in real time."""
    await manager.connect(ws)
    try:
        while True:
            # We don't expect inbound messages; this keeps the socket open.
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)
