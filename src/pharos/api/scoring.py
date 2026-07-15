"""Lazy anomaly scorer behind POST /score-track.

Loads (once, cached) the exact `SequenceAnomalyModel` artifact written by the detector. If no valid
artifact exists, it explicitly reports a runtime fallback trained from stored tracks or a synthetic
Singapore scenario so the endpoint still works on a fresh deploy. The route is read-only: it
scores the pasted track's shape and never fetches a URL or writes to the DB.
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.config import get_settings
from pharos.db.models import Position, Track
from pharos.detect.anomaly import normalized_scores
from pharos.detect.seq_anomaly import SequenceAnomalyModel, model_artifact_path
from pharos.logging import get_logger
from pharos.tracks.build import track_sequence

log = get_logger(__name__)
_MODEL: SequenceAnomalyModel | None = None
_MODEL_SOURCE = "uninitialized"


def _training_sequences(session: Session) -> np.ndarray:
    settings = get_settings()
    tracks = [
        t for t in session.scalars(select(Track).where(Track.sequence.isnot(None))) if t.sequence
    ]
    if len(tracks) >= 4:
        return np.array([t.sequence for t in tracks], dtype=np.float64)
    # Fallback: a synthetic scenario so a fresh deploy still scores.
    from pharos.ingest.synthetic import generate_scenario
    from pharos.tracks.build import segment

    sc = generate_scenario("singapore", seed=0, n_normal=14)
    by_vessel: dict[str, list[Position]] = {}
    for p in sc.positions:
        by_vessel.setdefault(p.mmsi, []).append(p)
    seqs = []
    for pts in by_vessel.values():
        for voyage in segment(pts, settings.track_gap_split_minutes):
            if len(voyage) >= 6:
                seqs.append(
                    track_sequence(sorted(voyage, key=lambda p: p.ts), settings.anomaly_seq_len)
                )
    return np.array(seqs, dtype=np.float64)


def get_scorer(session: Session, artifact_path: str | Path | None = None) -> SequenceAnomalyModel:
    global _MODEL, _MODEL_SOURCE
    if _MODEL is None:
        settings = get_settings()
        artifact = (
            Path(artifact_path)
            if artifact_path is not None
            else model_artifact_path(settings.anomaly_model_dir)
        )
        if artifact.is_file():
            try:
                _MODEL = SequenceAnomalyModel.load(artifact)
                _MODEL_SOURCE = "trained-artifact"
                log.info("anomaly_artifact_loaded", path=str(artifact))
                return _MODEL
            except (
                EOFError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                pickle.UnpicklingError,
            ) as exc:
                log.warning("anomaly_artifact_invalid", path=str(artifact), error=str(exc))
        x = _training_sequences(session)
        model = SequenceAnomalyModel(hidden=settings.anomaly_hidden, seed=0)
        model.fit(x)
        model.calibrate(x, settings.anomaly_threshold_pct)
        model.metadata = {"source": "runtime-fallback", "training_tracks": len(x)}
        _MODEL = model
        _MODEL_SOURCE = "runtime-fallback"
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
    seq = np.array([track_sequence(positions, settings.anomaly_seq_len)], dtype=np.float64)
    model = get_scorer(session)
    raw = float(model.score(seq)[0])
    threshold = model.threshold or 0.0
    norm = float(normalized_scores(np.array([raw]), threshold)[0])
    return {
        "anomaly_score": round(norm, 4),
        "reconstruction_error": round(raw, 6),
        "threshold": round(threshold, 6),
        "is_anomalous": bool(raw > threshold),
        "model_source": _MODEL_SOURCE,
        "points_scored": len(positions),
    }


def reset_scorer() -> None:
    """Drop the cached model (tests / after a re-ingest)."""
    global _MODEL, _MODEL_SOURCE
    _MODEL = None
    _MODEL_SOURCE = "uninitialized"
