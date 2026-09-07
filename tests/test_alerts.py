"""The alert engine, exercised directly."""

from __future__ import annotations

import pytest

from app.alerts import THRESHOLDS, evaluate_alert


def beats(*names):
    """Recent beats, newest first, as evaluate_alert expects them."""
    return [{"classification": n} for n in names]


def test_confident_normal_beat_is_green():
    level, _ = evaluate_alert("Normal", 0.95, [], [])
    assert level == "green"


def test_low_confidence_is_uncertain_whatever_the_class():
    level, _ = evaluate_alert("Ventricular", 0.3, [], [])
    assert level == "uncertain"


def test_unclassified_is_uncertain_even_when_confident():
    level, _ = evaluate_alert("Unclassified", 0.99, [], [])
    assert level == "uncertain"


def test_confident_ventricular_beat_is_red():
    level, detail = evaluate_alert("Ventricular", 0.9, [], [])
    assert level == "red"
    assert "ventricular" in detail.lower()


def test_a_run_of_ventricular_beats_is_red():
    """Below the single-beat confidence bar, but the run itself escalates."""
    conf = THRESHOLDS["v_confidence_red"] - 0.05
    level, _ = evaluate_alert("Ventricular", conf, [], beats("Ventricular", "Ventricular"))
    assert level == "red"


def test_a_lone_supraventricular_beat_is_amber():
    level, _ = evaluate_alert("Supraventricular", 0.9, [], [])
    assert level == "amber"


def test_abnormal_beat_with_an_abnormal_rate_is_red():
    level, _ = evaluate_alert("Supraventricular", 0.9, ["TACHY"], [])
    assert level == "red"


def test_a_normal_beat_is_never_red_because_of_history():
    """A confident normal beat must not inherit an earlier run's severity."""
    level, _ = evaluate_alert(
        "Normal", 0.99, [], beats("Ventricular", "Ventricular", "Ventricular")
    )
    assert level == "amber"


def test_recent_abnormal_beats_soften_a_normal_beat_to_amber():
    level, _ = evaluate_alert("Normal", 0.95, [], beats("Ventricular", "Fusion"))
    assert level == "amber"


def test_irregular_flag_on_a_normal_beat_is_amber():
    level, _ = evaluate_alert("Normal", 0.95, ["IRREG"], [])
    assert level == "amber"


@pytest.mark.parametrize("level", ["red", "amber", "green", "uncertain"])
def test_every_level_carries_an_explanation(level):
    """The detail string is shown to the user, so it must never be empty."""
    cases = {
        "red": ("Ventricular", 0.95, [], []),
        "amber": ("Supraventricular", 0.9, [], []),
        "green": ("Normal", 0.95, [], []),
        "uncertain": ("Normal", 0.1, [], []),
    }
    got, detail = evaluate_alert(*cases[level])
    assert got == level
    assert detail.strip()
