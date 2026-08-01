"""Read-only API + the guarded inference route, exercised over a seeded in-memory DB."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pharos.api.app import app, get_db
from pharos.api.scoring import reset_scorer
from pharos.config import get_settings
from pharos.db import models  # noqa: F401
from pharos.db.base import Base
from pharos.detect.run import run_detectors
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.reference import seed_zones
from pharos.ingest.synthetic import _step, generate_scenario
from pharos.tracks.build import build_tracks


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    # StaticPool + check_same_thread=False: TestClient runs sync endpoints in a threadpool,
    # so the in-memory DB must be a single connection shared across threads.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    sc = generate_scenario("singapore", seed=0, n_normal=12)
    seed_zones(db)
    persist_scenario_or_positions(db, sc.vessels, sc.positions)
    db.commit()
    build_tracks(db, region="singapore")
    db.add_all(run_detectors(sc.positions, get_settings()))
    db.commit()

    def _override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override
    settings = get_settings()
    original_model_dir = settings.anomaly_model_dir
    settings.anomaly_model_dir = tmp_path / "models"
    reset_scorer()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        settings.anomaly_model_dir = original_model_dir
        reset_scorer()
        db.close()
        engine.dispose()


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_stats(client: TestClient) -> None:
    s = client.get("/stats").json()
    assert s["vessels"] > 0 and s["tracks"] > 0 and s["zones"] >= 5
    assert s["incidents_by_detector"]["spoof"] >= 1


def test_incidents_and_filter(client: TestClient) -> None:
    allinc = client.get("/incidents").json()
    assert allinc and allinc[0]["score"] >= allinc[-1]["score"]  # riskiest first
    gaps = client.get("/incidents", params={"detector": "gap"}).json()
    assert gaps and all(i["detector"] == "gap" for i in gaps)


def test_incident_detail_and_404(client: TestClient) -> None:
    first = client.get("/incidents").json()[0]
    detail = client.get(f"/incidents/{first['incident_id']}").json()
    assert "evidence" in detail
    assert client.get("/incidents/nope").status_code == 404


def test_zones_geojson(client: TestClient) -> None:
    fc = client.get("/zones").json()
    assert fc["type"] == "FeatureCollection"
    ring = fc["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]  # closed ring
    # GeoJSON order is [lon, lat]; Singapore lon ~103.
    assert 100 < ring[0][0] < 110 or -120 < ring[0][0] < -80


def test_tracks_geojson(client: TestClient) -> None:
    fc = client.get("/tracks").json()
    assert fc["type"] == "FeatureCollection"
    assert all(f["geometry"]["type"] == "LineString" for f in fc["features"])


def test_vessel_track(client: TestClient) -> None:
    mmsi = client.get("/vessels").json()[0]["mmsi"]
    feat = client.get(f"/vessels/{mmsi}/track").json()
    assert feat["geometry"]["type"] == "LineString"
    assert client.get("/vessels/000000/track").status_code == 404


def test_maritime_picture(client: TestClient) -> None:
    rollups = client.get("/maritime-picture").json()
    assert rollups and rollups[0]["risk"] >= rollups[-1]["risk"]
    assert "components" in rollups[0] and "detectors" in rollups[0]


def _points(zigzag: bool) -> list[dict[str, float]]:
    lat, lon, heading = 1.2, 103.8, 90.0
    pts = []
    for i in range(20):
        pts.append({"lat": lat, "lon": lon, "sog": 11.0})
        heading += (60.0 if i % 2 == 0 else -55.0) if zigzag else 0.0
        lat, lon = _step(lat, lon, heading, 1.0)
    return pts


def test_score_track_ranks_zigzag_higher(client: TestClient) -> None:
    straight = client.post("/score-track", json={"points": _points(zigzag=False)}).json()
    zig = client.post("/score-track", json={"points": _points(zigzag=True)}).json()
    assert zig["anomaly_score"] > straight["anomaly_score"]
    assert "reconstruction_error" in zig
    assert zig["model_source"] in {"trained-artifact", "runtime-fallback"}


def test_detect_is_refused_only_where_the_host_opts_out(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trim the host, not the model: training stays on locally and is switched off in
    `render.yaml`.

    /detect trains the anomaly model. The free tier never returned (measured: no response
    after 180 s) while holding an inference slot the whole time, so a few calls could starve
    /score-track — the route the dashboard actually uses — into "server busy". That is a
    property of that host, so the deploy opts out rather than the default being weakened.
    """
    monkeypatch.setenv("PHAROS_API_ENABLE_DETECT", "false")
    get_settings.cache_clear()
    try:
        r = client.post("/detect", params={"region": "singapore"})
        assert r.status_code == 503
        assert "disabled on this deployment" in r.json()["detail"]
    finally:
        monkeypatch.delenv("PHAROS_API_ENABLE_DETECT", raising=False)
        get_settings.cache_clear()


def test_detect_persists_incidents_and_serves_exact_artifact(client: TestClient) -> None:
    # No opt-in needed: a local run trains at full strength by default.
    result = client.post("/detect", params={"region": "singapore"})
    assert result.status_code == 200
    anomaly = result.json()["anomaly"]
    assert anomaly["flagged"] >= 1
    assert get_settings().anomaly_model_dir.joinpath("gru-sequence-anomaly.pt").is_file()
    assert client.get("/stats").json()["incidents_by_detector"]["anomaly"] == anomaly["flagged"]

    scored = client.post("/score-track", json={"points": _points(zigzag=False)}).json()
    assert scored["model_source"] == "trained-artifact"


def test_score_track_needs_enough_points(client: TestClient) -> None:
    r = client.post("/score-track", json={"points": [{"lat": 1.2, "lon": 103.8}]}).json()
    assert "error" in r


def test_score_track_rejects_invalid_coordinates(client: TestClient) -> None:
    """Bad coordinates must be reported, never crash (500) or silently score to a misleading
    'not anomalous'. Before validation, a missing field 500'd and an inf coordinate scored to
    NaN and returned is_anomalous=false — a verdict on garbage input."""
    base = [{"lat": 1.1 + 0.1 * k, "lon": 103.1 + 0.1 * k, "ts": k * 60} for k in range(5)]

    # Missing lat used to raise a KeyError -> 500.
    missing = client.post("/score-track", json={"points": [{"lon": 103.1}, *base]})
    assert missing.status_code == 200 and "error" in missing.json()

    # Non-numeric lat used to raise a ValueError -> 500.
    bad = {"points": [{"lat": "north", "lon": 103.1}, *base]}
    nonnumeric = client.post("/score-track", json=bad)
    assert nonnumeric.status_code == 200 and "error" in nonnumeric.json()

    # Out-of-range and (via raw body) inf must be rejected, not scored.
    oob = client.post("/score-track", json={"points": [{"lat": 200.0, "lon": 103.1}, *base]})
    assert oob.status_code == 200 and "error" in oob.json()
    inf = client.post(
        "/score-track",
        content='{"points":[{"lat":1e999,"lon":103.1},'
        '{"lat":1.2,"lon":103.2},{"lat":1.3,"lon":103.3},{"lat":1.4,"lon":103.4},'
        '{"lat":1.5,"lon":103.5},{"lat":1.6,"lon":103.6}]}',
        headers={"content-type": "application/json"},
    )
    body = inf.json()
    assert "error" in body, f"inf coordinate must be rejected, got {body}"


def test_geoint_evidence(client: TestClient) -> None:
    ev = client.get("/geoint/evidence").json()
    assert ev  # non-empty
    first = ev[0]
    # ARGUS-compatible evidence shape, riskiest first.
    for field in ("doc_id", "title", "source", "reliability", "credibility", "summary", "url"):
        assert field in first
    assert first["source"] == "PHAROS maritime domain awareness"
    assert first["kind"] == "geoint"
    # min_score filter narrows the set.
    high = client.get("/geoint/evidence", params={"min_score": 0.9}).json()
    assert len(high) <= len(ev)
