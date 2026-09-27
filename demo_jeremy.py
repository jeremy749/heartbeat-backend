"""
demo_jeremy.py
─────────────────────────────────────────────────────────────────────────────
A *scripted* demo feed, for recording the project video.

device_bridge.py --demo streams random beats, which is right for checking that
the plumbing works and wrong for filming: nothing in particular ever happens.
This script is choreographed instead. It streams mostly-normal beats, then at a
known beat number sends three noisy ones in a row that classify as Ventricular,
so the dashboard's banner goes red on cue and settles back to green afterwards.
That is the moment you narrate ("here I moved my arm").

    # terminal 1
    uvicorn app.main:app --port 8000

    # terminal 2
    set HEARTBEAT_DEVICE_KEY=<the key the server printed>
    python demo_jeremy.py --patient Jeremy

Then sign into the dashboard as the same name and watch. Ctrl+C stops it.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import math
import os
import random
import time
from typing import List, Optional

import requests

DEFAULT_URL = "http://127.0.0.1:8000/api/beats"

# Beat ingest is authenticated. Set this to the key the server prints at
# startup, or pass --key.
DEVICE_KEY = os.environ.get("HEARTBEAT_DEVICE_KEY", "")


# ── Waveform shapes ───────────────────────────────────────────────────────────
def clean_beat_window(n: int = 200) -> List[float]:
    """A realistic, clean P-QRS-T heartbeat."""
    out = []
    for i in range(n):
        t = i / n
        p_wave = 0.12 * math.exp(-((t - 0.25) ** 2) / 0.0010)
        qrs = 1.0 * math.exp(-((t - 0.45) ** 2) / 0.0004)
        t_wave = 0.25 * math.exp(-((t - 0.65) ** 2) / 0.0040)
        out.append(round(p_wave + qrs + t_wave + random.uniform(-0.02, 0.02), 4))
    return out


def noisy_beat_window(n: int = 200) -> List[float]:
    """An erratic, thrown-off signal: wandering baseline plus random spikes.

    This is what motion artifact actually looks like on a single-lead trace -
    the electrode moves, the baseline wanders, and the detector sees peaks that
    were never heartbeats.
    """
    out = []
    base = 0.0
    for _ in range(n):
        base += random.uniform(-0.15, 0.15)
        spike = random.uniform(-1.3, 1.3) if random.random() < 0.30 else 0.0
        out.append(round(base + spike + random.uniform(-0.3, 0.3), 4))
    return out


def mild_beat_window(n: int = 200) -> List[float]:
    """A clean beat with a wider, lower QRS - off, but not noise."""
    out = []
    for i in range(n):
        t = i / n
        p_wave = 0.10 * math.exp(-((t - 0.25) ** 2) / 0.0012)
        qrs = 0.85 * math.exp(-((t - 0.45) ** 2) / 0.0006)
        t_wave = 0.22 * math.exp(-((t - 0.66) ** 2) / 0.0050)
        out.append(round(p_wave + qrs + t_wave + random.uniform(-0.05, 0.05), 4))
    return out


# ── Posting ───────────────────────────────────────────────────────────────────
def post_beat(url: str, key: str, patient: str, code: str, conf: float,
              bpm: float, samples: List[float], flags: Optional[List[str]] = None) -> str:
    """Send one beat. Returns "" on success, or a reason it failed.

    The reason matters: the earlier version of this script called requests.post
    and returned True without looking at the status, so a run against a server
    that rejected every beat printed a thousand cheerful "ok" lines and filmed
    an empty dashboard.
    """
    payload = {
        "class_code": code,
        "confidence": round(float(conf), 2),
        "probabilities": {code: round(float(conf), 2)},
        "bpm": round(float(bpm)),
        "flags": flags or [],
        "samples": samples,
        "patient": patient,
    }
    # Bypass the proxy for localhost only; a deployed backend should go through
    # whatever proxy a school or office network requires.
    local = "127.0.0.1" in url or "localhost" in url
    proxies = {"http": None, "https": None} if local else None
    try:
        resp = requests.post(
            url, json=payload, timeout=10,
            headers={"X-Device-Key": key}, proxies=proxies,
        )
    except requests.RequestException as exc:
        return f"could not reach {url} - {exc}"

    if resp.status_code == 401:
        return ("rejected (401): set HEARTBEAT_DEVICE_KEY, or pass --key, to "
                "match the key the server printed at startup")
    if resp.status_code >= 400:
        return f"rejected ({resp.status_code}): {resp.text[:120]}"
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Scripted demo feed for the project video")
    parser.add_argument("--url", default=DEFAULT_URL, help="Backend ingest URL")
    parser.add_argument("--key", default=DEVICE_KEY,
                        help="Device ingest key (default: $HEARTBEAT_DEVICE_KEY)")
    parser.add_argument("--patient", default="Jeremy",
                        help="Account the beats land on; sign in as this name")
    parser.add_argument("--beats", type=int, default=1000, help="How many beats to stream")
    parser.add_argument("--delay", type=float, default=0.8,
                        help="Seconds between beats (0.8 ~= 75 bpm pacing)")
    parser.add_argument("--off-at", type=int, default=100,
                        help="Beat number where the artifact starts")
    parser.add_argument("--off-count", type=int, default=3,
                        help="How many artifact beats in a row")
    parser.add_argument("--off-code", default="V", choices=["V", "S", "F", "Q"],
                        help="Class for the artifact beats. V turns the banner red; "
                             "Q shows an uncertain/grey alert instead")
    parser.add_argument("--caution-rate", type=float, default=0.03,
                        help="Fraction of beats that are a single odd (amber) beat, so "
                             "the stream looks varied rather than uniformly green")
    args = parser.parse_args()

    print(f"Streaming {args.beats} beats for '{args.patient}'  ->  {args.url}")
    print(f"Artifact at beats {args.off_at}-{args.off_at + args.off_count - 1}. "
          f"Sign in as '{args.patient}'. Ctrl+C to stop.\n")

    for i in range(1, args.beats + 1):
        if args.off_at <= i < args.off_at + args.off_count:
            code, conf = args.off_code, random.uniform(0.74, 0.90)
            bpm, samples = random.uniform(120, 150), noisy_beat_window()
            flags, label = ["IRREG"], "OFF  (arm movement / artifact)"
        elif random.random() < args.caution_rate:
            # A lone ectopic beat: amber, never red, because one beat is not a run.
            code, conf = random.choice(["S", "F"]), random.uniform(0.62, 0.82)
            bpm, samples = random.uniform(70, 95), mild_beat_window()
            flags, label = [], "caution"
        else:
            code, conf = "N", random.uniform(0.90, 0.98)
            bpm, samples = random.uniform(68, 82), clean_beat_window()
            flags, label = [], "Normal"

        problem = post_beat(args.url, args.key, args.patient, code, conf, bpm, samples, flags)
        status = "ok" if not problem else "FAILED"
        print(f"  beat {i:>4}  {code}  conf={conf:.2f}  bpm={bpm:>3.0f}  {label}  {status}")

        if problem:
            # Stop at the first failure rather than streaming hundreds more.
            # Filming a dashboard that is quietly receiving nothing is the one
            # outcome this script exists to avoid.
            print(f"\n  {problem}")
            print("  Stopped - nothing is reaching the dashboard.")
            return

        time.sleep(args.delay)

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
