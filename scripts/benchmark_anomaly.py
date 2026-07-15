"""Reproduce PHAROS's GRU capacity selection across data and initialization seeds.

This is intentionally separate from ``make eval``: the normal evaluation stays fast, while this
deeper sweep checks that the configured hidden size is not an artifact of one torch initialization.

    python -m scripts.benchmark_anomaly
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass

from pharos.config import get_settings
from pharos.detect.seq_anomaly import SequenceAnomalyModel
from pharos.eval.goldset import GOLD_SEEDS, TEST_REGION, TRAIN_REGION, build_gold
from pharos.eval.metrics import roc_auc_anomaly_vs_normal, scenario_sequences


@dataclass(frozen=True)
class CapacityResult:
    hidden: int
    parameters: int
    runs: int
    within_mean: float
    within_sd: float
    cross_mean: float
    cross_sd: float
    cross_min: float


def benchmark_capacity(
    hidden_sizes: tuple[int, ...], model_seeds: tuple[int, ...]
) -> list[CapacityResult]:
    settings = get_settings()
    datasets = []
    for data_seed in GOLD_SEEDS:
        train_x, train_labels = scenario_sequences(
            build_gold(data_seed, TRAIN_REGION),
            settings.anomaly_seq_len,
            settings.track_gap_split_minutes,
        )
        test_x, test_labels = scenario_sequences(
            build_gold(data_seed, TEST_REGION),
            settings.anomaly_seq_len,
            settings.track_gap_split_minutes,
        )
        datasets.append((train_x, train_labels, test_x, test_labels))

    results = []
    for hidden in hidden_sizes:
        within: list[float] = []
        cross: list[float] = []
        parameters = 0
        for model_seed in model_seeds:
            for train_x, train_labels, test_x, test_labels in datasets:
                model = SequenceAnomalyModel(hidden=hidden, seed=model_seed)
                model.fit(train_x)
                parameters = model.parameter_count
                within.append(roc_auc_anomaly_vs_normal(model.score(train_x), train_labels))
                cross.append(roc_auc_anomaly_vs_normal(model.score(test_x), test_labels))
        results.append(
            CapacityResult(
                hidden=hidden,
                parameters=parameters,
                runs=len(cross),
                within_mean=statistics.fmean(within),
                within_sd=statistics.pstdev(within),
                cross_mean=statistics.fmean(cross),
                cross_sd=statistics.pstdev(cross),
                cross_min=min(cross),
            )
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hidden", nargs="+", type=int, default=[8, 16, 32, 64])
    parser.add_argument("--model-seeds", nargs="+", type=int, default=list(GOLD_SEEDS))
    args = parser.parse_args()
    for result in benchmark_capacity(tuple(args.hidden), tuple(args.model_seeds)):
        print(
            f"hidden={result.hidden:>2} params={result.parameters:>6,} runs={result.runs:>2} "
            f"within={result.within_mean:.3f}±{result.within_sd:.3f} "
            f"cross={result.cross_mean:.3f}±{result.cross_sd:.3f} "
            f"cross_min={result.cross_min:.3f}"
        )


if __name__ == "__main__":
    main()
