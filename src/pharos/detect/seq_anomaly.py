"""Sequence trajectory-anomaly model — a GRU autoencoder over the *ordered* track.

The flagship anomaly model. Rather than a bag of coordinates, it consumes the track as a
**sequence** of per-step motion (`tracks.build.track_sequence`: `[dx, dy, step_len, turn]`) and
reconstructs it with a recurrent encoder-decoder — so it models pattern-of-life as *dynamics* (a
straight run, a bend, a detour that returns), which is what separates a subtle anomalous excursion
from a legitimate single manoeuvre. The eval keeps nonlinear Isolation Forest and linear PCA
baselines (`anomaly.py`) for a fair comparison; the recurrent model beats both under unsupervised
training (`docs/EVAL.md`).

Trained the honest way for the operational setting: an unlabeled track population (which can
contain anomalies) with a held-out **validation split**, **early stopping** on validation loss
(best weights restored), and a recorded **learning curve** — not a fixed epoch count on the whole
set. Anomaly score = per-track reconstruction error. Torch backend (CPU is plenty at this data
scale; fast training here is expected, not a shortcut — the honest signal is the val-loss curve
and a sub-1.0 AUC, both reported).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import torch
from numpy.typing import NDArray
from torch import nn


@dataclass
class TrainHistory:
    train_loss: list[float] = field(default_factory=list)
    val_loss: list[float] = field(default_factory=list)
    best_epoch: int = 0
    stopped_epoch: int = 0


class _SeqAE(nn.Module):
    def __init__(self, n_features: int, hidden: int) -> None:
        super().__init__()
        self.hidden = hidden
        self.encoder = nn.GRU(n_features, hidden, batch_first=True)
        self.decoder = nn.GRU(hidden, hidden, batch_first=True)
        self.out = nn.Linear(hidden, n_features)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, h = self.encoder(x)  # h: (1, B, H) — the encoded context
        length = x.shape[1]
        z = h[-1].unsqueeze(1).repeat(1, length, 1)  # broadcast context over the sequence
        decoded, _ = self.decoder(z)
        return self.out(decoded)  # type: ignore[no-any-return]


class SequenceAnomalyModel:
    """GRU-autoencoder anomaly scorer over per-step track sequences."""

    def __init__(self, hidden: int = 8, seed: int = 0) -> None:
        self.hidden = hidden
        self.seed = seed
        self._model: _SeqAE | None = None
        self._mean: NDArray[np.float64] | None = None
        self._std: NDArray[np.float64] | None = None
        self.threshold: float | None = None
        self.n_features: int | None = None
        self.history = TrainHistory()

    def _standardize(self, x: NDArray[np.float64]) -> NDArray[np.float64]:
        assert self._mean is not None and self._std is not None
        return (x - self._mean) / self._std

    @property
    def parameter_count(self) -> int:
        """Number of trainable parameters in the fitted network."""
        assert self._model is not None, "fit() first"
        return sum(parameter.numel() for parameter in self._model.parameters())

    def fit(
        self,
        sequences: NDArray[np.float64],
        epochs: int = 300,
        lr: float = 5e-3,
        val_frac: float = 0.25,
        patience: int = 25,
    ) -> TrainHistory:
        """Train on unlabeled sequences (N, L, C) with a val split + early stopping."""
        torch.manual_seed(self.seed)
        rng = np.random.default_rng(self.seed)
        x = np.asarray(sequences, dtype=np.float64)
        n, _length, self.n_features = x.shape
        # Per-channel standardization over all steps.
        flat = x.reshape(-1, self.n_features)
        self._mean = flat.mean(axis=0)
        self._std = flat.std(axis=0) + 1e-6
        xs = torch.tensor(self._standardize(x), dtype=torch.float32)

        idx = rng.permutation(n)
        n_val = max(1, int(n * val_frac)) if n >= 4 else 0
        val_idx = torch.tensor(idx[:n_val])
        train_idx = torch.tensor(idx[n_val:]) if n_val else torch.tensor(idx)
        x_train = xs[train_idx]
        x_val = xs[val_idx] if n_val else xs[train_idx]

        self._model = _SeqAE(self.n_features, self.hidden)
        opt = torch.optim.Adam(self._model.parameters(), lr=lr)
        loss_fn = nn.MSELoss()

        best_val = float("inf")
        best_state: dict[str, Any] | None = None
        wait = 0
        self.history = TrainHistory()
        for epoch in range(epochs):
            self._model.train()
            opt.zero_grad()
            loss = loss_fn(self._model(x_train), x_train)
            loss.backward()
            opt.step()

            self._model.eval()
            with torch.no_grad():
                vloss = float(loss_fn(self._model(x_val), x_val).item())
            self.history.train_loss.append(float(loss.item()))
            self.history.val_loss.append(vloss)

            if vloss < best_val - 1e-6:
                best_val = vloss
                best_state = {k: v.clone() for k, v in self._model.state_dict().items()}
                self.history.best_epoch = epoch
                wait = 0
            else:
                wait += 1
                if wait >= patience:
                    self.history.stopped_epoch = epoch
                    break
        if best_state is not None:
            self._model.load_state_dict(best_state)  # restore best-val weights
        if not self.history.stopped_epoch:
            self.history.stopped_epoch = len(self.history.val_loss) - 1
        return self.history

    def score(self, sequences: NDArray[np.float64]) -> NDArray[np.float64]:
        assert self._model is not None, "fit() first"
        x = np.asarray(sequences, dtype=np.float64)
        xs = torch.tensor(self._standardize(x), dtype=torch.float32)
        self._model.eval()
        with torch.no_grad():
            recon = self._model(xs)
            err = ((recon - xs) ** 2).mean(dim=(1, 2)).cpu().numpy()
        out: NDArray[np.float64] = err.astype(np.float64)
        return out

    def calibrate(self, benign_sequences: NDArray[np.float64], pct: float) -> float:
        self.threshold = float(np.percentile(self.score(benign_sequences), pct))
        return self.threshold

    def flag(self, sequences: NDArray[np.float64]) -> NDArray[np.bool_]:
        assert self.threshold is not None, "calibrate() first"
        return self.score(sequences) > self.threshold

    def save(self, path: str | Path) -> None:
        assert self._model is not None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "state_dict": self._model.state_dict(),
                "mean": self._mean,
                "std": self._std,
                "threshold": self.threshold,
                "n_features": self.n_features,
                "hidden": self.hidden,
                "seed": self.seed,
            },
            path,
        )

    @classmethod
    def load(cls, path: str | Path) -> SequenceAnomalyModel:
        payload = torch.load(path, weights_only=False)
        m = cls(hidden=payload["hidden"], seed=payload["seed"])
        m.n_features = payload["n_features"]
        m._mean = payload["mean"]
        m._std = payload["std"]
        m.threshold = payload["threshold"]
        m._model = _SeqAE(m.n_features, m.hidden)
        m._model.load_state_dict(payload["state_dict"])
        return m
