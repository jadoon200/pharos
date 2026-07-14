from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PHAROS_", env_file=".env", extra="ignore")

    # Host port 5434 by default so PHAROS's Postgres coexists with SENTINEL (5432) and
    # ARGUS (5433).
    database_url: str = "postgresql+psycopg://pharos:pharos@localhost:5434/pharos"

    http_timeout_seconds: float = 30.0

    # --- Collection: NOAA Marine Cadastre AIS (free bulk CSV, no key) ------------------
    # The reproducible training/eval workhorse. A slice is one zone x a few days, cached
    # under data/ais/ (gitignored). Column names follow NOAA's AIS CSV header.
    ais_data_dir: Path = Path("data/ais")

    # --- Collection: AISStream.io live global lane (optional; the `live` extra) --------
    # Free WebSocket AIS; empty key disables the live lane (NOAA bulk needs no key).
    aisstream_key: str = ""
    aisstream_url: str = "wss://stream.aisstream.io/v0/stream"
    # Bounding boxes to subscribe to, [[[lat,lon],[lat,lon]], ...]. Default: the Singapore
    # Strait / Malacca approaches — the demo lane's whole point.
    aisstream_bbox: list[list[list[float]]] = [[[0.9, 103.3], [1.5, 104.2]]]
    aisstream_seconds: float = 120.0  # bounded capture window per `make live`

    # --- Collection: Global Fishing Watch event labels (optional cross-check) ----------
    # Free API token; empty disables the GFW cross-check in the eval.
    gfw_token: str = ""
    gfw_api_url: str = "https://gateway.api.globalfishingwatch.org/v3"

    # --- Track building ----------------------------------------------------------------
    # A voyage is split when a vessel is silent longer than this (a candidate AIS gap) or
    # its implied jump is physically impossible (see detect/spoof.py).
    track_gap_split_minutes: float = 60.0
    # Resample cadence for the anomaly model's fixed-length track features.
    track_resample_minutes: float = 5.0
    track_min_points: int = 6  # ignore stubs too short to analyse

    # --- Detectors ---------------------------------------------------------------------
    # Dark-ship / AIS-gap: a silence at least this long, in/near a sensitive zone, whose
    # reappearance is displaced, is a candidate dark event. Coverage-confound handled by the
    # reliability grade (terrestrial dense vs satellite sparse) — never an automated verdict.
    gap_min_minutes: float = 120.0
    gap_min_displacement_km: float = 5.0
    # Ship-to-ship rendezvous: two vessels within this range, both slow, for this long.
    rendezvous_max_km: float = 0.5
    rendezvous_max_speed_kn: float = 1.0
    rendezvous_min_minutes: float = 30.0
    # An STS is between *transiting* vessels that converge then separate — not an anchored
    # fleet sitting near each other. A participant must reach at least this speed somewhere on
    # its track (proof it was under way), else it's treated as anchored and excluded. This is
    # the fix for catastrophic rendezvous over-firing in congested ports (real-data-driven).
    rendezvous_min_transit_speed_kn: float = 3.0
    # A genuine STS is a *discrete* pairing. A vessel co-located with many others is an
    # anchorage cluster or a GPS-glitchy track that teleports near everyone — drop pairings
    # touching any vessel with more than this many distinct partners.
    rendezvous_max_partners: int = 2
    # Loitering: dwelling within a small radius (or inside a zone) for this long at low speed.
    loiter_radius_km: float = 2.0
    loiter_min_minutes: float = 60.0
    loiter_max_speed_kn: float = 2.0
    # ...but a vessel sitting at ~0 kn is *anchored*, not loitering-with-intent. Require some
    # motion (the vessel is holding/patrolling, not moored). Real-data-driven: LA/LB anchored
    # fleets otherwise flood the loiter detector.
    loiter_min_speed_kn: float = 0.5
    # Spoofing: an implied speed above this (from consecutive reports) is physically
    # impossible for a surface vessel → a certain-positive identity/kinematic anomaly.
    spoof_max_speed_kn: float = 60.0

    # --- Flagship trajectory-anomaly model ---------------------------------------------
    #   auto -> MLX on Apple silicon if importable, else torch. "mlx" | "torch" | "auto".
    anomaly_backend: str = "auto"
    anomaly_seq_len: int = 16  # resampled steps per windowed track sample
    anomaly_hidden: int = 64
    anomaly_epochs: int = 20
    anomaly_model_dir: Path = Path("data/models")
    # Anomaly score percentile (over benign training tracks) above which a track is flagged.
    anomaly_threshold_pct: float = 99.0

    # --- API hardening for public deployment (safe local-dev defaults) -----------------
    api_allowed_origins: str = ""
    api_max_request_chars: int = 20_000
    api_rate_limit_requests: int = 30
    api_rate_limit_window_seconds: float = 60.0
    api_trust_forwarded_header: bool = False
    api_inference_concurrency: int = 2
    api_inference_acquire_timeout_seconds: float = 15.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
