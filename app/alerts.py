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
    is_abnormal = classification in ABNORMAL_CLASSES

    if classification == "Unclassified" or confidence < t["min_confidence"]:
        return "uncertain", "Low-confidence reading - not a diagnosis."

    # Red/amber escalation only when THIS beat is itself abnormal. A confident
    # normal beat must never read as "Urgent" just because of earlier beats.
    if is_abnormal:
        if classification == "Ventricular" and confidence >= t["v_confidence_red"]:
            return "red", f"Confident ventricular beat ({round(confidence * 100)}%)."
        if v_count >= t["v_count_red_in_window"]:
            return "red", f"Ventricular run - {v_count} of the last {len(window)} beats."
        if "TACHY" in flags or "BRADY" in flags:
            return "red", "Abnormal rhythm with an abnormal heart rate."
        return "amber", f"{classification} beat detected - keep watching."

    # Current beat is normal and confident. At most a gentle caution if the
    # recent run has been abnormal, but never red.
    if abnormal_count >= t["abnormal_count_amber_in_window"]:
        return "amber", f"Recent abnormal beats - {abnormal_count} in the last {len(window)}."
    if "IRREG" in flags:
        return "amber", "Irregular rhythm detected."
    return "green", "Rhythm within normal limits."
