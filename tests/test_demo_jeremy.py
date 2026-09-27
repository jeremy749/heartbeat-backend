"""The scripted demo feed used for recording the project video.

Only one thing here really needs guarding. The original version called
requests.post and returned success without looking at the status code, so a run
against a server that rejected every beat printed a screen full of "ok" while
the dashboard received nothing. That is a bad bug in a script whose entire job
is to make the dashboard do something on camera.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import demo_jeremy  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def send(monkeypatch, response=None, raises=None, capture=None):
    def fake_post(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture.update(kwargs)
        if raises is not None:
            raise raises
        return response or FakeResponse()

    monkeypatch.setattr(demo_jeremy.requests, "post", fake_post)
    return demo_jeremy.post_beat(
        "http://127.0.0.1:8000/api/beats", "k", "Jeremy", "N", 0.95, 72, [0.1, 0.2]
    )


def test_a_successful_post_reports_no_problem(monkeypatch):
    assert send(monkeypatch) == ""


def test_a_rejected_beat_is_reported_rather_than_swallowed(monkeypatch):
    """The regression: this used to come back as success."""
    problem = send(monkeypatch, FakeResponse(401))
    assert problem
    assert "401" in problem
    assert "HEARTBEAT_DEVICE_KEY" in problem  # says how to fix it


def test_a_server_error_is_reported(monkeypatch):
    problem = send(monkeypatch, FakeResponse(500, "boom"))
    assert "500" in problem


def test_an_unreachable_backend_is_reported(monkeypatch):
    problem = send(monkeypatch, raises=demo_jeremy.requests.RequestException("refused"))
    assert "could not reach" in problem


def test_the_device_key_travels_as_a_header(monkeypatch):
    """Never in the URL: query strings end up in the server's access log."""
    captured = {}
    send(monkeypatch, capture=captured)
    assert captured["headers"]["X-Device-Key"] == "k"
    assert "k" not in captured["url"].split("?")[-1] or "?" not in captured["url"]


def test_the_payload_carries_what_the_dashboard_needs(monkeypatch):
    captured = {}
    send(monkeypatch, capture=captured)
    payload = captured["json"]
    assert payload["class_code"] == "N"
    assert payload["patient"] == "Jeremy"
    assert payload["samples"] == [0.1, 0.2]


@pytest.mark.parametrize("shape", ["clean_beat_window", "noisy_beat_window", "mild_beat_window"])
def test_every_waveform_is_the_length_the_model_expects(shape):
    """200 samples, matching SEGMENT_LEN in the notebook and the firmware."""
    assert len(getattr(demo_jeremy, shape)()) == 200


def test_the_artifact_beats_look_different_from_clean_ones():
    """The noisy window is the thing that turns the banner red on camera; if it
    looked like a clean beat there would be nothing to film."""
    clean = demo_jeremy.clean_beat_window()
    noisy = demo_jeremy.noisy_beat_window()
    spread = lambda xs: max(xs) - min(xs)  # noqa: E731
    assert spread(noisy) > spread(clean) * 1.5
