"""
Server-side alert criteria — the single source of truth.

This mirrors the frontend's alerts.js, but running it here means the alert level
is computed once, stored with each beat, and returned to every client (dashboard,
history export, a future mobile app) identically. The frontend now displays this
value rather than recomputing it.

Levels, most to least severe: red, amber, green, uncertain.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

THRESHOLDS = {
    "window": 10,                       # how many recent beats to consider
    "min_confidence": 0.6,             # below this a reading is "uncertain"
    "v_confidence_red": 0.7,           # a single confident Ventricular beat -> red
    "v_count_red_in_window": 3,        # this many V beats in the window -> red
    "abnormal_confidence_amber": 0.6,  # a confident abnormal beat -> at least amber
    "abnormal_count_amber_in_window": 2,
}

ABNORMAL_CLASSES = {"Ventricular", "Supraventricular", "Fusion"}


def evaluate_alert(
    classification: str,
    confidence: float,
    flags: Optional[List[str]],
    previous_beats: Optional[List[dict]] = None,
    thresholds: Dict = THRESHOLDS,
) -> Tuple[str, str]:
    """Return (level, detail) for a beat.

    `previous_beats` are the most recent already-stored beats (newest first);
    the current beat is included in the window so runs are counted correctly.
    """
    t = thresholds
    flags = flags or []
    previous_beats = previous_beats or []

    # Window includes the current beat, like the frontend's history[0].
    window = ([{"classification": classification}] + previous_beats)[: t["window"]]
    v_count = sum(1 for b in window if b.get("classification") == "Ventricular")
    abnormal_count = sum(1 for b in window if b.get("classification") in ABNORMAL_CLASSES)

    if classification == "Unclassified" or confidence < t["min_confidence"]:
        return "uncertain", "Low-confidence reading - not a diagnosis."

    if classification == "Ventricular" and confidence >= t["v_confidence_red"]:
        return "red", f"Confident ventricular beat ({round(confidence * 100)}%)."
    if v_count >= t["v_count_red_in_window"]:
        return "red", f"{v_count} ventricular beats in the last {len(window)}."
    if ("TACHY" in flags or "BRADY" in flags) and classification in ABNORMAL_CLASSES:
        return "red", "Abnormal rhythm with an abnormal heart rate."

    if (
        (classification in ABNORMAL_CLASSES and confidence >= t["abnormal_confidence_amber"])
        or abnormal_count >= t["abnormal_count_amber_in_window"]
        or "IRREG" in flags
    ):
        return "amber", f"{classification} beat detected - keep watching."

    return "green", "Rhythm within normal limits."
