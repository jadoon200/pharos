"""MLX port of the trajectory-anomaly autoencoder — the Apple-silicon-native backend.

Mirrors `anomaly.TrajectoryAnomalyModel` (same features, same standardize → autoencoder →
reconstruction-error scoring) using Apple's MLX so training runs on the M-series GPU. It is a
darwin-only optional path (the `mlx` extra); the torch model is the portable default and the
benchmark that would make MLX the default is recorded in `docs/EVAL.md` (pending).

Imported lazily and guarded so a machine without MLX never breaks: `mlx_backend_ready()` gates it,
and `resolve_backend` in `backends.py` only routes here when MLX is genuinely available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:  # keep import-time light; mlx is optional
    pass


def mlx_backend_ready() -> bool:
    try:
        import mlx.core
        import mlx.nn  # noqa: F401

        return True
    except ImportError:
        return False


class MLXTrajectoryAnomalyModel:
    """Autoencoder anomaly scorer implemented in MLX (darwin-only)."""

    def __init__(self, hidden: int = 64, seed: int = 0) -> None:
        if not mlx_backend_ready():
            raise RuntimeError(
                "MLX backend requested but mlx is not installed. Install the `mlx` extra on "
                "Apple silicon (pip install .[mlx]) or use the torch backend."
            )
        self.hidden = hidden
        self.seed = seed
        self._model: Any = None
        self._mean: NDArray[np.float64] | None = None
        self._std: NDArray[np.float64] | None = None
        self.threshold: float | None = None
        self.input_dim: int | None = None

    def _build(self, input_dim: int) -> Any:
        import mlx.nn as nn

        hidden_dim = self.hidden
        bottleneck = max(2, hidden_dim // 4)

        class AE(nn.Module):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__()
                self.e1 = nn.Linear(input_dim, hidden_dim)
                self.e2 = nn.Linear(hidden_dim, bottleneck)
                self.d1 = nn.Linear(bottleneck, hidden_dim)
                self.d2 = nn.Linear(hidden_dim, input_dim)

            def __call__(self, x: Any) -> Any:
                z = nn.relu(self.e1(x))
                z = self.e2(z)
                z = nn.relu(self.d1(nn.relu(z)))
                return self.d2(z)

        return AE()

    def fit(self, features: NDArray[np.float64], epochs: int = 20, lr: float = 1e-2) -> None:
        import mlx.core as mx
        import mlx.nn as nn
        import mlx.optimizers as optim

        x = np.asarray(features, dtype=np.float64)
        self.input_dim = x.shape[1]
        self._mean = x.mean(axis=0)
        self._std = x.std(axis=0) + 1e-6
        xs = mx.array(((x - self._mean) / self._std).astype(np.float32))

        self._model = self._build(self.input_dim)
        opt = optim.Adam(learning_rate=lr)

        def loss_fn(model: Any, batch: Any) -> Any:
            return nn.losses.mse_loss(model(batch), batch, reduction="mean")

        loss_and_grad = nn.value_and_grad(self._model, loss_fn)
        for _ in range(epochs):
            _, grads = loss_and_grad(self._model, xs)
            opt.update(self._model, grads)
            mx.eval(self._model.parameters(), opt.state)

    def score(self, features: NDArray[np.float64]) -> NDArray[np.float64]:
        import mlx.core as mx

        assert self._model is not None and self._mean is not None and self._std is not None
        x = np.asarray(features, dtype=np.float64)
        xs = mx.array(((x - self._mean) / self._std).astype(np.float32))
        out = self._model(xs)
        err = mx.mean((out - xs) ** 2, axis=1)
        return np.asarray(err, dtype=np.float64)

    def calibrate(self, benign_features: NDArray[np.float64], pct: float) -> float:
        self.threshold = float(np.percentile(self.score(benign_features), pct))
        return self.threshold

    def flag(self, features: NDArray[np.float64]) -> NDArray[np.bool_]:
        assert self.threshold is not None
        return self.score(features) > self.threshold
