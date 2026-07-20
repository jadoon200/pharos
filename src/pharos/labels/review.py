"""Build and operate the deterministic, blinded SG-PILOT-v0 human-review queue."""

from __future__ import annotations

import argparse
import html
import json
import random
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.config import Settings, get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.db.models import EventTrackMatch, Incident, Track, TrackReview
from pharos.detect.seq_anomaly import SequenceAnomalyModel, model_artifact_path
from pharos.logging import configure_logging

QUEUE_VERSION = "queue-v1"
QUEUE_SEED = 20260720
LABELS = (
    "normal",
    "route_or_kinematic_anomaly",
    "rendezvous_or_loiter",
    "verified_external_incident",
    "ais_data_artifact",
    "uncertain",
)
CONFIDENCES = ("low", "medium", "high")


def _near_threshold_ids(tracks: Sequence[Track], settings: Settings) -> set[str]:
    artifact = model_artifact_path(settings.anomaly_model_dir)
    eligible = [track for track in tracks if track.sequence]
    if not artifact.exists() or not eligible:
        return set()
    torch.set_num_threads(2)
    model = SequenceAnomalyModel.load(artifact)
    if model.threshold is None:
        return set()
    scores = model.score(np.array([track.sequence for track in eligible], dtype=np.float64))
    return {
        track.track_id
        for track, score in zip(eligible, scores, strict=True)
        if 0.8 * model.threshold <= score <= 1.2 * model.threshold
    }


def _strata(session: Session, settings: Settings) -> tuple[dict[str, list[str]], dict[str, float]]:
    query = select(Track).where(Track.region == settings.collector_region)
    if settings.pilot_start_at is not None:
        query = query.where(Track.end_ts >= settings.pilot_start_at)
    tracks = session.scalars(query.order_by(Track.track_id)).all()
    by_id = {track.track_id: track for track in tracks}
    incidents = session.scalars(
        select(Incident).where(Incident.track_id.in_(list(by_id))).order_by(Incident.incident_id)
    ).all()
    incident_ids = {incident.track_id for incident in incidents if incident.track_id}
    external_ids = {
        track_id
        for track_id in session.scalars(
            select(EventTrackMatch.track_id).where(
                EventTrackMatch.track_id.isnot(None),
                EventTrackMatch.status.in_(("matched", "ambiguous")),
            )
        ).all()
        if track_id is not None
    }
    artifact_ids = {
        incident.track_id
        for incident in incidents
        if incident.track_id
        and (
            incident.detector == "spoof"
            or "implied_speed_kn" in (incident.evidence or {})
            or "implied_speed" in (incident.evidence or {})
        )
    }
    near_ids = _near_threshold_ids(tracks, settings) - incident_ids
    score_by_track: dict[str, float] = defaultdict(float)
    for incident in incidents:
        if incident.track_id:
            score_by_track[incident.track_id] = max(
                score_by_track[incident.track_id], incident.score
            )

    assigned: set[str] = set()

    def take(ids: set[str], *, scored: bool = False) -> list[str]:
        values = ids & set(by_id) - assigned
        assigned.update(values)
        if scored:
            return sorted(values, key=lambda track_id: (-score_by_track[track_id], track_id))
        return sorted(values)

    pools = {
        "external-event": take(external_ids),
        "ais-artifact-candidate": take(artifact_ids),
        "pharos-alert": take(incident_ids, scored=True),
        "near-threshold": take(near_ids),
        "random-normal": sorted(set(by_id) - assigned),
    }
    return pools, score_by_track


def _design_path(settings: Settings) -> Path:
    return settings.labels_dir.parent / "review-queue-design.json"


def queue_sampling_design(session: Session, settings: Settings) -> dict[str, Any]:
    """Return the sampling design **frozen at queue-build time**.

    Inclusion probabilities are only meaningful against the track population that existed when
    the queue was drawn; recomputing them later, after new tracks accrue, would silently skew
    the inverse-probability weights in the evaluation. The design is therefore persisted the
    first time it is computed with a queue present, and read back verbatim afterwards.
    """
    path = _design_path(settings)
    if path.exists():
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    pools, _scores = _strata(session, settings)
    queued = session.scalars(
        select(TrackReview).where(TrackReview.review_id.like(f"{QUEUE_VERSION}:%"))
    ).all()
    selected: defaultdict[str, int] = defaultdict(int)
    for review in queued:
        selected[review.stratum] += 1
    strata: dict[str, dict[str, float | int | str]] = {}
    for name, pool in pools.items():
        count = selected[name]
        population = len(pool)
        strata[name] = {
            "population": population,
            "selected": count,
            "inclusion_probability": count / population if population else 0.0,
            "selection": "all" if count == population else "deterministic-seeded",
        }
    design = {
        "queue_version": QUEUE_VERSION,
        "seed": QUEUE_SEED,
        "strata": strata,
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    if queued:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(design, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return design


def build_review_queue(session: Session, settings: Settings) -> dict[str, Any]:
    existing = session.scalar(
        select(func.count())
        .select_from(TrackReview)
        .where(TrackReview.review_id.like(f"{QUEUE_VERSION}:%"))
    )
    if existing:
        return queue_sampling_design(session, settings)

    pools, _scores = _strata(session, settings)
    rng = random.Random(QUEUE_SEED)
    selected: list[tuple[str, str]] = []
    selected.extend((track_id, "external-event") for track_id in pools["external-event"])
    selected.extend(
        (track_id, "ais-artifact-candidate") for track_id in pools["ais-artifact-candidate"]
    )
    alert_target = min(60, len(pools["pharos-alert"]))
    selected.extend((track_id, "pharos-alert") for track_id in pools["pharos-alert"][:alert_target])

    near_pool = list(pools["near-threshold"])
    rng.shuffle(near_pool)
    near_target = min(max(20, settings.review_queue_min // 5), len(near_pool))
    selected.extend((track_id, "near-threshold") for track_id in near_pool[:near_target])

    remaining = settings.review_queue_min - len(selected)
    normal_pool = list(pools["random-normal"])
    rng.shuffle(normal_pool)
    if remaining > 0:
        selected.extend((track_id, "random-normal") for track_id in normal_pool[:remaining])

    rng.shuffle(selected)
    session.add_all(
        TrackReview(
            review_id=f"{QUEUE_VERSION}:{order:04d}",
            track_id=track_id,
            queue_order=order,
            stratum=stratum,
            reviewer_id="primary",
        )
        for order, (track_id, stratum) in enumerate(selected, start=1)
    )
    session.commit()
    return queue_sampling_design(session, settings)


def blinded_payload(review: TrackReview, track: Track) -> dict[str, Any]:
    """Return only route shape and kinematics—never identity, scores, strata, or location."""
    sequence = track.sequence or []
    x = 0.0
    y = 0.0
    polyline = [[x, y]]
    steps: list[dict[str, float]] = []
    duration_s = max(0.0, (track.end_ts - track.start_ts).total_seconds())
    step_seconds = duration_s / len(sequence) if sequence else 0.0
    for index, row in enumerate(sequence, start=1):
        dx, dy, distance, turn = (float(value) for value in row)
        x += dx
        y += dy
        polyline.append([round(x, 5), round(y, 5)])
        speed_kn = distance / 1.852 / (step_seconds / 3600.0) if step_seconds > 0 else 0.0
        steps.append(
            {
                "step": float(index),
                "speed_kn": round(speed_kn, 2),
                "turn_degrees": round(turn, 2),
            }
        )
    return {
        "review_number": review.queue_order,
        "duration_minutes": round(duration_s / 60.0, 1),
        "rough_length_km": round(track.distance_km or 0.0, 1),
        "relative_polyline_km": polyline,
        "kinematics": steps,
    }


def render_review_html(payload: dict[str, Any], path: Path) -> None:
    points = payload["relative_polyline_km"]
    width, height, pad = 760.0, 360.0, 30.0
    xs = [float(point[0]) for point in points] or [0.0]
    ys = [float(point[1]) for point in points] or [0.0]
    x_span = max(max(xs) - min(xs), 1e-9)
    y_span = max(max(ys) - min(ys), 1e-9)
    coords = " ".join(
        f"{pad + (x - min(xs)) / x_span * (width - 2 * pad):.1f},"
        f"{height - pad - (y - min(ys)) / y_span * (height - 2 * pad):.1f}"
        for x, y in zip(xs, ys, strict=True)
    )
    speeds = [row["speed_kn"] for row in payload["kinematics"]] or [0.0]
    max_speed = max(max(speeds), 1.0)
    spark = " ".join(
        f"{pad + i / max(len(speeds) - 1, 1) * (width - 2 * pad):.1f},"
        f"{110 - speed / max_speed * 80:.1f}"
        for i, speed in enumerate(speeds)
    )
    safe = html.escape(json.dumps(payload, sort_keys=True))
    document = f"""<!doctype html><meta charset='utf-8'><title>PHAROS blinded review</title>
<style>body{{background:#07100f;color:#d6f0ec;font:14px system-ui;margin:24px}}
svg{{background:#0f201e;border:1px solid #1c3330;border-radius:10px;display:block;margin:12px 0}}
polyline{{fill:none;stroke:#5eead4;stroke-width:2}} .muted{{color:#7fa5a0}}</style>
<h1>Review #{payload["review_number"]}</h1>
<p class='muted'>Re-based route shape · no vessel identity, detector output, score, or location</p>
<svg width='{width:.0f}' height='{height:.0f}'><polyline points='{coords}'/></svg>
<h2>Speed profile</h2><svg width='{width:.0f}' height='120'><polyline points='{spark}'/></svg>
<p>Duration: {payload["duration_minutes"]} min · rough length: {payload["rough_length_km"]} km</p>
<details><summary>Per-step kinematics</summary><pre>{safe}</pre></details>"""
    path.write_text(document, encoding="utf-8")


def build_rereview_queue(
    session: Session, settings: Settings, *, now: datetime | None = None
) -> int:
    now = now or datetime.now(UTC)
    eligible = session.scalars(
        select(TrackReview).where(
            TrackReview.reviewer_id == "primary",
            TrackReview.reviewed_at.isnot(None),
            TrackReview.reviewed_at <= now - timedelta(days=3),
        )
    ).all()
    existing_tracks = set(
        session.scalars(
            select(TrackReview.track_id).where(TrackReview.reviewer_id == "primary-rereview")
        ).all()
    )
    eligible = [review for review in eligible if review.track_id not in existing_tracks]
    existing_count = len(existing_tracks)
    rng = random.Random(QUEUE_SEED + 1)
    rng.shuffle(eligible)
    count = round(len(eligible) * settings.review_rereview_fraction)
    if eligible and count == 0:
        count = 1
    for order, primary in enumerate(eligible[:count], start=existing_count + 1):
        session.add(
            TrackReview(
                review_id=f"{QUEUE_VERSION}-rereview:{order:04d}",
                track_id=primary.track_id,
                queue_order=order,
                stratum=primary.stratum,
                reviewer_id="primary-rereview",
            )
        )
    session.commit()
    return count


def _prompt_choice(prompt: str, choices: tuple[str, ...]) -> str:
    for index, value in enumerate(choices, start=1):
        print(f"  {index}. {value}")
    while True:
        value = input(f"{prompt}: ").strip()
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1]


def review_next(session: Session, *, reviewer_id: str = "primary", open_file: bool = True) -> bool:
    review = session.scalar(
        select(TrackReview)
        .where(TrackReview.reviewer_id == reviewer_id, TrackReview.reviewed_at.is_(None))
        .order_by(TrackReview.queue_order)
    )
    if review is None:
        return False
    track = session.get(Track, review.track_id)
    if track is None:
        raise RuntimeError("queued track no longer exists")
    payload = blinded_payload(review, track)
    path = Path(tempfile.mkdtemp(prefix="pharos-review-")) / f"review-{review.queue_order}.html"
    render_review_html(payload, path)
    print(f"Blinded review #{review.queue_order}: {path}")
    if open_file:
        subprocess.run(["open", str(path)], check=False)
    review.label = _prompt_choice("label", LABELS)
    review.subtype = input("short subtype (optional): ").strip()[:128] or None
    review.confidence = _prompt_choice("confidence", CONFIDENCES)
    review.reason = input("one-line reason (optional): ").strip()[:500] or None
    review.reviewed_at = datetime.now(UTC)
    session.commit()
    return True


def adjudicate(session: Session) -> int:
    grouped: dict[str, list[TrackReview]] = defaultdict(list)
    for review in session.scalars(
        select(TrackReview).where(TrackReview.reviewed_at.isnot(None))
    ).all():
        grouped[review.track_id].append(review)
    updated = 0
    for reviews in grouped.values():
        primary = next((review for review in reviews if review.reviewer_id == "primary"), None)
        repeat = next(
            (review for review in reviews if review.reviewer_id == "primary-rereview"), None
        )
        if primary is None or repeat is None or repeat.adjudication is not None:
            continue
        if primary.label == repeat.label:
            primary.adjudication = repeat.adjudication = "agree"
        else:
            print(f"Disagreement: {primary.label} vs {repeat.label}")
            resolved = _prompt_choice("resolved label", LABELS)
            primary.adjudication = repeat.adjudication = f"resolved-{resolved}"
        updated += 1
    session.commit()
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rereview", action="store_true")
    parser.add_argument("--adjudicate", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    configure_logging()
    init_sqlite_schema()
    settings = get_settings()
    with session_scope() as session:
        build_review_queue(session, settings)
        if args.adjudicate:
            print(f"adjudicated={adjudicate(session)}")
            return
        reviewer_id = "primary"
        if args.rereview:
            build_rereview_queue(session, settings)
            reviewer_id = "primary-rereview"
        if not review_next(session, reviewer_id=reviewer_id, open_file=not args.no_open):
            print("review queue complete")


if __name__ == "__main__":
    main()
