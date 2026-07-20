from pharos.config import Settings, get_settings


def test_defaults_are_zero_cost() -> None:
    """Out of the box PHAROS needs no keys — NOAA bulk + demo seed only."""
    # Defaults must be tested independently of a developer's gitignored .env, which may
    # legitimately contain the optional live-lane credentials documented in .env.example.
    s = Settings(_env_file=None)
    assert s.aisstream_key == ""  # live lane opt-in
    assert s.gfw_token == ""  # GFW cross-check opt-in
    assert s.anomaly_seq_len == 16  # the GRU sequence length
    assert s.anomaly_model_sha256 == ""  # opt-in freeze pin
    assert s.collector_batch_seconds == 45.0
    assert s.pilot_start_at is None
    assert s.collector_downsample_seconds == 45.0
    assert s.collector_health_timeout_seconds == 90.0
    assert s.process_interval_minutes == 2.0
    assert s.retention_positions_days == 21
    assert s.retention_prune_interval_hours == 24.0
    assert s.storage_warn_gb == 2.0
    assert s.storage_hard_gb == 5.0
    assert "5434" in s.database_url  # coexists with SENTINEL (5432) / ARGUS (5433)


def test_env_prefix(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PHAROS_GAP_MIN_MINUTES", "45")
    assert Settings().gap_min_minutes == 45.0


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()
