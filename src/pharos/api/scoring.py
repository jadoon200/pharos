"""Lazy anomaly scorer behind POST /score-track.

Builds (once, cached) a `TrajectoryAnomalyModel` from whatever tracks the database holds, falling
back to a synthetic Singapore scenario so the endpoint works out of the box on a fresh deploy. The
route is read-only: it scores the pasted track's shape and never fetches a URL or writes to the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.config import get_settings
from pharos.db.models import Position, Track
from pharos.detect.anomaly import TrajectoryAnomalyModel, normalized_scores
from pharos.tracks.build import track_features

_MODEL: TrajectoryAnomalyModel | None = None


def _training_features(session: Session) -> np.ndarray:
    settings = get_settings()
    tracks = [
        t for t in session.scalars(select(Track).where(Track.features.isnot(None))) if t.features
    ]
    if len(tracks) >= 4:
        return np.array([t.features for t in tracks], dtype=np.float64)
    # Fallback: a synthetic scenario so a fresh deploy still scores.
    from pharos.eval.metrics import scenario_features
    from pharos.ingest.synthetic import generate_scenario

    sc = generate_scenario("singapore", seed=0, n_normal=14)
    x, _ = scenario_features(sc, settings.anomaly_seq_len, settings.track_gap_split_minutes)
    return x


def get_scorer(session: Session) -> TrajectoryAnomalyModel:
    global _MODEL
    if _MODEL is None:
        settings = get_settings()
        x = _training_features(session)
        model = TrajectoryAnomalyModel(hidden=settings.anomaly_hidden, seed=0)
        model.fit(x, epochs=settings.anomaly_epochs)
        model.calibrate(x, settings.anomaly_threshold_pct)
        _MODEL = model
    return _MODEL


def _parse_ts(value: Any, i: int) -> datetime:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    # Fall back to a synthetic 5-min cadence if timestamps are absent/unparseable.
    return datetime.fromtimestamp(i * 300.0, tz=UTC)


def score_track_points(session: Session, points: list[dict[str, Any]]) -> dict[str, Any]:
    """Score a pasted track's shape. `points` = [{lat, lon, ts?, sog?}, ...] (>= 6 points)."""
    if len(points) < 6:
        return {"error": "need at least 6 points to score a track shape"}
    settings = get_settings()
    positions = [
        Position(
            mmsi="query",
            ts=_parse_ts(p.get("ts"), i),
            lat=float(p["lat"]),
            lon=float(p["lon"]),
            sog=float(p["sog"]) if p.get("sog") is not None else None,
        )
        for i, p in enumerate(points)
    ]
    positions.sort(key=lambda p: p.ts)
    feats = np.array([track_features(positions, settings.anomaly_seq_len)], dtype=np.float64)
    model = get_scorer(session)
    raw = float(model.score(feats)[0])
    threshold = model.threshold or 0.0
    norm = float(normalized_scores(np.array([raw]), threshold)[0])
    return {
        "anomaly_score": round(norm, 4),
        "reconstruction_error": round(raw, 6),
        "threshold": round(threshold, 6),
        "is_anomalous": bool(raw > threshold),
        "points_scored": len(positions),
    }


def reset_scorer() -> None:
    """Drop the cached model (tests / after a re-ingest)."""
    global _MODEL
    _MODEL = None
