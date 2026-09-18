"""The close code a real browser sees, over a real socket.

TestClient talks ASGI directly, so it reports 4401 whether the endpoint closes
before or after accepting. A real server does not: closing before accept makes
uvicorn reject the handshake with HTTP 403, which browsers surface as close code
1006. The dashboard only signs out on 1008/4401 and retries everything else, so
that difference is the difference between a sign-out and a reconnect loop. This
test therefore runs the app under uvicorn on a real port.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

uvicorn = pytest.importorskip("uvicorn")
websockets = pytest.importorskip("websockets")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live_server(env):
    """Run the real app under uvicorn; yields the base URL."""
    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)

    yield f"127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


def _close_code_for(url: str) -> int:
    """Open the socket and report the close code, as a browser would see it."""

    async def go():
        try:
            async with websockets.connect(url) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
            return None  # stayed open
        except websockets.exceptions.ConnectionClosed as e:
            return e.code
        except websockets.exceptions.InvalidStatus as e:
            # Handshake refused: a browser reports this as 1006, which the
            # dashboard cannot tell apart from a flaky network.
            return 1006 if e.response.status_code else None

    return asyncio.run(go())


@pytest.mark.parametrize("query", ["", "?ticket=garbage"])
def test_rejected_socket_closes_with_4401_on_the_wire(live_server, query):
    assert _close_code_for(f"ws://{live_server}/ws{query}") == 4401
