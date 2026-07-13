from pharos.zones import REGISTRY, all_zones, zone_by_id, zone_for


def test_registry_wellformed() -> None:
    ids = [z.zone_id for z in REGISTRY]
    assert len(ids) == len(set(ids))  # unique ids
    for z in REGISTRY:
        assert len(z.polygon) >= 3
        assert z.kind in {"chokepoint", "port", "eez", "protected", "lane"}


def test_zone_for_singapore_strait() -> None:
    z = zone_for(1.2, 103.8)
    assert z is not None
    assert z.zone_id in {"singapore-strait", "singapore-anchorages", "phillip-channel"}


def test_zone_for_open_ocean_is_none() -> None:
    assert zone_for(-40.0, -140.0) is None


def test_zone_for_sensitive_only_prefers_watch_area() -> None:
    # Phillip Channel (sensitive) overlaps the anchorages (not sensitive); sensitive_only
    # must never return the non-sensitive zone.
    z = zone_for(1.17, 103.8, sensitive_only=True)
    if z is not None:
        assert z.sensitive is True


def test_zone_by_id_and_all() -> None:
    assert zone_by_id("malacca-strait") is not None
    assert zone_by_id("nope") is None
    assert len(all_zones()) == len(REGISTRY)
