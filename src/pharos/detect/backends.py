"""Trajectory-anomaly backend selection (torch vs MLX).

torch is the portable default (Linux/CI); MLX is the Apple-silicon-native path. The default is
benchmark-gated in `docs/EVAL.md` — mirroring SENTINEL's MLX adoption decision — so this resolver
only *offers* MLX when it is importable and explicitly requested/auto-selected on darwin; it never
forces a dependency that isn't installed.
"""

from __future__ import annotations

import platform
from importlib.util import find_spec


def mlx_available() -> bool:
    return platform.system() == "Darwin" and find_spec("mlx") is not None


def resolve_backend(name: str) -> str:
    """Resolve a configured backend name ("auto"|"mlx"|"torch") to a concrete backend.

    "auto" prefers MLX on Apple silicon when installed, else torch. An explicit "mlx" that
    isn't available falls back to torch with the caller left to log it (never a hard failure).
    """
    name = (name or "auto").lower()
    if name == "torch":
        return "torch"
    if name == "mlx":
        return "mlx" if mlx_available() else "torch"
    # auto
    return "mlx" if mlx_available() else "torch"
