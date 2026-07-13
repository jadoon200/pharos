"""The trajectory-anomaly model must rank the injected zig-zag above benign transits, transfer
across regions, and persist. AUC is threshold-free, so these checks don't depend on a cutoff."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.db.models import Incident
from pharos.detect.anomaly import (
    PCAAnomalyBaseline,
    TrajectoryAnomalyModel,
    detect_anomalies,
    normalized_scores,
)
from pharos.detect.backends import resolve_backend
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.synthetic import generate_scenario
from pharos.tracks.build import build_tracks, track_features


def _features(region: str, seed: int, seq_len: int = 16) -> tuple[np.ndarray, list[str]]:
    """Feature matrix + per-track event label for a scenario's voyages."""
    sc = generate_scenario(region, seed=seed, n_normal=14)
    from pharos.config import get_settings
    from pharos.tracks.build import segment

    gap = get_settings().track_gap_split_minutes
    by_vessel: dict[str, list] = {}
    for p in sc.positions:
        by_vessel.setdefault(p.mmsi, []).append(p)
    truth = {e.mmsi: e.event_type for e in sc.truth}
    feats, labels = [], []
    for mmsi, pts in by_vessel.items():
        for voyage in segment(pts, gap):
            if len(voyage) >= 6:
                feats.append(track_features(sorted(voyage, key=lambda p: p.ts), seq_len))
                labels.append(truth.get(mmsi, "normal"))
    return np.array(feats, dtype=np.float64), labels


def _auc_anomaly_vs_normal(scores: np.ndarray, labels: list[str]) -> float:
    """Rank-based ROC-AUC of the injected route-anomaly against benign transits only.

    The other injected events (loiter, rendezvous, spoof, gap) also reconstruct poorly — they
    are genuinely non-normal shapes owned by the deterministic detectors — so the anomaly model's
    isolated skill is measured against normal transits, not against them.
    """
    lab = np.array(labels)
    pos = scores[lab == "anomaly"]
    neg = scores[lab == "normal"]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return float(wins / (len(pos) * len(neg)))


def test_model_ranks_anomaly_highest() -> None:
    x, labels = _features("singapore", seed=0)
    model = TrajectoryAnomalyModel(hidden=64, seed=0)
    model.fit(x, epochs=40)
    scores = model.score(x)
    # The zig-zag anomaly should reconstruct far worse than benign transits.
    assert _auc_anomaly_vs_normal(scores, labels) >= 0.9


def test_cross_region_generalization() -> None:
    # Train pattern-of-life on Singapore, score US west coast — the headline.
    x_sg, _ = _features("singapore", seed=1)
    x_us, labels_us = _features("us-west", seed=1)
    model = TrajectoryAnomalyModel(hidden=64, seed=0)
    model.fit(x_sg, epochs=40)
    scores = model.score(x_us)
    # Shape-based, region-agnostic features → a model trained off Singapore still separates
    # the anomaly from normal transits off a different coast. (The eval reports the real number.)
    assert _auc_anomaly_vs_normal(scores, labels_us) >= 0.8


def test_pca_baseline_also_ranks_anomaly() -> None:
    x, labels = _features("singapore", seed=2)
    base = PCAAnomalyBaseline(n_components=4)
    base.fit(x)
    scores = base.score(x)
    assert _auc_anomaly_vs_normal(scores, labels) >= 0.8


def test_normalized_scores_semantics() -> None:
    scores = np.array([0.0, 1.0, 2.0])
    norm = normalized_scores(scores, threshold=1.0)
    assert norm[0] == pytest.approx(0.0)
    assert norm[1] == pytest.approx(0.5)
    assert norm[2] == pytest.approx(1.0)


def test_save_load_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    x, _ = _features("singapore", seed=0)
    model = TrajectoryAnomalyModel(hidden=32, seed=0)
    model.fit(x, epochs=10)
    model.calibrate(x, pct=95)
    path = tmp_path / "anom.pt"
    model.save(path)
    loaded = TrajectoryAnomalyModel.load(path)
    assert np.allclose(model.score(x), loaded.score(x), atol=1e-5)
    assert loaded.threshold == model.threshold


def test_resolve_backend_defaults_to_torch_without_mlx() -> None:
    # On CI/Linux (no mlx) every mode resolves to torch.
    assert resolve_backend("torch") == "torch"
    assert resolve_backend("auto") in {"torch", "mlx"}


def test_detect_anomalies_writes_incidents(session: Session) -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=14)
    persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    build_tracks(session, region="singapore")
    session.commit()
    stats = detect_anomalies(session, region="singapore")
    session.commit()
    assert stats["flagged"] >= 1
    n = session.scalar(
        select(func.count()).select_from(Incident).where(Incident.detector == "anomaly")
    )
    assert n == stats["flagged"]
