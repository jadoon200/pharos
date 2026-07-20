"""Reproducible SG-PILOT-v0 evaluation over frozen data, labels, and review versions."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sklearn.metrics import average_precision_score, roc_auc_score
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.config import Settings, get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.db.models import (
    CollectorRun,
    CoverageOutage,
    EvaluationRun,
    EventTrackMatch,
    ExternalEvent,
    Incident,
    Position,
    Track,
    TrackReview,
    Vessel,
    Zone,
)
from pharos.detect.seq_anomaly import SequenceAnomalyModel, model_artifact_path
from pharos.labels.coverage import (
    observed_hours,
    observed_intervals,
    observed_vessel_hours,
)
from pharos.labels.match import RULE_VERSION, assert_every_covered_event_terminates
from pharos.labels.review import QUEUE_VERSION, queue_sampling_design
from pharos.logging import configure_logging
from pharos.timeutil import utc_aware

FROZEN_ARTIFACT_SHA256 = "01faa27e17dd194b8913a439034a5f71d56f48ebb291ddb073e6c9b0ee7788fb"
_MARK_START = "<!-- AUTO-EVAL-PILOT:START -->"
_MARK_END = "<!-- AUTO-EVAL-PILOT:END -->"
_POSITIVE_LABELS = {
    "route_or_kinematic_anomaly",
    "rendezvous_or_loiter",
    "verified_external_incident",
}


def not_estimable(reason: str, *, denominator: int = 0) -> dict[str, Any]:
    return {"status": "not estimable", "reason": reason, "denominator": denominator}


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> dict[str, Any]:
    if total <= 0:
        return not_estimable("no eligible observations")
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total))
        / denominator
    )
    return {
        "status": "estimated",
        "value": round(proportion, 6),
        "ci95": [round(max(0.0, centre - margin), 6), round(min(1.0, centre + margin), 6)],
        "numerator": successes,
        "denominator": total,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_frozen_artifact(settings: Settings) -> tuple[Path, str]:
    artifact = model_artifact_path(settings.anomaly_model_dir)
    if not artifact.exists():
        raise FileNotFoundError(f"frozen artifact not found: {artifact}")
    actual = _sha256(artifact)
    expected = settings.anomaly_model_sha256 or FROZEN_ARTIFACT_SHA256
    if actual != expected or actual != FROZEN_ARTIFACT_SHA256:
        raise RuntimeError("SG-PILOT-v0 frozen artifact SHA-256 mismatch")
    return artifact, actual


def _latest_review_by_track(session: Session) -> dict[str, TrackReview]:
    reviews = session.scalars(
        select(TrackReview).where(
            TrackReview.reviewer_id == "primary", TrackReview.reviewed_at.isnot(None)
        )
    ).all()
    return {review.track_id: review for review in reviews}


def _weighted_discrimination(
    session: Session,
    reviews: dict[str, TrackReview],
    sampling_design: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    strata = sampling_design["strata"]
    if any(
        details["selected"] and float(details["inclusion_probability"]) <= 0.0
        for details in strata.values()
    ):
        return not_estimable("one or more reviewed strata lack valid inclusion probabilities")
    artifact = model_artifact_path(settings.anomaly_model_dir)
    if not artifact.exists():
        return not_estimable("frozen model artifact is unavailable for reviewed-track scoring")
    tracks = session.scalars(
        select(Track).where(Track.track_id.in_(list(reviews)), Track.sequence.isnot(None))
    ).all()
    tracks = [track for track in tracks if track.sequence]
    if not tracks:
        return not_estimable("reviewed benchmark has no scoreable track sequences")
    torch.set_num_threads(2)
    model = SequenceAnomalyModel.load(artifact)
    raw_scores = model.score(np.array([track.sequence for track in tracks], dtype=np.float64))
    score_by_track = {
        track.track_id: float(score) for track, score in zip(tracks, raw_scores, strict=True)
    }
    rows: list[tuple[int, float, float]] = []
    for track_id, review in sorted(reviews.items()):
        if review.label == "uncertain" or track_id not in score_by_track:
            continue
        probability = float(strata.get(review.stratum, {}).get("inclusion_probability", 0.0))
        if probability <= 0.0:
            return not_estimable(f"invalid inclusion probability for {review.stratum}")
        rows.append(
            (int(review.label in _POSITIVE_LABELS), score_by_track[track_id], 1 / probability)
        )
    if not rows or len({row[0] for row in rows}) < 2:
        return not_estimable(
            "reviewed anomaly-score benchmark lacks both classes", denominator=len(rows)
        )
    labels = np.array([row[0] for row in rows])
    scores = np.array([row[1] for row in rows])
    weights = np.array([row[2] for row in rows])
    return {
        "status": "estimated",
        "roc_auc": round(float(roc_auc_score(labels, scores, sample_weight=weights)), 6),
        "pr_auc": round(float(average_precision_score(labels, scores, sample_weight=weights)), 6),
        "denominator": len(rows),
        "weighting": "inverse inclusion probability",
    }


def _external_agreement(session: Session, confidence: str) -> dict[str, Any]:
    events = session.scalars(
        select(ExternalEvent)
        .where(ExternalEvent.source_confidence == confidence)
        .order_by(ExternalEvent.event_id)
    ).all()
    eligible = 0
    detected = 0
    by_source: dict[str, dict[str, int]] = defaultdict(lambda: {"detected": 0, "eligible": 0})
    for event in events:
        matches = session.scalars(
            select(EventTrackMatch).where(
                EventTrackMatch.event_id == event.event_id,
                EventTrackMatch.in_observed_coverage.is_(True),
                EventTrackMatch.status.in_(("matched", "ambiguous")),
            )
        ).all()
        if not matches:
            continue
        eligible += 1
        by_source[event.source]["eligible"] += 1
        track_ids = [match.track_id for match in matches if match.track_id]
        has_alert = bool(
            session.scalar(
                select(Incident.incident_id).where(Incident.track_id.in_(track_ids)).limit(1)
            )
        )
        if has_alert:
            detected += 1
            by_source[event.source]["detected"] += 1
    result = wilson_interval(detected, eligible)
    result["sources"] = dict(sorted(by_source.items()))
    result["term"] = "recall" if confidence == "official" else "agreement"
    return result


def _review_agreement(session: Session) -> dict[str, Any]:
    primary = {
        review.track_id: review
        for review in session.scalars(
            select(TrackReview).where(
                TrackReview.reviewer_id == "primary", TrackReview.reviewed_at.isnot(None)
            )
        )
    }
    repeats = session.scalars(
        select(TrackReview).where(
            TrackReview.reviewer_id == "primary-rereview", TrackReview.reviewed_at.isnot(None)
        )
    ).all()
    compared = [
        (primary[review.track_id], review) for review in repeats if review.track_id in primary
    ]
    return wilson_interval(
        sum(left.label == right.label for left, right in compared), len(compared)
    )


def _per_detector(session: Session, reviews: dict[str, TrackReview]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for detector in ("gap", "rendezvous", "loiter", "spoof", "anomaly"):
        track_ids = set(
            session.scalars(
                select(Incident.track_id).where(
                    Incident.detector == detector, Incident.track_id.in_(list(reviews))
                )
            ).all()
        )
        eligible = [review for track_id, review in reviews.items() if track_id in track_ids]
        positives = sum(review.label in _POSITIVE_LABELS for review in eligible)
        output[detector] = (
            wilson_interval(positives, len(eligible))
            if positives >= 5
            else not_estimable("fewer than 5 reviewed positives", denominator=len(eligible))
        )
    return output


def _false_positive_concentration(
    session: Session, reviews: dict[str, TrackReview], intervals: list[Any]
) -> dict[str, dict[str, int]]:
    negative_tracks = {
        track_id
        for track_id, review in reviews.items()
        if review.label in {"normal", "ais_data_artifact"}
    }
    incidents = session.scalars(
        select(Incident).where(Incident.track_id.in_(negative_tracks))
    ).all()
    vessel_types = dict(session.execute(select(Vessel.mmsi, Vessel.ship_type)).tuples().all())
    zone_kinds = dict(session.execute(select(Zone.zone_id, Zone.kind)).tuples().all())
    groups: dict[str, Counter[str]] = {
        "reliability_grade": Counter(),
        "zone_kind": Counter(),
        "vessel_type": Counter(),
        "coverage_state": Counter(),
    }
    for incident in incidents:
        groups["reliability_grade"][incident.reliability] += 1
        zone_kind = (
            zone_kinds.get(incident.zone_id, "open-strait") if incident.zone_id else "open-strait"
        )
        groups["zone_kind"][zone_kind] += 1
        groups["vessel_type"][vessel_types.get(incident.mmsi) or "unknown"] += 1
        in_coverage = any(
            utc_aware(incident.ts_start) <= interval.end
            and utc_aware(incident.ts_end or incident.ts_start) >= interval.start
            for interval in intervals
        )
        state = "observed" if in_coverage else "outage-or-unobserved"
        groups["coverage_state"][state] += 1
    return {name: dict(sorted(counter.items())) for name, counter in groups.items()}


def compute_metrics(
    session: Session,
    settings: Settings,
    *,
    now: datetime,
    sampling_design: dict[str, Any],
) -> dict[str, Any]:
    intervals = observed_intervals(session, start_at=settings.pilot_start_at, now=now)
    hours = observed_hours(intervals)
    vessel_hours = observed_vessel_hours(session, intervals, settings.collector_region)
    reviews = _latest_review_by_track(session)
    reviewed_alert_tracks = set(
        session.scalars(select(Incident.track_id).where(Incident.track_id.in_(list(reviews)))).all()
    )
    eligible_alert_reviews = [reviews[track_id] for track_id in reviewed_alert_tracks if track_id]
    alert_positives = sum(review.label in _POSITIVE_LABELS for review in eligible_alert_reviews)
    incident_count = (
        session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.region == settings.collector_region)
        )
        or 0
    )
    runs = session.scalars(select(CollectorRun)).all()
    outages = session.scalars(select(CoverageOutage)).all()
    calendar_start = settings.pilot_start_at or (min((run.started_at for run in runs), default=now))
    calendar_hours = max(0.0, (now - utc_aware(calendar_start)).total_seconds() / 3600.0)
    return {
        "status": "complete"
        if len(reviews) >= 200 and len(eligible_alert_reviews) >= 50
        else "preliminary",
        "review_progress": {
            "tracks": {"reviewed": len(reviews), "target": 200},
            "pharos_alerts": {"reviewed": len(eligible_alert_reviews), "target": 50},
            "uncertain": sum(review.label == "uncertain" for review in reviews.values()),
        },
        "precision_reviewed_alerts": wilson_interval(alert_positives, len(eligible_alert_reviews)),
        "external_official_recall": _external_agreement(session, "official"),
        "external_gfw_agreement": _external_agreement(session, "silver"),
        "weighted_discrimination": _weighted_discrimination(
            session, reviews, sampling_design, settings
        ),
        "per_detector": _per_detector(session, reviews),
        "alert_rate": {
            "value_per_100_observed_vessel_hours": (
                round(incident_count * 100.0 / vessel_hours, 6) if vessel_hours else None
            ),
            "incidents": incident_count,
            "observed_vessel_hours": round(vessel_hours, 6),
            "observed_hours": round(hours, 6),
            "calendar_hours": round(calendar_hours, 6),
        },
        "false_positive_concentration": _false_positive_concentration(session, reviews, intervals),
        "score_drift": _score_drift(session),
        "operations": {
            "accepted_reports": sum(run.report_count for run in runs),
            "throughput_reports_per_observed_hour": round(
                sum(run.report_count for run in runs) / hours, 6
            )
            if hours
            else None,
            "uptime_fraction": round(hours / calendar_hours, 6) if calendar_hours else None,
            "reconnect_count": max(0, len(runs) - 1),
            "outage_count": len(outages),
            "outage_hours": round(
                sum(
                    max(
                        0.0,
                        (
                            utc_aware(outage.closed_at or now) - utc_aware(outage.opened_at)
                        ).total_seconds()
                        / 3600.0,
                    )
                    for outage in outages
                ),
                6,
            ),
            "observed_hours": round(hours, 6),
            "calendar_hours": round(calendar_hours, 6),
        },
        "review_agreement": _review_agreement(session),
        "limitations": [
            "AIS reception coverage and laptop uptime confound apparent vessel silence.",
            "AISStream is a beta feed with no service-level agreement.",
            "GFW labels are AIS-derived silver labels; their overlap is agreement, not "
            "independent recall.",
            "Single-reviewer labels use delayed blind re-review and remain subject to "
            "reviewer bias.",
        ],
    }


def _score_drift(session: Session) -> dict[str, Any]:
    incidents = session.scalars(select(Incident).order_by(Incident.ts_start)).all()
    vessels = dict(session.execute(select(Vessel.mmsi, Vessel.ship_type)).tuples().all())
    grouped: dict[str, list[float]] = defaultdict(list)
    for incident in incidents:
        day = utc_aware(incident.ts_start).date().isoformat()
        grouped[f"{day}|{vessels.get(incident.mmsi) or 'unknown'}"].append(incident.score)
    return {
        key: {
            "count": len(values),
            "p50": round(float(np.percentile(values, 50)), 6),
            "p95": round(float(np.percentile(values, 95)), 6),
        }
        for key, values in sorted(grouped.items())
    }


def _data_snapshot(session: Session, settings: Settings, now: datetime) -> dict[str, Any]:
    min_ts, max_ts = session.execute(select(func.min(Position.ts), func.max(Position.ts))).one()
    intervals = observed_intervals(session, start_at=settings.pilot_start_at, now=now)
    return {
        "positions": session.scalar(select(func.count()).select_from(Position)) or 0,
        "vessels": session.scalar(select(func.count()).select_from(Vessel)) or 0,
        "tracks": session.scalar(select(func.count()).select_from(Track)) or 0,
        "incidents": session.scalar(select(func.count()).select_from(Incident)) or 0,
        "min_ts": utc_aware(min_ts).isoformat() if min_ts else None,
        "max_ts": utc_aware(max_ts).isoformat() if max_ts else None,
        "observed_hours": round(observed_hours(intervals), 6),
    }


def _label_versions(session: Session) -> dict[str, Any]:
    rows = session.execute(
        select(
            ExternalEvent.source,
            func.count(),
            func.max(ExternalEvent.retrieved_at),
        ).group_by(ExternalEvent.source)
    ).all()
    return {
        "sources": {
            source: {
                "count": count,
                "retrieval_cutoff": utc_aware(cutoff).isoformat() if cutoff else None,
            }
            for source, count, cutoff in rows
        },
        "review_queue_version": QUEUE_VERSION,
        "match_rule_version": RULE_VERSION,
    }


def _render_eval_block(run: EvaluationRun) -> str:
    metrics = run.metrics
    precision = metrics["precision_reviewed_alerts"]

    def format_estimate(value: dict[str, Any]) -> str:
        if value["status"] != "estimated":
            return f"not estimable — {value['reason']} (n={value['denominator']})"
        low, high = value["ci95"]
        return (
            f"{value['value']:.3f} (95% Wilson CI {low:.3f}-{high:.3f}; n={value['denominator']})"
        )

    return "\n".join(
        [
            _MARK_START,
            "## SG-PILOT-v0 evaluation",
            "",
            f"Status: **{metrics['status']}**. Frozen artifact `{run.artifact_sha256}`.",
            "",
            f"- Precision among reviewed PHAROS alerts: {format_estimate(precision)}",
            "- Recall against official external events: "
            + format_estimate(metrics["external_official_recall"]),
            "- Agreement with GFW silver events: "
            + format_estimate(metrics["external_gfw_agreement"]),
            f"- Observed/calendar hours: {metrics['operations']['observed_hours']:.2f} / "
            f"{metrics['operations']['calendar_hours']:.2f}",
            "- Metrics retain explicit denominators; sparse breakdowns render as "
            "`not estimable`, never zero.",
            "",
            "Limitations: " + " ".join(metrics["limitations"]),
            _MARK_END,
        ]
    )


def _write_eval_doc(path: Path, run: EvaluationRun) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else "# Evaluation\n"
    block = _render_eval_block(run)
    if _MARK_START in text and _MARK_END in text:
        before, rest = text.split(_MARK_START, 1)
        _old, after = rest.split(_MARK_END, 1)
        text = before.rstrip() + "\n\n" + block + after
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


def run_pilot_evaluation(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    write_outputs: bool = True,
    verify_artifact: bool = True,
) -> EvaluationRun:
    now = now or datetime.now(UTC)
    artifact_sha = (
        verify_frozen_artifact(settings)[1] if verify_artifact else FROZEN_ARTIFACT_SHA256
    )
    assert_every_covered_event_terminates(session)
    sampling_design = queue_sampling_design(session, settings)
    metrics = compute_metrics(session, settings, now=now, sampling_design=sampling_design)
    external_count = session.scalar(select(func.count()).select_from(ExternalEvent)) or 0
    negatives = {
        "external_labels_empty": "No external events retrieved in the evaluation snapshot."
        if external_count == 0
        else None,
        "official_recall": metrics["external_official_recall"].get("reason"),
        "gfw_agreement": metrics["external_gfw_agreement"].get("reason"),
    }
    exclusions = {
        "out_of_coverage_external_events": session.scalar(
            select(func.count())
            .select_from(EventTrackMatch)
            .where(EventTrackMatch.in_observed_coverage.is_(False))
        )
        or 0,
        "uncertain_reviews": metrics["review_progress"]["uncertain"],
        "policy": "outage-crossing time is excluded from observed-time denominators",
    }
    run = EvaluationRun(
        run_id=f"SG-PILOT-v0:{now:%Y%m%dT%H%M%S}",
        artifact_sha256=artifact_sha,
        data_snapshot=_data_snapshot(session, settings, now),
        label_versions=_label_versions(session),
        sampling_design=sampling_design,
        metrics=metrics,
        exclusions=exclusions,
        negatives=negatives,
        created_at=now,
    )
    session.merge(run)
    session.commit()
    if write_outputs:
        _write_eval_doc(Path("docs/EVAL.md"), run)
        output = Path("data/evaluations.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "run_id": run.run_id,
                    "artifact_sha256": run.artifact_sha256,
                    "data_snapshot": run.data_snapshot,
                    "label_versions": run.label_versions,
                    "sampling_design": run.sampling_design,
                    "metrics": run.metrics,
                    "exclusions": run.exclusions,
                    "negatives": run.negatives,
                    "created_at": utc_aware(run.created_at).isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return run


def main() -> None:
    configure_logging()
    init_sqlite_schema()
    with session_scope() as session:
        run = run_pilot_evaluation(session, get_settings())
    print(json.dumps(run.metrics, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
