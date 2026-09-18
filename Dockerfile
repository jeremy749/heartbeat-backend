# Heartbeat backend.
#
# Works on any host that runs a container and sets $PORT (Render, Railway,
# Fly.io, Cloud Run). Nothing here is host-specific.

FROM python:3.13-slim

# Don't buffer stdout, or the startup lines (including the device key) sit in a
# buffer instead of appearing in the host's log viewer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so a code change doesn't reinstall them on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY device_bridge.py .

# Where the SQLite file lives. Point HEARTBEAT_DB at a mounted volume to keep
# data across deploys; left as-is it sits on the container's own disk, which on
# most free tiers is wiped on restart (see SEED_DEMO in the README).
ENV HEARTBEAT_DB=/data/heartbeat.db
RUN mkdir -p /data

# Hosts hand the port over in $PORT; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT is expanded. One worker on purpose: the WebSocket
# connection registry and the login throttle are per-process, so a second
# worker would silently halve the rate limit and drop live beats for anyone
# whose socket landed on the other one.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1
