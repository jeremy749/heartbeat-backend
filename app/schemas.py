"""
Pydantic request/response models for the Heartbeat backend.

These define the shape of the data that flows:
    device + classifier  ->  POST /api/beats  ->  database  ->  frontend
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# Map the model's single-letter class codes to human-readable names.
CLASS_NAMES: Dict[str, str] = {
    "N": "Normal",
    "S": "Supraventricular",
    "V": "Ventricular",
    "F": "Fusion",
    "Q": "Unclassified",
}

# Which classes are considered abnormal / should raise a flag.
ABNORMAL_CLASSES = {"S", "V", "F"}


class BeatIn(BaseModel):
    """One classified beat, sent by realtime_classifier.py (or the device bridge)."""

    # Single-letter class code from the model, e.g. "N", "V".
    # Either `class_code` or `classification` may be supplied.
    class_code: Optional[str] = Field(default=None, description="Model class code, e.g. 'V'")
    classification: Optional[str] = Field(default=None, description="Human-readable class name")

    confidence: float = Field(..., ge=0.0, le=1.0, description="Top-class probability 0..1")
    probabilities: Optional[Dict[str, float]] = Field(
        default=None, description="Full probability distribution per class code"
    )

    # Optional device-side context.
    bpm: Optional[float] = Field(default=None, description="Heart rate at time of beat")
    flags: Optional[List[str]] = Field(
        default=None, description="Rule-based device flags, e.g. ['TACHY', 'PVC']"
    )
    samples: Optional[List[float]] = Field(
        default=None, description="Raw 200-sample beat window (optional, for waveform replay)"
    )
    recorded_at: Optional[datetime] = Field(
        default=None, description="Device timestamp; server fills in if absent"
    )
    patient: Optional[str] = Field(
        default=None, description="Patient/identity this beat belongs to"
    )


class BeatOut(BaseModel):
    """A stored beat as returned to the frontend."""

    id: int
    patient: str
    class_code: str
    classification: str
    confidence: float
    is_abnormal: bool
    alert_level: str
    alert_detail: str
    bpm: Optional[float] = None
    flags: List[str] = []
    probabilities: Dict[str, float] = {}
    recorded_at: datetime


class StatsOut(BaseModel):
    """Summary counts for the dashboard."""

    total_beats: int
    abnormal_beats: int
    counts_by_class: Dict[str, int]
    latest_bpm: Optional[float] = None
    latest_classification: Optional[str] = None
