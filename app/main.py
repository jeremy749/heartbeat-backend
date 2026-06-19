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
import math
import os
import statistics
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from . import alerts
from . import database as db
from .schemas import (
    ABNORMAL_CLASSES,
    CLASS_NAMES,
    AccountOut,
    AuthOut,
    BeatIn,
    BeatOut,
    ChangePasswordIn,
    LoginIn,
    StatsOut,
    UserOut,
)


def _hrv(bpms: List[float]) -> dict:
    """Heart-rate variability from a series of heart rates.

    RR interval (ms) = 60000 / bpm. SDNN is the spread of RR intervals; RMSSD is
    the root-mean-square of successive differences (a common short-term HRV
    measure). Returns Nones when there isn't enough data.
    """
    rr = [60000.0 / b for b in bpms if b and b > 0]
    if len(rr) < 2:
        return {"mean_bpm": round(statistics.mean(bpms), 1) if bpms else None,
                "sdnn": None, "rmssd": None, "count": len(rr)}
    diffs = [rr[i + 1] - rr[i] for i in range(len(rr) - 1)]
    rmssd = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    return {
        "mean_bpm": round(statistics.mean(bpms), 1),
        "sdnn": round(statistics.pstdev(rr), 1),
        "rmssd": round(rmssd, 1),
        "count": len(rr),
    }


def require_user(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> dict:
    """Resolve the signed-in user from a Bearer token (header) or ?token= query.

    The query form lets plain download links (CSV export) carry the token.
    """
    tok = None
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization[7:]
    tok = tok or token
    user = db.user_for_token(tok)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


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

# Which websites are allowed to call this API.
#   - local dev ports (Vite)
#   - any *.netlify.app site (your deployed frontend)
#   - anything listed in the CORS_ORIGINS env var (comma-separated), e.g. a
#     custom domain:  CORS_ORIGINS=https://heartbeat.yourdomain.com
_default_origins = [
    "http://localhost:5173",
    "http://localhost:4173",
    "http://localhost:3000",
]
_env_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_default_origins + _env_origins,
    allow_origin_regex=r"https://.*\.netlify\.app",
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


@app.post("/api/login", response_model=AuthOut, tags=["users"])
def login(body: LoginIn):
    """Sign in or sign up with name + password; returns a session token.

    First time a name is used, the given password is set for it. After that the
    password must match.
    """
    user = db.authenticate(body.name, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Wrong password for that name.")
    token = db.create_session(user["id"])
    return {"id": user["id"], "name": user["name"], "token": token}


@app.post("/api/logout", tags=["users"])
def logout(user=Depends(require_user), token: Optional[str] = Query(default=None),
           authorization: Optional[str] = Header(default=None)):
    """Invalidate the current session token."""
    tok = token
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization[7:]
    if tok:
        db.delete_session(tok)
    return {"status": "signed out"}


@app.get("/api/me", response_model=UserOut, tags=["users"])
def me(user=Depends(require_user)):
    """The currently signed-in user (verifies a token is valid)."""
    return user


@app.get("/api/account", response_model=AccountOut, tags=["users"])
def account(user=Depends(require_user)):
    """Account summary: name, created date, and reading count."""
    info = db.get_account(user["id"])
    if not info:
        raise HTTPException(status_code=404, detail="Account not found")
    return info


@app.post("/api/change-password", tags=["users"])
def change_password(body: ChangePasswordIn, user=Depends(require_user)):
    """Change the signed-in user's password (checks the current one)."""
    if not db.change_password(user["id"], body.current_password, body.new_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    return {"status": "password changed"}


@app.delete("/api/account/readings", tags=["users"])
def delete_readings(user=Depends(require_user)):
    """Delete all of the signed-in user's readings (keeps the account)."""
    removed = db.delete_user_readings(user["id"])
    return {"status": "ok", "removed": removed}


@app.delete("/api/account", tags=["users"])
def delete_account(user=Depends(require_user)):
    """Delete the signed-in user's account, readings, and sessions."""
    db.delete_user(user["id"])
    return {"status": "account deleted"}


@app.post("/api/beats", response_model=BeatOut, tags=["beats"])
async def ingest_beat(beat: BeatIn):
    """Ingest one classified beat from the classifier/device, store and broadcast it."""
    code, name = _resolve_class(beat)
    is_abnormal = code in ABNORMAL_CLASSES

    # Resolve the owning user: prefer an explicit user_id, else the name.
    if beat.user_id is not None:
        user_id, patient = beat.user_id, (beat.patient or db.DEFAULT_USER)
    else:
        user = db.get_or_create_user(beat.patient or db.DEFAULT_USER)
        user_id, patient = user["id"], user["name"]

    # Compute the alert level here, on the server (single source of truth),
    # using this user's recent beats so runs of abnormal beats count.
    recent = db.get_recent(user_id, alerts.THRESHOLDS["window"])
    alert_level, alert_detail = alerts.evaluate_alert(name, beat.confidence, beat.flags, recent)

    stored = db.insert_beat(
        user_id=user_id,
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

    await manager.broadcast({"type": "beat", "data": stored, "samples": beat.samples})
    return stored


@app.get("/api/history", response_model=List[BeatOut], tags=["beats"])
def history(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    type: Optional[str] = Query(None, description="Filter by classification name, or 'All'"),
    abnormal_only: bool = Query(False),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    since: Optional[str] = Query(None, description="ISO timestamp lower bound"),
    until: Optional[str] = Query(None, description="ISO timestamp upper bound"),
    user=Depends(require_user),
):
    """Beats newest-first for the signed-in user, with optional filters."""
    return db.get_history(
        limit=limit,
        offset=offset,
        user_id=user["id"],
        class_filter=type,
        abnormal_only=abnormal_only,
        min_confidence=min_confidence,
        since=since,
        until=until,
    )


@app.get("/api/latest", response_model=Optional[BeatOut], tags=["beats"])
def latest(user=Depends(require_user)):
    """The signed-in user's most recent beat (or null if none yet)."""
    return db.get_latest(user["id"])


@app.get("/api/stats", response_model=StatsOut, tags=["beats"])
def stats(user=Depends(require_user)):
    """Summary counts for the dashboard cards (signed-in user)."""
    return db.get_stats(user["id"])


@app.get("/api/trends", tags=["beats"])
def trends(points: int = Query(60, ge=5, le=500), user=Depends(require_user)):
    """Heart-rate series and class/alert distributions for the Trends view."""
    return db.get_trends(user["id"], points)


@app.get("/api/strip", tags=["beats"])
def strip(count: int = Query(8, ge=1, le=50), user=Depends(require_user)):
    """A multi-beat ECG strip (sample windows joined end-to-end) plus HRV."""
    beats = db.get_recent_with_samples(user["id"], count)
    samples: List[float] = []
    for b in beats:
        samples.extend(b["samples"])
    return {
        "beats": len(beats),
        "samples": samples,
        "hrv": _hrv(db.get_bpm_series(user["id"], 200)),
    }


@app.get("/api/report.pdf", tags=["beats"])
def report_pdf(user=Depends(require_user)):
    """Generate a one-page PDF summary of the signed-in user's readings."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="reportlab not installed - run: pip install reportlab",
        )

    s = db.get_stats(user["id"])
    hrv = _hrv(db.get_bpm_series(user["id"], 200))
    styles = getSampleStyleSheet()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title="Heartbeat ECG Report")
    story = [
        Paragraph("Heartbeat ECG Report", styles["Title"]),
        Paragraph(f"User: {user['name']}", styles["Normal"]),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]),
        Spacer(1, 16),
    ]

    summary_rows = [
        ["Summary", ""],
        ["Total beats", str(s["total_beats"])],
        ["Abnormal beats", str(s["abnormal_beats"])],
        ["Mean heart rate", f"{hrv['mean_bpm']} bpm" if hrv["mean_bpm"] is not None else "-"],
        ["HRV (RMSSD)", f"{hrv['rmssd']} ms" if hrv["rmssd"] is not None else "-"],
        ["HRV (SDNN)", f"{hrv['sdnn']} ms" if hrv["sdnn"] is not None else "-"],
    ]
    table_style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111826")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("PADDING", (0, 0), (-1, -1), 6),
    ])
    t = Table(summary_rows, colWidths=[200, 200])
    t.setStyle(table_style)
    story += [t, Spacer(1, 18), Paragraph("Beats by type", styles["Heading2"])]

    class_rows = [["Type", "Count"]] + [[k, str(v)] for k, v in s["counts_by_class"].items()]
    if len(class_rows) == 1:
        class_rows.append(["(no data)", "0"])
    t2 = Table(class_rows, colWidths=[200, 200])
    t2.setStyle(table_style)
    story += [
        t2,
        Spacer(1, 24),
        Paragraph(
            "Educational / research project. Not a medical device and not a diagnosis.",
            styles["Italic"],
        ),
    ]

    doc.build(story)
    pdf = buf.getvalue()
    buf.close()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=heartbeat_report.pdf"},
    )


@app.get("/api/export.csv", tags=["beats"])
def export_csv(
    type: Optional[str] = Query(None),
    abnormal_only: bool = Query(False),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    user=Depends(require_user),
):
    """Download the signed-in user's (filtered) history as a CSV spreadsheet."""
    rows = db.get_history(
        limit=100000,
        user_id=user["id"],
        class_filter=type,
        abnormal_only=abnormal_only,
        min_confidence=min_confidence,
        since=since,
        until=until,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    # Friendly, human-readable column headers (no underscores).
    writer.writerow(
        ["ID", "Patient", "Recorded At", "Classification", "Confidence",
         "Abnormal", "Alert Level", "Heart Rate (bpm)", "Flags"]
    )
    for r in rows:
        writer.writerow([
            r["id"], r["patient"], r["recorded_at"], r["classification"],
            f"{round(r['confidence'] * 100)}%",          # 0.97 -> "97%"
            "Yes" if r["is_abnormal"] else "No",          # 1/0 -> Yes/No
            r["alert_level"].capitalize(),
            r["bpm"] if r["bpm"] is not None else "",
            ", ".join(r["flags"]),
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
