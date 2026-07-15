import numpy as np

from pharos.geo import (
    haversine_km,
    haversine_pairs_km,
    haversine_series_km,
    implied_speed_kn,
    point_in_polygon,
)

# Singapore Strait square (lat, lon), matches zones.py "singapore-strait".
_SG = [(1.10, 103.55), (1.10, 104.10), (1.30, 104.10), (1.30, 103.55)]


def test_haversine_known_distance() -> None:
    # One degree of latitude ≈ 111.19 km.
    assert abs(haversine_km(0.0, 0.0, 1.0, 0.0) - 111.19) < 0.5


def test_haversine_zero() -> None:
    assert haversine_km(1.2, 103.8, 1.2, 103.8) == 0.0


def test_haversine_series_length() -> None:
    lats = np.array([1.0, 1.1, 1.2])
    lons = np.array([103.0, 103.0, 103.0])
    steps = haversine_series_km(lats, lons)
    assert steps.shape == (2,)
    assert np.all(steps > 0)


def test_vector_haversine_matches_scalar() -> None:
    lat1 = np.array([0.0, 1.2, 27.5])
    lon1 = np.array([0.0, 103.8, -92.0])
    lat2 = np.array([1.0, 1.201, 28.0])
    lon2 = np.array([0.0, 103.802, -91.5])
    vector = haversine_pairs_km(lat1, lon1, lat2, lon2)
    scalar = np.array(
        [haversine_km(a, b, c, d) for a, b, c, d in zip(lat1, lon1, lat2, lon2, strict=True)]
    )
    np.testing.assert_allclose(vector, scalar, rtol=1e-12, atol=1e-12)


def test_implied_speed_impossible() -> None:
    # ~111 km in 60 s → wildly impossible for a surface vessel.
    speed = implied_speed_kn(0.0, 0.0, 1.0, 0.0, seconds=60.0)
    assert speed > 1000


def test_implied_speed_nonpositive_interval() -> None:
    assert implied_speed_kn(0.0, 0.0, 1.0, 0.0, seconds=0.0) == 0.0


def test_point_in_polygon_inside_and_outside() -> None:
    assert point_in_polygon(1.2, 103.8, _SG) is True
    assert point_in_polygon(5.0, 120.0, _SG) is False


def test_point_in_polygon_degenerate() -> None:
    assert point_in_polygon(1.2, 103.8, [(0.0, 0.0), (1.0, 1.0)]) is False
