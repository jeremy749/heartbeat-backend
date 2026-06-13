"""
device_bridge.py
─────────────────────────────────────────────────────────────────────────────
Glue between realtime_classifier.py and the FastAPI backend.

Your existing realtime_classifier.py prints each classified beat to the
console. To get those beats into the dashboard, POST them to the backend
instead of (or in addition to) printing.

Two ways to use this:

  A) Import post_beat() inside realtime_classifier.py and call it right after
     classify_beat(), e.g.:

         from device_bridge import post_beat
         cls, conf, all_probs = classify_beat(samples, model, classes)
         post_beat(cls, conf, all_probs, bpm=current_bpm)

  B) Run this file standalone with --demo to push fake beats and confirm the
     backend + frontend light up before your hardware is wired in:

         python device_bridge.py --demo
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import math
import random
import time
from typing import Dict, List, Optional

import requests

# Use 127.0.0.1 rather than "localhost": on Windows "localhost" can resolve to
# IPv6 (::1) first and hang, while the server listens on IPv4. 127.0.0.1 is the
# same machine but takes the direct IPv4 route.
BACKEND_URL = "http://127.0.0.1:8000/api/beats"


def post_beat(
    class_code: str,
    confidence: float,
    probabilities: Optional[Dict[str, float]] = None,
    bpm: Optional[float] = None,
    flags: Optional[List[str]] = None,
    samples: Optional[List[float]] = None,
    patient: str = "Demo Patient",
    url: str = BACKEND_URL,
) -> bool:
    """Send one classified beat to the backend. Returns True on success."""
    payload = {
        "class_code": class_code,
        "confidence": float(confidence),
        "probabilities": probabilities or {},
        "bpm": bpm,
        "flags": flags or [],
        "samples": samples,
        "patient": patient,
    }
    try:
        # proxies={...: None} tells requests to bypass any system/corporate/school
        # HTTP proxy for this call. Without it, requests routes even a localhost
        # request through the configured proxy, which can't reach your local
        # server and times out.
        resp = requests.post(
            url, json=payload, timeout=3, proxies={"http": None, "https": None}
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        # Don't crash the classifier just because the dashboard is offline.
        print(f"  [device_bridge: could not reach backend - {exc}]")
        return False


def _fake_beat_window(code: str, n: int = 200) -> List[float]:
    """Generate a synthetic 200-sample ECG-like beat for the demo waveform.

    Produces a small baseline with a P wave, a sharp QRS complex, and a T wave.
    Ventricular ("V") beats get a wider, inverted QRS to look visibly different.
    """
    wide = code == "V"
    samples = []
    for i in range(n):
        t = i / n
        baseline = 0.05 * math.sin(2 * math.pi * t * 2)
        p_wave = 0.12 * math.exp(-((t - 0.25) ** 2) / 0.0010)
        qrs_w = 0.0016 if wide else 0.0004
        qrs_amp = -0.6 if wide else 1.0
        qrs = qrs_amp * math.exp(-((t - 0.45) ** 2) / qrs_w)
        t_wave = 0.25 * math.exp(-((t - 0.65) ** 2) / 0.0040)
        noise = random.uniform(-0.02, 0.02)
        samples.append(round(baseline + p_wave + qrs + t_wave + noise, 4))
    return samples


def _demo(n: int = 50, url: str = BACKEND_URL) -> None:
    """Push fake beats so you can see the dashboard update without hardware."""
    classes = ["N", "N", "N", "V", "S", "F", "Q"]  # weighted toward Normal
    print(f"Posting {n} demo beats to {url} (Ctrl+C to stop)...")
    for i in range(n):
        code = random.choice(classes)
        conf = round(random.uniform(0.55, 0.98), 2)
        probs = {c: round(random.random(), 2) for c in ["N", "S", "V", "F", "Q"]}
        probs[code] = conf
        bpm = round(random.uniform(58, 105), 0)
        samples = _fake_beat_window(code)
        ok = post_beat(code, conf, probs, bpm=bpm, samples=samples)
        print(f"  beat {i + 1:>3}  {code}  conf={conf:.2f}  bpm={bpm}  {'ok' if ok else 'FAILED'}")
        time.sleep(0.8)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Heartbeat device bridge")
    parser.add_argument("--demo", action="store_true", help="Push fake beats to the backend")
    parser.add_argument("--n", type=int, default=50, help="Number of demo beats")
    parser.add_argument("--url", default=BACKEND_URL, help="Backend ingest URL")
    args = parser.parse_args()

    if args.demo:
        _demo(args.n, args.url)
    else:
        parser.print_help()
