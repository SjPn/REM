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
    crawl_delay_sec: float = 1.8
    crawl_delay_jitter_sec: float = 0.7
    crawl_block_backoff_sec: float = 12.0
    crawl_max_pages: int = 5
    http_timeout_sec: float = 30.0
    http_verify_ssl: bool = False
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
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

    # Deal scoring thresholds
    deal_likely_min: int = 70
    deal_ambiguous_min: int = 40
    vanish_too_fast_days: int = 3


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
