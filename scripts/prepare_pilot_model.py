"""Prepare a frozen GRU artifact from reproducible, local real-AIS corpora."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import torch

from pharos.config import get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.detect.anomaly import detect_anomalies
from pharos.detect.seq_anomaly import model_artifact_path
from pharos.ingest.noaa import load_csv
from pharos.ingest.persist import ensure_vessels, persist_positions
from pharos.ingest.reference import seed_zones
from pharos.logging import configure_logging
from pharos.tracks.build import build_tracks


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source(value: str) -> tuple[Path, str]:
    path, separator, region = value.partition("=")
    if not separator or not path or not region:
        raise argparse.ArgumentTypeError("source must be PATH=REGION")
    source_path = Path(path)
    if not source_path.is_file():
        raise argparse.ArgumentTypeError(f"source does not exist: {source_path}")
    return source_path, region


def prepare(sources: list[tuple[Path, str]], artifact: Path) -> dict[str, object]:
    """Idempotently ingest sources, rebuild training tracks, and write one artifact."""
    init_sqlite_schema()
    source_manifest: list[dict[str, str]] = []
    for path, region in sources:
        vessels, positions = load_csv(path, region)
        with session_scope() as session:
            seed_zones(session)
            ensure_vessels(session, vessels)
            persist_positions(session, positions)
        source_manifest.append({"path": str(path), "region": region, "sha256": _sha256(path)})

    with session_scope() as session:
        track_stats = build_tracks(session)
    with session_scope() as session:
        model_stats = detect_anomalies(session, model_path=artifact)

    return {
        "artifact": str(artifact),
        "artifact_sha256": _sha256(artifact),
        "sources": source_manifest,
        "tracks": track_stats,
        "model": model_stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        type=_source,
        help="repeatable PATH=REGION local AIS source",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=model_artifact_path(get_settings().anomaly_model_dir),
    )
    args = parser.parse_args()
    configure_logging()
    torch.set_num_threads(2)
    manifest = prepare(args.source, args.artifact)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
