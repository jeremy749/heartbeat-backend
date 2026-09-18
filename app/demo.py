"""Seed the demo account with a plausible recording.

Why this exists: on a free host the filesystem is usually ephemeral, so the
database is empty again after every redeploy and every cold start. Anyone
opening the link - a judge, a teacher, someone you sent it to - would land on an
empty dashboard and no amount of explaining fixes a first impression.

So when SEED_DEMO is set and the demo account has no readings, the server
generates a few hours of them at startup. The rows go through the same alert
engine and the same table as real beats; nothing about them is special except
that a function rather than a heart produced them.
"""

from __future__ import annotations

import math
import os
import random
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from . import alerts
from . import database as db
from .schemas import ABNORMAL_CLASSES, CLASS_NAMES

# Roughly six hours of readings at a beat every 30 seconds. Enough to fill the
# trends charts and page the history table without making a cold start crawl.
DEMO_BEATS = int(os.environ.get("DEMO_BEATS", "720"))
DEMO_INTERVAL_SECONDS = 30
# Only recent beats carry a waveform, matching what retention would have left.
DEMO_WAVEFORM_BEATS = 40


def _beat_window(code: str, n: int = 200) -> List[float]:
    """A synthetic 200-sample beat: P wave, QRS complex, T wave.

    A ventricular beat gets the wide, inverted QRS that makes it look different
    from a normal one on the strip, because that difference is the whole point
    of showing a waveform at all.
    """
    wide = code == "V"
    out = []
    for i in range(n):
        t = i / n
        baseline = 0.05 * math.sin(2 * math.pi * t * 2)
        p_wave = 0.12 * math.exp(-((t - 0.25) ** 2) / 0.0010)
        qrs = (-0.6 if wide else 1.0) * math.exp(
            -((t - 0.45) ** 2) / (0.0016 if wide else 0.0004)
        )
        t_wave = 0.25 * math.exp(-((t - 0.65) ** 2) / 0.0040)
        out.append(round(baseline + p_wave + qrs + t_wave + random.uniform(-0.02, 0.02), 4))
    return out


def _class_sequence(count: int, rng: random.Random) -> List[str]:
    """Mostly normal, with occasional runs of ectopic beats.

    Drawn as runs rather than independently per beat: real arrhythmias arrive in
    clusters, and a run is what makes the alert engine escalate, so independent
    draws would produce a dashboard that never shows anything interesting.

    The ectopic rate here (a few percent) is higher than a healthy resting
    recording and deliberately so - at a clinically typical rate the abnormal
    filter and the class chart are empty, and a demo that shows none of the
    features it was built for demonstrates nothing.
    """
    codes: List[str] = []
    while len(codes) < count:
        roll = rng.random()
        if roll < 0.72:
            codes.extend(["N"] * rng.randint(12, 30))
        elif roll < 0.86:
            codes.extend(["V"] * rng.randint(2, 5))       # a short ventricular run
        elif roll < 0.94:
            codes.append("S")                              # a lone atrial beat
        elif roll < 0.98:
            codes.append("F")                              # a fusion beat
        else:
            codes.append("Q")                              # unclassifiable
    return codes[:count]


def build_beats(
    user_id: int,
    patient: str,
    count: int = DEMO_BEATS,
    end: Optional[datetime] = None,
    seed: int = 20260918,
) -> List[dict]:
    """Build the rows for a demo recording, ending now and working backwards.

    Deterministic for a given seed, so a redeploy produces the same recording
    and the demo does not change shape under anyone who is mid-look.
    """
    rng = random.Random(seed)
    end = end or datetime.now(timezone.utc)
    codes = _class_sequence(count, rng)

    rows: List[dict] = []
    recent: List[dict] = []  # newest first, for the alert window
    for i, code in enumerate(codes):
        recorded_at = end - timedelta(seconds=DEMO_INTERVAL_SECONDS * (count - 1 - i))

        # A resting heart rate that drifts over the session, faster during an
        # ectopic run, plus a little beat-to-beat noise so HRV is not flat.
        drift = 6 * math.sin(i / 90)
        bpm = 68 + drift + rng.uniform(-3, 3) + (14 if code == "V" else 0)
        bpm = round(max(45, min(150, bpm)), 1)

        confidence = round(rng.uniform(0.88, 0.99) if code == "N" else rng.uniform(0.62, 0.95), 2)
        if code == "Q":
            confidence = round(rng.uniform(0.30, 0.55), 2)

        name = CLASS_NAMES.get(code, "Unclassified")
        flags = []
        if bpm > 100:
            flags.append("TACHY")
        elif bpm < 55:
            flags.append("BRADY")

        level, detail = alerts.evaluate_alert(name, confidence, flags, recent)

        probabilities = {c: round(rng.uniform(0.0, 0.2), 2) for c in CLASS_NAMES}
        probabilities[code] = confidence

        rows.append({
            "user_id": user_id,
            "patient": patient,
            "class_code": code,
            "classification": name,
            "confidence": confidence,
            "is_abnormal": code in ABNORMAL_CLASSES,
            "alert_level": level,
            "alert_detail": detail,
            "bpm": bpm,
            "flags": flags,
            "probabilities": probabilities,
            # Waveforms only on the newest beats, which is all retention would
            # have kept anyway.
            "samples": _beat_window(code) if i >= count - DEMO_WAVEFORM_BEATS else None,
            "recorded_at": recorded_at,
        })

        recent.insert(0, {"classification": name})
        del recent[alerts.THRESHOLDS["window"]:]

    return rows


def seed_demo_data(force: bool = False) -> int:
    """Give the demo account a recording if it has none. Returns rows written.

    Idempotent: with data already there it does nothing, so a host that *does*
    keep its disk will not accumulate a fresh six hours on every restart.
    """
    user = db.get_or_create_user(db.DEFAULT_USER)
    if not force and db.count_beats(user["id"]) > 0:
        return 0
    return db.insert_beats(build_beats(user["id"], user["name"]))
