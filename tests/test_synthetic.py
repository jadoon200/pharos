from pharos.geo import haversine_km, implied_speed_kn
from pharos.ingest.synthetic import generate_scenario


def test_scenario_deterministic() -> None:
    a = generate_scenario("singapore", seed=1)
    b = generate_scenario("singapore", seed=1)
    assert len(a.positions) == len(b.positions)
    assert [p.lat for p in a.positions[:20]] == [p.lat for p in b.positions[:20]]


def test_scenario_has_all_event_types() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=6)
    types = {e.event_type for e in sc.truth}
    assert {"normal", "gap", "rendezvous", "loiter", "spoof", "anomaly"} <= types


def test_gap_leaves_a_real_silence() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=2)
    gap = next(e for e in sc.truth if e.event_type == "gap")
    pts = sorted((p for p in sc.positions if p.mmsi == gap.mmsi), key=lambda p: p.ts)
    # The largest inter-report interval on the gap vessel spans the injected silence.
    gaps_min = [(pts[i + 1].ts - pts[i].ts).total_seconds() / 60.0 for i in range(len(pts) - 1)]
    assert max(gaps_min) >= 150  # injected 180-min silence, minus one report interval


def test_rendezvous_is_colocated_and_slow() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=2)
    ev = next(e for e in sc.truth if e.event_type == "rendezvous")
    assert ev.counterpart_mmsi is not None
    a = [p for p in sc.positions if p.mmsi == ev.mmsi and ev.ts_start <= p.ts <= ev.ts_end]
    b = [
        p
        for p in sc.positions
        if p.mmsi == ev.counterpart_mmsi and ev.ts_start <= p.ts <= ev.ts_end
    ]
    assert a and b
    # During the dwell the pair sits within a few hundred metres, both slow.
    assert haversine_km(a[0].lat, a[0].lon, b[0].lat, b[0].lon) < 0.5
    assert max(p.sog or 0 for p in a) < 1.0


def test_spoof_has_impossible_jump() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=2)
    ev = next(e for e in sc.truth if e.event_type == "spoof")
    pts = sorted((p for p in sc.positions if p.mmsi == ev.mmsi), key=lambda p: p.ts)
    speeds = [
        implied_speed_kn(
            pts[i].lat,
            pts[i].lon,
            pts[i + 1].lat,
            pts[i + 1].lon,
            (pts[i + 1].ts - pts[i].ts).total_seconds(),
        )
        for i in range(len(pts) - 1)
    ]
    assert max(speeds) > 60  # a physically impossible surface-vessel speed


def test_cross_region() -> None:
    sc = generate_scenario("us-west", seed=0, n_normal=3)
    assert all(p.region == "us-west" for p in sc.positions)
    assert all(v.flag in {"US", None} for v in sc.vessels)
