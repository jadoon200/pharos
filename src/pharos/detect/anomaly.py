"""Flagship trajectory-anomaly model — learned pattern-of-life over route shape.

The four deterministic detectors catch *specified* behaviours; this model catches the
*unspecified* one — a track whose shape doesn't look like the normal traffic. It is a small
autoencoder trained on benign track-shape descriptors (`tracks.build.track_features`: resampled,
translated, rotated-to-canonical, region-agnostic); a voyage the model reconstructs poorly is
anomalous.

Two properties make this the honest-eval centrepiece:

- **Cross-region generalization.** Because the features encode *shape* not *place* (a straight
  transit looks the same in the Singapore Strait and off California), a model trained on one
  waterway can be scored on another. That train-A / test-B AUC — the number that survives the
  region change — is the maritime analogue of SENTINEL's cross-network transfer result. The eval
  (`pharos.eval`) reports it against a within-region baseline.
- **Threshold-free headline.** AUC needs no operating point; the pipeline's incident flagging uses
  a benign-calibrated percentile threshold, kept separate from the metric.

Backend: torch (the portable default, used on Linux/CI). An MLX port (`anomaly_mlx.py`) is the
Apple-silicon path; the default is benchmark-gated in `docs/EVAL.md` (MLX benchmark pending).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import torch
from numpy.typing import NDArray
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from torch import nn

from pharos.config import get_settings
from pharos.db.base import session_scope
from pharos.db.models import Incident, Track
from pharos.detect.backends import resolve_backend
from pharos.detect.base import make_incident
from pharos.logging import configure_logging, get_logger

log = get_logger(__name__)


class _AutoEncoder(nn.Module):
    def __init__(self, input_dim: int, hidden: int) -> None:
        super().__init__()
        bottleneck = max(2, hidden // 4)
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, bottleneck),
        )
        self.decoder = nn.Sequential(
            nn.ReLU(),
            nn.Linear(bottleneck, hidden),
            nn.ReLU(),
            nn.Linear(hidden, input_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.decoder(self.encoder(x))  # type: ignore[no-any-return]


class TrajectoryAnomalyModel:
    """Autoencoder anomaly scorer over fixed-length track-shape features."""

    def __init__(self, hidden: int = 64, seed: int = 0) -> None:
        self.hidden = hidden
        self.seed = seed
        self._model: _AutoEncoder | None = None
        self._mean: NDArray[np.float64] | None = None
        self._std: NDArray[np.float64] | None = None
        self.threshold: float | None = None
        self.input_dim: int | None = None

    # --- fit / score -------------------------------------------------------------------
    def _standardize(self, x: NDArray[np.float64]) -> NDArray[np.float64]:
        assert self._mean is not None and self._std is not None
        return (x - self._mean) / self._std

    def fit(self, features: NDArray[np.float64], epochs: int = 20, lr: float = 1e-2) -> None:
        torch.manual_seed(self.seed)
        np.random.seed(self.seed)
        x = np.asarray(features, dtype=np.float64)
        self.input_dim = x.shape[1]
        self._mean = x.mean(axis=0)
        self._std = x.std(axis=0) + 1e-6
        xs = torch.tensor(self._standardize(x), dtype=torch.float32)

        self._model = _AutoEncoder(self.input_dim, self.hidden)
        opt = torch.optim.Adam(self._model.parameters(), lr=lr)
        loss_fn = nn.MSELoss()
        self._model.train()
        for _ in range(epochs):
            opt.zero_grad()
            out = self._model(xs)
            loss = loss_fn(out, xs)
            loss.backward()
            opt.step()

    def score(self, features: NDArray[np.float64]) -> NDArray[np.float64]:
        """Per-row reconstruction error (higher = more anomalous)."""
        assert self._model is not None, "fit() first"
        x = np.asarray(features, dtype=np.float64)
        xs = torch.tensor(self._standardize(x), dtype=torch.float32)
        self._model.eval()
        with torch.no_grad():
            out = self._model(xs)
            err = ((out - xs) ** 2).mean(dim=1).cpu().numpy()
        result: NDArray[np.float64] = err.astype(np.float64)
        return result

    def calibrate(self, benign_features: NDArray[np.float64], pct: float) -> float:
        """Set the flagging threshold at the `pct` percentile of benign reconstruction error."""
        self.threshold = float(np.percentile(self.score(benign_features), pct))
        return self.threshold

    def flag(self, features: NDArray[np.float64]) -> NDArray[np.bool_]:
        assert self.threshold is not None, "calibrate() first"
        return self.score(features) > self.threshold

    # --- persistence -------------------------------------------------------------------
    def save(self, path: str | Path) -> None:
        assert self._model is not None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "state_dict": self._model.state_dict(),
            "mean": self._mean,
            "std": self._std,
            "threshold": self.threshold,
            "input_dim": self.input_dim,
            "hidden": self.hidden,
            "seed": self.seed,
        }
        torch.save(payload, path)

    @classmethod
    def load(cls, path: str | Path) -> TrajectoryAnomalyModel:
        payload = torch.load(path, weights_only=False)
        model = cls(hidden=payload["hidden"], seed=payload["seed"])
        model.input_dim = payload["input_dim"]
        model._mean = payload["mean"]
        model._std = payload["std"]
        model.threshold = payload["threshold"]
        model._model = _AutoEncoder(model.input_dim, model.hidden)
        model._model.load_state_dict(payload["state_dict"])
        return model


def normalized_scores(scores: NDArray[np.float64], threshold: float) -> NDArray[np.float64]:
    """Map raw reconstruction error to a [0,1] incident score (0.5 at threshold, saturating)."""
    ratio = scores / (threshold + 1e-12)
    return np.clip(0.5 * ratio, 0.0, 1.0)


class PCAAnomalyBaseline:
    """A linear PCA-reconstruction baseline — the honest comparison for the autoencoder.

    Fit PCA on benign features, reconstruct, and score by reconstruction error. If the small
    autoencoder can't beat this, the eval says so (`docs/EVAL.md`) — the SENTINEL discipline of
    reporting the number that survives a fair baseline.
    """

    def __init__(self, n_components: int = 4) -> None:
        self.n_components = n_components
        self._pca: Any = None
        self._mean: NDArray[np.float64] | None = None
        self._std: NDArray[np.float64] | None = None
        self.threshold: float | None = None

    def fit(self, features: NDArray[np.float64]) -> None:
        from sklearn.decomposition import PCA

        x = np.asarray(features, dtype=np.float64)
        self._mean = x.mean(axis=0)
        self._std = x.std(axis=0) + 1e-6
        xs = (x - self._mean) / self._std
        n = min(self.n_components, xs.shape[1], max(1, xs.shape[0] - 1))
        self._pca = PCA(n_components=n, random_state=0).fit(xs)

    def score(self, features: NDArray[np.float64]) -> NDArray[np.float64]:
        assert self._pca is not None and self._mean is not None and self._std is not None
        xs = (np.asarray(features, dtype=np.float64) - self._mean) / self._std
        recon = self._pca.inverse_transform(self._pca.transform(xs))
        return np.mean((xs - recon) ** 2, axis=1).astype(np.float64)

    def calibrate(self, benign_features: NDArray[np.float64], pct: float) -> float:
        self.threshold = float(np.percentile(self.score(benign_features), pct))
        return self.threshold

    def flag(self, features: NDArray[np.float64]) -> NDArray[np.bool_]:
        assert self.threshold is not None
        return self.score(features) > self.threshold


def _build_model(backend: str, hidden: int, seed: int) -> Any:
    if backend == "mlx":
        from pharos.detect.anomaly_mlx import MLXTrajectoryAnomalyModel

        return MLXTrajectoryAnomalyModel(hidden=hidden, seed=seed)
    return TrajectoryAnomalyModel(hidden=hidden, seed=seed)


def detect_anomalies(
    session: Session, region: str | None = None, train_region: str | None = None
) -> dict[str, int | str]:
    """Train the anomaly model and rebuild its incidents for `region`.

    Trains on `train_region` (or `region` itself) and flags voyages the model reconstructs
    worst. Rebuilds only the `detector="anomaly"` incidents, leaving the deterministic
    detectors' rows intact (they are fused by the ensemble). Skips gracefully with too few tracks.
    """
    settings = get_settings()
    backend = resolve_backend(settings.anomaly_backend)

    def _load(reg: str | None) -> list[Track]:
        q = select(Track).where(Track.features.isnot(None))
        if reg is not None:
            q = q.where(Track.region == reg)
        return [t for t in session.scalars(q) if t.features]

    train_tracks = _load(train_region or region)
    score_tracks = _load(region)
    if len(train_tracks) < 4 or not score_tracks:
        log.info("anomaly_skip", reason="too few tracks", train=len(train_tracks))
        return {"total": 0, "flagged": 0, "backend": backend, "scored": len(score_tracks)}

    x_train = np.array([t.features for t in train_tracks], dtype=np.float64)
    x_score = np.array([t.features for t in score_tracks], dtype=np.float64)

    model = _build_model(backend, settings.anomaly_hidden, seed=0)
    model.fit(x_train, epochs=settings.anomaly_epochs)
    threshold = model.calibrate(x_train, settings.anomaly_threshold_pct)
    scores = model.score(x_score)
    norm = normalized_scores(scores, threshold)

    clear = delete(Incident).where(Incident.detector == "anomaly")
    if region is not None:
        clear = clear.where(Incident.region == region)
    session.execute(clear)

    flagged = 0
    for track, raw, sc in zip(score_tracks, scores, norm, strict=True):
        if raw <= threshold:
            continue
        flagged += 1
        session.add(
            make_incident(
                detector="anomaly",
                incident_type="trajectory anomaly",
                mmsi=track.mmsi,
                score=float(sc),
                confidence=0.45,  # unsupervised → moderate reliability by design
                ts_start=track.start_ts,
                ts_end=track.end_ts,
                lat=track.start_lat,
                lon=track.start_lon,
                region=track.region,
                track_id=track.track_id,
                techniques=["trajectory-anomaly", "pattern-of-life-deviation"],
                evidence={
                    "reconstruction_error": round(float(raw), 5),
                    "threshold": round(threshold, 5),
                    "backend": backend,
                },
            )
        )
    log.info("anomaly_complete", region=region, backend=backend, flagged=flagged)
    return {"total": flagged, "flagged": flagged, "backend": backend, "scored": len(score_tracks)}


def main() -> None:
    configure_logging()
    with session_scope() as session:
        detect_anomalies(session)


if __name__ == "__main__":
    main()
