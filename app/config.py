from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{DATA_DIR / 'estatemonitor.db'}"
    # Human-like crawl pacing (seconds). Defaults are conservative to reduce bans.
    crawl_delay_sec: float = 3.2
    crawl_delay_jitter_sec: float = 1.8
    crawl_block_backoff_sec: float = 45.0
    crawl_human_mode: bool = True
    # Every N requests take a longer "coffee" break (min/max inclusive range).
    crawl_break_every_min: int = 10
    crawl_break_every_max: int = 18
    crawl_break_sec_min: float = 25.0
    crawl_break_sec_max: float = 70.0
    crawl_max_pages: int = 5
    http_timeout_sec: float = 30.0
    http_verify_ssl: bool = False
    # Residential / datacenter proxy for crawl (http://user:pass@host:port or socks5://...)
    # Empty = direct connection. Secret — set only in Coolify / local .env, never commit.
    http_proxy: str | None = None
    # Fixed UA if set; empty = rotate realistic Chrome/Edge/Firefox pool per session.
    user_agent: str = ""
    log_level: str = "INFO"
    enrich_details: bool = True
    max_detail_pages: int = 40
    min_seen_for_vanish: int = 20
    # Daily scheduler (local time). Default: every day at 07:00
    crawl_schedule_cron: str = "0 7 * * *"
    scheduler_max_pages: int = 8
    scheduler_max_details: int = 80
    backfill_max_pages: int = 15
    backfill_max_details: int = 200

    # FX for UAH/EUR → USD (NBU-ish; update periodically). 2026-08-21: 44.61 грн/$, 52.13 грн/€.
    uah_per_usd: float = 44.61
    usd_per_eur: float = 1.169  # ≈ 52.13 / 44.61

    # Deal scoring thresholds
    deal_likely_min: int = 70
    deal_ambiguous_min: int = 40
    vanish_too_fast_days: int = 3


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
