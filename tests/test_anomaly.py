"""The flagship GRU sequence-AE must separate anomalies from benign traffic under UNSUPERVISED
training (no labels), transfer across regions, and beat the flattened/linear baselines — which is
the honest finding that the recurrent depth is necessary, not decorative. AUC is threshold-free."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.config import get_settings
from pharos.db.models import Incident
from pharos.detect.anomaly import (
    PCAAnomalyBaseline,
    TrajectoryAnomalyModel,
    detect_anomalies,
    normalized_scores,
)
from pharos.detect.backends import resolve_backend
from pharos.detect.seq_anomaly import SequenceAnomalyModel
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.synthetic import generate_scenario
from pharos.tracks.build import build_tracks, segment, track_features, track_sequence


def _voyages(region: str, seed: int):  # type: ignore[no-untyped-def]
    """Per-voyage sequences, flattened features, and labels for a scenario."""
    sc = generate_scenario(region, seed=seed, n_normal=16)
    seq_len = get_settings().anomaly_seq_len
    gap = get_settings().track_gap_split_minutes
    by_vessel: dict[str, list] = {}
    for p in sc.positions:
        by_vessel.setdefault(p.mmsi, []).append(p)
    truth = {e.mmsi: e.event_type for e in sc.truth}
    seqs, feats, labels = [], [], []
    for mmsi, pts in by_vessel.items():
        for voyage in segment(pts, gap):
            if len(voyage) >= 6:
                vs = sorted(voyage, key=lambda p: p.ts)
                seqs.append(track_sequence(vs, seq_len))
                feats.append(track_features(vs, seq_len))
                labels.append(truth.get(mmsi, "normal"))
    return np.array(seqs), np.array(feats, dtype=np.float64), labels


def _auc(scores: np.ndarray, labels: list[str]) -> float:
    """Rank-based ROC-AUC of the injected route-anomaly against benign transits only."""
    lab = np.array(labels)
    pos, neg = scores[lab == "anomaly"], scores[lab == "normal"]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return float(wins / (len(pos) * len(neg)))


def test_gru_separates_anomalies_unsupervised() -> None:
    # Trained on ALL tracks (no labels — the honest operational setup), the GRU still ranks the
    # subtle detours above benign transits + benign manoeuvres.
    s, _f, lab = _voyages("singapore", seed=0)
    g = SequenceAnomalyModel(hidden=64, seed=0)
    g.fit(s)
    assert _auc(g.score(s), lab) >= 0.85
    assert g.history.best_epoch >= 0 and g.history.val_loss  # a real train/val curve exists


def test_gru_cross_region_transfer() -> None:
    s_sg, _f, _l = _voyages("singapore", seed=1)
    s_us, _fu, lab_us = _voyages("us-west", seed=1)
    g = SequenceAnomalyModel(hidden=64, seed=0)
    g.fit(s_sg)  # train Singapore, score US west coast
    assert _auc(g.score(s_us), lab_us) >= 0.8


def test_gru_beats_baselines_unsupervised() -> None:
    # The honest depth-matters finding: under unsupervised training on the hard set, the flattened
    # MLP-AE and linear PCA fall APART (below chance), while the GRU holds up.
    s, f, lab = _voyages("singapore", seed=2)
    gru = SequenceAnomalyModel(hidden=64, seed=0)
    gru.fit(s)
    mlp = TrajectoryAnomalyModel(hidden=64, seed=0)
    mlp.fit(f, epochs=80)
    pca = PCAAnomalyBaseline(n_components=6)
    pca.fit(f)
    gru_auc = _auc(gru.score(s), lab)
    assert gru_auc >= 0.85
    assert gru_auc > _auc(mlp.score(f), lab)  # GRU beats the flattened baseline
    assert gru_auc > _auc(pca.score(f), lab)  # GRU beats the linear baseline


def test_normalized_scores_semantics() -> None:
    norm = normalized_scores(np.array([0.0, 1.0, 2.0]), threshold=1.0)
    assert norm[0] == pytest.approx(0.0)
    assert norm[1] == pytest.approx(0.5)
    assert norm[2] == pytest.approx(1.0)


def test_gru_save_load_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    s, _f, _l = _voyages("singapore", seed=0)
    g = SequenceAnomalyModel(hidden=32, seed=0)
    g.fit(s)
    g.calibrate(s, pct=95)
    path = tmp_path / "gru.pt"
    g.save(path)
    loaded = SequenceAnomalyModel.load(path)
    assert np.allclose(g.score(s), loaded.score(s), atol=1e-4)
    assert loaded.threshold == g.threshold


def test_resolve_backend_defaults_to_torch_without_mlx() -> None:
    assert resolve_backend("torch") == "torch"
    assert resolve_backend("auto") in {"torch", "mlx"}


def test_detect_anomalies_writes_incidents(session: Session) -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=16)
    persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    build_tracks(session, region="singapore")
    session.commit()
    stats = detect_anomalies(session, region="singapore")
    session.commit()
    assert stats["flagged"] >= 1
    assert stats["model"] == "gru-seq-ae"
    n = session.scalar(
        select(func.count()).select_from(Incident).where(Incident.detector == "anomaly")
    )
    assert n == stats["flagged"]
