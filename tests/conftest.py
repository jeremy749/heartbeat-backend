"""Shared fixtures.

Every test gets its own SQLite file via HEARTBEAT_DB, which app.database reads
per call, so nothing leaks between tests. The device key is pinned so tests can
present it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

DEVICE_KEY = "test-device-key"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEVICE_API_KEY", DEVICE_KEY)
    from app import main

    # These are process-wide caches; reset them so tests don't inherit state.
    main._DEVICE_KEY = None
    main.LOGIN_LIMITER.clear()
    return tmp_path


@pytest.fixture()
def client(env):
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def device_headers():
    return {"X-Device-Key": DEVICE_KEY}


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup(client, name: str, password: str = "pw") -> dict:
    """Create (or sign in to) an account and return the login payload."""
    r = client.post("/api/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


BEAT = {"class_code": "N", "confidence": 0.9, "bpm": 70}


def ticket(client, token: str) -> str:
    """Mint a one-use download/socket ticket for a signed-in user."""
    r = client.post("/api/ticket", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


WS_TIMEOUT = 5.0


def ws_receive(ws, timeout: float = WS_TIMEOUT):
    """Read one frame, failing the test if none arrives.

    TestClient's receive() blocks forever on a socket that stays open, so a
    regression that *fails* to close a socket would hang the suite instead of
    reporting a failure. Reading on a worker thread turns that into an
    assertion.
    """
    import concurrent.futures

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(ws.receive)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise AssertionError(
            f"no websocket frame within {timeout}s - the socket was left open"
        ) from None
    finally:
        # Don't block teardown on the worker if it is still parked in receive().
        pool.shutdown(wait=False)
