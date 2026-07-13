from pharos.config import Settings, get_settings


def test_defaults_are_zero_cost() -> None:
    """Out of the box PHAROS needs no keys — NOAA bulk + demo seed only."""
    s = Settings()
    assert s.aisstream_key == ""  # live lane opt-in
    assert s.gfw_token == ""  # GFW cross-check opt-in
    assert s.anomaly_backend == "auto"
    assert "5434" in s.database_url  # coexists with SENTINEL (5432) / ARGUS (5433)


def test_env_prefix(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PHAROS_GAP_MIN_MINUTES", "45")
    assert Settings().gap_min_minutes == 45.0


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()
