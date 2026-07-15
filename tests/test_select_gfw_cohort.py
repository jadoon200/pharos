from __future__ import annotations

import csv
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from scripts.select_gfw_cohort import build_cohort, event_mmsi, select_vessels

from pharos.ingest.gfw import GfwEvent


def _event(mmsi: str, counterpart: str | None = None) -> GfwEvent:
    raw = {"encounter": {"vessel": {"ssvid": counterpart}}} if counterpart else {}
    return GfwEvent("rendezvous", mmsi, datetime.now(UTC), None, 1.0, 2.0, raw)


def test_event_mmsi_includes_encounter_counterpart() -> None:
    assert event_mmsi([_event("1", "2")]) == {"1", "2"}


def test_select_vessels_keeps_labels_and_deterministic_background() -> None:
    counts = Counter({"1": 10, "2": 10, "3": 10, "4": 2})
    first, matched = select_vessels(counts, {"1", "missing"}, background_vessels=1)
    second, _ = select_vessels(counts, {"1", "missing"}, background_vessels=1)
    assert matched == {"1"}
    assert first == second
    assert len(first) == 2
    assert "4" not in first  # below the minimum report count


def test_build_cohort_writes_selected_reports(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "cohort.csv"
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["MMSI", "LAT", "LON"])
        writer.writeheader()
        for mmsi in ("1", "2", "3"):
            for _ in range(6):
                writer.writerow({"MMSI": mmsi, "LAT": 1, "LON": 2})

    result = build_cohort(source, output, [_event("1", "2")], background_vessels=1)

    assert result.matched_labeled_vessels == 2
    assert result.background_vessels == 1
    assert result.reports == 18
