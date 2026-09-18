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

import asyncio
import csv
import io
import json
import math
import os
import secrets
import statistics
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from . import alerts
from . import database as db
from . import demo
from .ratelimit import FailureLimiter
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


def _bearer(authorization: Optional[str]) -> Optional[str]:
    """Pull the token out of an `Authorization: Bearer <token>` header."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    return None


def require_user(authorization: Optional[str] = Header(default=None)) -> dict:
    """Resolve the signed-in user from the Authorization header.

    Header only, deliberately. A session token is good for 30 days, and a URL
    is written to the access log, the browser history and every proxy on the
    way - so the query-parameter form this used to accept handed out a durable
    credential in plain text. Downloads and the WebSocket, which cannot set a
    header, use a short-lived ticket instead (see require_ticket_user).
    """
    user = db.user_for_token(_bearer(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


def require_ticket_user(
    authorization: Optional[str] = Header(default=None),
    ticket: Optional[str] = Query(default=None),
) -> dict:
    """Authorize a download by Authorization header, or by a one-use ticket.

    The ticket is what a plain <a href> or a WebSocket handshake can carry. It
    is spent on redemption, so a URL captured from a log cannot be replayed.
    """
    user = db.user_for_token(_bearer(authorization)) or db.redeem_ticket(ticket)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


# Slow down password guessing: after this many failures for the same
# name+address inside the window, sign-in is refused until they age out.
LOGIN_LIMITER = FailureLimiter(
    max_failures=int(os.environ.get("LOGIN_MAX_FAILURES", "8")),
    window_seconds=float(os.environ.get("LOGIN_WINDOW_SECONDS", "300")),
)


def _iso_bound(value: Optional[str], field: str) -> Optional[str]:
    """Normalize a since/until filter to the same UTC form the rows are stored in.

    Without this, a bound written in another offset compares wrongly against the
    stored text and quietly returns the wrong rows.
    """
    if not value:
        return None
    try:
        return db.utc_iso(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be an ISO timestamp, e.g. 2026-01-01T00:00:00Z",
        )


# How often the background sweep runs. It drops old waveforms, applies the beat
# retention window if one is set, and clears expired sessions.
RETENTION_INTERVAL_SECONDS = float(os.environ.get("RETENTION_INTERVAL_SECONDS", "3600"))


def run_retention() -> dict:
    """One retention pass. Safe to call at any time; returns what it did."""
    result = {
        "waveforms_dropped": db.prune_samples(),
        "beats_removed": db.delete_old_beats(),
        "sessions_purged": db.purge_expired_sessions(),
        "tickets_purged": db.purge_expired_tickets(),
    }
    if any(result.values()):
        print(
            f"[heartbeat] retention: {result['waveforms_dropped']} waveform(s) dropped, "
            f"{result['beats_removed']} beat(s) removed, "
            f"{result['sessions_purged']} session(s) purged, "
            f"{result['tickets_purged']} ticket(s) purged"
        )
    return result


async def _retention_loop() -> None:
    """Run the sweep on a timer until the app shuts down."""
    while True:
        try:
            # Off the event loop: these are blocking SQLite writes.
            await asyncio.to_thread(run_retention)
        except Exception as exc:  # noqa: BLE001 - a sweep failing must not kill the loop
            print(f"[heartbeat] retention sweep failed: {exc!r}")
        await asyncio.sleep(RETENTION_INTERVAL_SECONDS)


_DEVICE_KEY: Optional[str] = None


def device_key() -> str:
    """The shared secret a device/classifier must present to POST beats.

    Read from the DEVICE_API_KEY environment variable when set; otherwise one is
    generated and stored in the database so it stays the same across restarts.
    Either way it is printed at startup.
    """
    global _DEVICE_KEY
    if _DEVICE_KEY is None:
        _DEVICE_KEY = os.environ.get("DEVICE_API_KEY", "").strip() or db.get_or_create_device_key()
    return _DEVICE_KEY


def require_ingest(
    x_device_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> Optional[dict]:
    """Authorize a beat ingest, by device key or by a signed-in user's token.

    Returns the signed-in user when a session token was used - their beats are
    then forced onto their own account - or None when a trusted device key was
    used, which may name the patient it is recording.

    Headers only: a device posting a beat can always set one, so there is no
    reason to accept a credential in the URL, where it would be logged.
    """
    if x_device_key and secrets.compare_digest(x_device_key, device_key()):
        return None
    user = db.user_for_token(_bearer(authorization))
    if user:
        return user
    raise HTTPException(
        status_code=401,
        detail="Beat ingest needs an X-Device-Key header or a signed-in session token.",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    # On a host with an ephemeral disk the database is empty after every cold
    # start, and an empty dashboard is a bad first impression for anyone opening
    # a shared link. Seeding is off unless SEED_DEMO is set, and does nothing
    # when the demo account already has readings.
    if os.environ.get("SEED_DEMO", "").strip().lower() in {"1", "true", "yes"}:
        seeded = demo.seed_demo_data()
        if seeded:
            print(f"[heartbeat] seeded the demo account with {seeded} readings")

    source = "DEVICE_API_KEY env" if os.environ.get("DEVICE_API_KEY", "").strip() else "generated, stored in the db"
    print(f"[heartbeat] device ingest key ({source}): {device_key()}")
    sweeper = asyncio.create_task(_retention_loop())
    try:
        yield
    finally:
        sweeper.cancel()


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
    # Retry-After is not a CORS-safelisted response header, so without this the
    # dashboard cannot read how long a throttled sign-in must wait.
    expose_headers=["Retry-After"],
)


# ── WebSocket connection manager (live push to the dashboard) ─────────────────
class ConnectionManager:
    """Live sockets, grouped by user.

    Keyed by user_id so a beat only reaches the person it belongs to - a flat
    list streamed everyone's ECG to every open dashboard.
    """

    def __init__(self) -> None:
        self.active: Dict[int, List[WebSocket]] = {}

    async def connect(self, ws: WebSocket, user_id: int) -> None:
        await ws.accept()
        self.active.setdefault(user_id, []).append(ws)

    def disconnect(self, ws: WebSocket, user_id: int) -> None:
        conns = self.active.get(user_id)
        if not conns or ws not in conns:
            return
        conns.remove(ws)
        if not conns:
            del self.active[user_id]

    async def broadcast(self, user_id: int, message: dict) -> None:
        dead = []
        for ws in list(self.active.get(user_id, [])):
            try:
                await ws.send_text(json.dumps(message, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, user_id)


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
def login(body: LoginIn, request: Request):
    """Sign in or sign up with name + password; returns a session token.

    First time a name is used, the given password is set for it and `created` is
    true. After that the password must match. Repeated failures for the same
    name and address are throttled.
    """
    client = request.client.host if request.client else "?"
    key = f"{(body.name or '').strip().lower()}|{client}"

    wait = LOGIN_LIMITER.retry_after(key)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed sign-in attempts. Try again in {int(wait) + 1}s.",
            headers={"Retry-After": str(int(wait) + 1)},
        )

    user = db.authenticate(body.name, body.password)
    if not user:
        LOGIN_LIMITER.record_failure(key)
        raise HTTPException(status_code=401, detail="Wrong password for that name.")

    LOGIN_LIMITER.reset(key)
    token = db.create_session(user["id"])
    return {
        "id": user["id"],
        "name": user["name"],
        "token": token,
        "created": user.get("created", False),
    }


@app.post("/api/logout", tags=["users"])
def logout(user=Depends(require_user), authorization: Optional[str] = Header(default=None)):
    """Invalidate the current session token."""
    tok = _bearer(authorization)
    if tok:
        db.delete_session(tok)
    return {"status": "signed out"}


@app.post("/api/ticket", tags=["users"])
def issue_ticket(user=Depends(require_user)):
    """Mint a short-lived, single-use ticket for one download or socket.

    The caller puts this in the URL instead of its session token.
    """
    return {
        "ticket": db.create_ticket(user["id"]),
        "expires_in": db.TICKET_TTL_SECONDS,
    }


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
async def ingest_beat(beat: BeatIn, ingestor: Optional[dict] = Depends(require_ingest)):
    """Ingest one classified beat from the classifier/device, store and broadcast it."""
    code, name = _resolve_class(beat)
    is_abnormal = code in ABNORMAL_CLASSES

    # Resolve the owning user.
    if ingestor is not None:
        # Authorized by session token: the beat lands on that user's own
        # account, whatever patient/user_id the payload claims.
        owner = ingestor
    elif beat.user_id is not None:
        # Authorized by device key, targeting an existing account by id.
        owner = db.get_user(beat.user_id)
        if owner is None:
            raise HTTPException(status_code=404, detail=f"No user with id {beat.user_id}")
    else:
        owner = db.get_or_create_user(beat.patient or db.DEFAULT_USER)
    user_id, patient = owner["id"], owner["name"]

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

    await manager.broadcast(user_id, {"type": "beat", "data": stored, "samples": beat.samples})
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
        since=_iso_bound(since, "since"),
        until=_iso_bound(until, "until"),
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
def report_pdf(user=Depends(require_ticket_user)):
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
    user=Depends(require_ticket_user),
):
    """Stream the signed-in user's (filtered) history as a CSV spreadsheet.

    Streamed rather than assembled in memory: a long history is exactly when
    someone wants the export, and that used to mean buffering every row and its
    rendered text before sending a single byte.
    """
    rows = db.iter_history(
        user_id=user["id"],
        class_filter=type,
        abnormal_only=abnormal_only,
        min_confidence=min_confidence,
        since=_iso_bound(since, "since"),
        until=_iso_bound(until, "until"),
    )

    def lines():
        buf = io.StringIO()
        writer = csv.writer(buf)

        def flush() -> str:
            out = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            return out

        # Friendly, human-readable column headers (no underscores).
        writer.writerow(
            ["ID", "Patient", "Recorded At", "Classification", "Confidence",
             "Abnormal", "Alert Level", "Heart Rate (bpm)", "Flags"]
        )
        yield flush()

        for r in rows:
            writer.writerow([
                r["id"], r["patient"], r["recorded_at"], r["classification"],
                f"{round(r['confidence'] * 100)}%",          # 0.97 -> "97%"
                "Yes" if r["is_abnormal"] else "No",          # 1/0 -> Yes/No
                r["alert_level"].capitalize(),
                r["bpm"] if r["bpm"] is not None else "",
                ", ".join(r["flags"]),
            ])
            yield flush()

    return StreamingResponse(
        lines(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=heartbeat_history.csv"},
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, ticket: Optional[str] = Query(default=None)):
    """Frontend connects here (as /ws?ticket=...) to receive its own beats live.

    A ticket rather than the session token: the handshake URL is logged like any
    other request, and this one is worthless seconds after it is issued.
    """
    user = db.redeem_ticket(ticket)
    if not user:
        # Accept first, *then* close. Closing before accept makes the server
        # reject the handshake with HTTP 403, which browsers surface as close
        # code 1006 - indistinguishable from a dropped network, so the client
        # would reconnect forever instead of signing out. Completing the
        # handshake lets the 4401 through (private range, "sign in again").
        await ws.accept()
        await ws.close(code=4401, reason="Sign in required")
        return
    await manager.connect(ws, user["id"])
    try:
        while True:
            # We don't expect inbound messages; this keeps the socket open.
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws, user["id"])
    except Exception:
        manager.disconnect(ws, user["id"])
