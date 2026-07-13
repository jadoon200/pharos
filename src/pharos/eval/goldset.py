"""The gold set — reproducible, labelled scenarios the detectors are scored against.

The gold set is the synthetic generator (`pharos.ingest.synthetic`), which is the single source of
truth shared by the tests, the demo seed, and this eval. Each scenario carries ground-truth events
for every detector *and* a coverage-gap calibration trap the gap detector must not flag. We average
over several seeds so the reported numbers are means over many cases, and use a second region as the
cross-region generalization partner — mirroring SENTINEL's multi-seed, honest-split discipline.
"""

from __future__ import annotations

from pharos.ingest.synthetic import Scenario, generate_scenario

GOLD_SEEDS = (0, 1, 2, 3, 4)
TRAIN_REGION = "singapore"
TEST_REGION = "us-west"
N_NORMAL = 14


def build_gold(seed: int, region: str = TRAIN_REGION) -> Scenario:
    """One labelled gold scenario (benign transits + one of each event + the coverage trap)."""
    return generate_scenario(region, seed=seed, n_normal=N_NORMAL)
