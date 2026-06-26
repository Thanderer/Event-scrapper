from __future__ import annotations
import logging
import os
import time
from datetime import datetime, timedelta, timezone
import schedule
from db import get_db_connection, replace_events
from marge import merge_events
from find_and_handle_duplicates import find_and_handle_duplicates
from scraper_interface import Event, ScraperConfig
from magdeburg_tourist_scraper import MagdeburgTouristScraper
from mvgm_scraper import MvgmScraper
from dates_md_scraper import DatesMdScraper

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("main")

IMAGE_DIR         = os.getenv("IMAGE_DIR", "./data/images")
SCRAPE_DAILY_DAYS = int(os.getenv("SCRAPE_DAILY_DAYS", "7"))
SCRAPE_DEEP_DAYS  = int(os.getenv("SCRAPE_DEEP_DAYS",  "365"))

SCRAPERS = [
    MagdeburgTouristScraper(),
    MvgmScraper(),
    DatesMdScraper(),
]

def _event_to_dict(e: Event) -> dict:
    """Convert an Event dataclass to the dict shape expected by replace_events()."""
    description = e.description
    if isinstance(description, str):
        description = [description] if description else None
    elif isinstance(description, list) and not description:
        description = None

    return {
        "event_name":       e.event_name or "",
        "_name_normalized": e._name_normalized or "",
        "source":           e.source or [],
        "source_url":       e.source_url or [],
        "scraped_at":       e.scraped_at,
        "description":      description,
        "keywords":         e.keywords or [],
        "start_iso":        e.start_iso,
        "end_iso":          e.end_iso,
        "venue_name":       e.venue_name,
        "address":          e.address or "",
        "geo_lat":          e.geo_lat,
        "geo_lon":          e.geo_lon,
        "price":            e.price,
        "image_url":        e.image_url or [],
        "image_local_path": e.image_local_path or [],
        "series_id":        e.series_id or [],
    }

def run_scrape(days_ahead: int, label: str) -> None:
    """
    Full pipeline for one scrape run:
      1. Each scraper runs → _dedup_exact_url() called inside scrape_events()
      2. merge_events() accumulates across scrapers (URL-based dedup)
      3. find_and_handle_duplicates() fuzzy dedup on combined pool
      4. replace_events() writes to DB
    """
    now        = datetime.now(timezone.utc)
    start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date   = start_date + timedelta(days=days_ahead)

    config = ScraperConfig(
        image_dir    = IMAGE_DIR,
        scrape_until = end_date,
    )

    log.info(f"[{label}] Starting: {start_date.date()} → {end_date.date()} ({days_ahead}d)")

    # scrape + URL merge
    pool: list[Event] = []
    for scraper in SCRAPERS:
        try:
            log.info(f"[{label}] Scraping {scraper.source_name} ...")
            # _dedup_exact_url() is called inside scrape_events() by each scraper
            events = scraper.scrape_events(config)
            log.info(f"[{label}] {scraper.source_name}: {len(events)} events after URL dedup")
            pool = merge_events(pool, events)
        except Exception as e:
            log.error(f"[{label}] {scraper.source_name} failed: {e}", exc_info=True)

    log.info(f"[{label}] After URL merge: {len(pool)} events")

    # fuzzy dedup
    pool = find_and_handle_duplicates(pool)
    log.info(f"[{label}] After fuzzy dedup: {len(pool)} events")

    # DB write
    rows = [_event_to_dict(e) for e in pool]
    try:
        conn = get_db_connection()
        replace_events(conn, rows, start_date, end_date)
        conn.close()
        log.info(f"[{label}] DB write complete: {len(rows)} rows → [{start_date.date()}, {end_date.date()})")
    except Exception as e:
        log.error(f"[{label}] DB write failed: {e}", exc_info=True)


def daily_job() -> None:
    run_scrape(days_ahead=SCRAPE_DAILY_DAYS, label="DAILY")


def deep_job() -> None:
    run_scrape(days_ahead=SCRAPE_DEEP_DAYS, label="DEEP")


def run_scheduler() -> None:
    log.info("Scheduler starting ...")
    log.info(f"  DAILY job : every day at 03:00  ({SCRAPE_DAILY_DAYS}d horizon)")
    log.info(f"  DEEP  job : Mon + Thu  at 02:00  ({SCRAPE_DEEP_DAYS}d horizon)")

    schedule.every().day.at("03:00").do(daily_job)
    schedule.every().monday.at("02:00").do(deep_job)
    schedule.every().thursday.at("02:00").do(deep_job)

    log.info("Running initial deep scrape on startup ...")
    deep_job()

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    run_scheduler()