"""The empirical coverage model is the dark-ship confound turned into a measurement.

These tests pin the two verdicts that matter and the boundary between them: a vessel that
goes silent in demonstrably-audible water is attributable; a vessel that goes silent where
nothing at all is being heard is a coverage story, not an evasion story.
"""

from datetime import datetime, timedelta

from pharos.config import Settings
from pharos.db.models import Position
from pharos.detect.coverage import CoverageModel
from pharos.detect.gaps import detect_gaps

T0 = datetime(2023, 7, 25, 0, 0)


def _pos(mmsi: str, lat: float, lon: float, minutes: float) -> Position:
    return Position(
        mmsi=mmsi, lat=lat, lon=lon, ts=T0 + timedelta(minutes=minutes), region="us-gulf"
    )


def _busy_corridor(lat0: float, lon0: float, lat1: float, lon1: float) -> list[Position]:
    """Background traffic heard all along a corridor for 8 hours — receivers demonstrably up."""
    out: list[Position] = []
    for v in range(4):  # 4 witnesses > the default min_witnesses of 2
        for step in range(13):
            f = step / 12
            for hour in range(9):
                out.append(
                    _pos(
                        f"witness{v}",
                        lat0 + (lat1 - lat0) * f,
                        lon0 + (lon1 - lon0) * f,
                        hour * 60,
                    )
                )
    return out


def test_silence_in_audible_water_is_vessel_attributed() -> None:
    background = _busy_corridor(28.0, -92.0, 28.5, -91.0)
    model = CoverageModel.from_positions(background)
    assessment = model.assess_gap("dark1", 28.0, -92.0, 28.5, -91.0, T0, T0 + timedelta(hours=6))
    assert assessment.verdict == "vessel-attributed"
    assert assessment.corridor_support >= 0.75
    assert assessment.witness_vessels == 4
    assert assessment.endpoint_witnessed == 2


def test_silence_where_nothing_is_heard_is_coverage_explained() -> None:
    # Traffic exists only near shore; the vessel goes dark far offshore — the real GFW case.
    model = CoverageModel.from_positions(_busy_corridor(29.5, -90.0, 29.6, -89.9))
    assessment = model.assess_gap("dark1", 26.0, -95.0, 25.0, -96.0, T0, T0 + timedelta(hours=6))
    assert assessment.verdict == "coverage-explained"
    assert assessment.corridor_support == 0.0
    assert assessment.witness_vessels == 0


def test_the_dark_vessel_never_witnesses_itself() -> None:
    # Its own pre-gap reports must not count as evidence that the area was audible.
    own = [_pos("dark1", 28.0, -92.0, m) for m in range(0, 400, 20)]
    model = CoverageModel.from_positions(own)
    assessment = model.assess_gap("dark1", 28.0, -92.0, 28.05, -92.05, T0, T0 + timedelta(hours=6))
    assert assessment.witness_vessels == 0
    assert assessment.verdict == "coverage-explained"


def test_witnesses_are_time_scoped_to_the_silent_window() -> None:
    # Vessels heard only long AFTER the gap closed do not prove the area was audible during it.
    late = [_pos("witnessA", 28.0, -92.0, 60 * 40 + m) for m in range(0, 120, 10)]
    model = CoverageModel.from_positions(late)
    assessment = model.assess_gap("dark1", 28.0, -92.0, 28.05, -92.05, T0, T0 + timedelta(hours=6))
    assert assessment.witness_vessels == 0


def test_gap_detector_grades_the_two_cases_differently() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    # Same silence, same displacement — only the surrounding reception differs.
    dark = [_pos("dark1", 28.0, -92.0, 0), _pos("dark1", 28.5, -91.0, 60 * 8)]

    audible = CoverageModel.from_positions(_busy_corridor(28.0, -92.0, 28.5, -91.0))
    silent_region = CoverageModel.from_positions(_busy_corridor(29.5, -90.0, 29.6, -89.9))

    attributed = detect_gaps(dark, settings, coverage=audible)
    explained = detect_gaps(dark, settings, coverage=silent_region)
    assert len(attributed) == len(explained) == 1

    # The call survives in both cases (reception is measured, intent is not) — but the
    # Admiralty grade separates them, which is the whole point.
    assert attributed[0].evidence["coverage_verdict"] == "vessel-attributed"
    assert explained[0].evidence["coverage_verdict"] == "coverage-explained"
    assert attributed[0].reliability < explained[0].reliability  # 'C' sorts before 'E'
    assert attributed[0].score == explained[0].score  # the *signal* is identical


def test_a_starved_index_would_mislabel_everything() -> None:
    """Why the incremental path must build the model from the WHOLE population.

    Scoring a handful of "dirty" vessels against an index built from only their own reports
    starves it of witnesses and calls every gap coverage-explained. This pins the failure so
    a future refactor that narrows the index gets caught here with its reason attached.
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    dark = [_pos("dark1", 28.0, -92.0, 0), _pos("dark1", 28.5, -91.0, 60 * 8)]
    background = _busy_corridor(28.0, -92.0, 28.5, -91.0)

    full_population = detect_gaps(
        dark, settings, coverage=CoverageModel.from_positions(background + dark)
    )
    starved = detect_gaps(dark, settings, coverage=CoverageModel.from_positions(dark))

    assert full_population[0].evidence["coverage_verdict"] == "vessel-attributed"
    assert starved[0].evidence["coverage_verdict"] == "coverage-explained"


def test_detector_without_a_model_is_unchanged() -> None:
    # The model is opt-in: existing callers keep the previous behaviour and evidence keys.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    dark = [_pos("dark1", 28.0, -92.0, 0), _pos("dark1", 28.5, -91.0, 60 * 8)]
    (incident,) = detect_gaps(dark, settings)
    assert "coverage_verdict" not in incident.evidence
    assert incident.evidence["coverage_caveat"]
