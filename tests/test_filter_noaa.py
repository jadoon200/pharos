from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pytest
from scripts.filter_noaa import filter_zip


def _archive(path: Path) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["MMSI", "BaseDateTime", "LAT", "LON", "SOG"])
    writer.writeheader()
    writer.writerows(
        [
            {"MMSI": "1", "BaseDateTime": "2023-07-25T00:00:00", "LAT": 27, "LON": -90, "SOG": 5},
            {"MMSI": "1", "BaseDateTime": "2023-07-25T00:01:00", "LAT": 28, "LON": -91, "SOG": 6},
            {"MMSI": "2", "BaseDateTime": "2023-07-25T00:00:00", "LAT": 40, "LON": -70, "SOG": 7},
            {
                "MMSI": "3",
                "BaseDateTime": "2023-07-25T00:00:00",
                "LAT": "bad",
                "LON": -90,
                "SOG": 0,
            },
        ]
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AIS_2023_07_25.csv", buffer.getvalue())


def test_filter_zip_writes_bbox_slice(tmp_path: Path) -> None:
    source = tmp_path / "day.zip"
    output = tmp_path / "gulf.csv"
    _archive(source)

    result = filter_zip(source, output, (24, -98, 31, -87))

    assert result.reports == 2
    assert result.vessels == 1
    assert len(result.sha256) == 64
    with output.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["MMSI"] for row in rows] == ["1", "1"]


def test_filter_zip_rejects_reversed_bbox(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bbox"):
        filter_zip(tmp_path / "missing.zip", tmp_path / "out.csv", (31, -98, 24, -87))


def test_filter_zip_can_retain_specific_mmsi(tmp_path: Path) -> None:
    source = tmp_path / "day.zip"
    output = tmp_path / "one-vessel.csv"
    _archive(source)

    result = filter_zip(source, output, (24, -98, 45, -65), {"2"})

    assert result.reports == 1
    assert result.vessels == 1
