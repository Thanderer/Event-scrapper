from __future__ import annotations
import logging
import os
import time
from datetime import datetime, timedelta, timezone
import schedule
from db import get_db_connection, replace_events, upsert_events, get_latest_date_for_a_source
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


IMAGE_DIR                     = os.getenv("IMAGE_DIR", "./data/images")
SCRAPE_DAILY_DAYS             = int(os.getenv("SCRAPE_DAILY_DAYS",    "7"))
SCRAPE_BIWEEKLY_DAYS          = int(os.getenv("SCRAPE_BIWEEKLY_DAYS", "30"))
SCRAPE_YEARLY_DAYS            = int(os.getenv("SCRAPE_YEARLY_DAYS",   "365"))
YEARLY_TRIGGER_THRESHOLD_DAYS = int(os.getenv("YEARLY_TRIGGER_THRESHOLD_DAYS", "30"))


SCRAPERS = [
    MagdeburgTouristScraper(),
    MvgmScraper(),
    DatesMdScraper(),
]
def should_run_yearly(conn, source_name: str) -> bool:
    latest = get_latest_date_for_a_source(conn, source_name)
    if latest is None:
        log.info(f"[{source_name}] No entries in DB → triggering yearly scrape")
        return True
    now = datetime.now(timezone.utc)
    days_remaining = (latest - now).days
    if days_remaining <= YEARLY_TRIGGER_THRESHOLD_DAYS:
        log.info(f"[{source_name}] Latest entry in {days_remaining}d → triggering yearly scrape")
        return True
    log.info(f"[{source_name}] Latest entry in {days_remaining}d → yearly scrape not needed")
    return False


def _event_to_dict(e: Event) -> dict:
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


def run_scrape(days_ahead: int, label: str, replace: bool = True) -> None:
    """
    replace=True  → delete window then insert (daily / biweekly)
    replace=False → upsert only, no delete (yearly)
    """
    now        = datetime.now(timezone.utc)
    start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date   = start_date + timedelta(days=days_ahead)

    config = ScraperConfig(
        image_dir    = IMAGE_DIR,
        scrape_until = end_date,
    )

    log.info(f"[{label}] Starting: {start_date.date()} → {end_date.date()} ({days_ahead}d) replace={replace}")

    pool: list[Event] = []
    for scraper in SCRAPERS:
        try:
            log.info(f"[{label}] Scraping {scraper.source_name} ...")
            events = scraper.scrape_events(config)
            log.info(f"[{label}] {scraper.source_name}: {len(events)} events after URL dedup")
            pool = merge_events(pool, events)
        except Exception as e:
            log.error(f"[{label}] {scraper.source_name} failed: {e}", exc_info=True)

    log.info(f"[{label}] After URL merge: {len(pool)} events")

    pool = find_and_handle_duplicates(pool)
    log.info(f"[{label}] After fuzzy dedup: {len(pool)} events")

    try:
        conn = get_db_connection()
        if replace:
            rows = [_event_to_dict(e) for e in pool]
            replace_events(conn, rows, start_date, end_date)
            log.info(f"[{label}] DB replace complete: {len(rows)} rows → [{start_date.date()}, {end_date.date()})")
        else:
            upsert_events(conn, pool)
            log.info(f"[{label}] DB upsert complete: {len(pool)} rows (no delete)")
        conn.close()
    except Exception as e:
        log.error(f"[{label}] DB write failed: {e}", exc_info=True)


def daily_job() -> None:
    run_scrape(days_ahead=SCRAPE_DAILY_DAYS, label="DAILY", replace=True)


def biweekly_job() -> None:
    run_scrape(days_ahead=SCRAPE_BIWEEKLY_DAYS, label="BIWEEKLY", replace=True)


def yearly_job() -> None:
    run_scrape(days_ahead=SCRAPE_YEARLY_DAYS, label="YEARLY", replace=False)


def startup_yearly_check() -> None:
    """On startup, run yearly for any source whose data is thin or missing."""
    conn = get_db_connection()
    needs_yearly = any(should_run_yearly(conn, s.source_name) for s in SCRAPERS)
    conn.close()
    if needs_yearly:
        log.info("Startup: yearly condition met → running yearly job")
        yearly_job()
    else:
        log.info("Startup: DB is well stocked → skipping yearly, running biweekly")
        biweekly_job()


def run_scheduler() -> None:
    log.info("Scheduler starting ...")
    log.info(f"  DAILY    job: every day at 03:00       ({SCRAPE_DAILY_DAYS}d horizon, replace)")
    log.info(f"  BIWEEKLY job: Mon + Thu at 02:00       ({SCRAPE_BIWEEKLY_DAYS}d horizon, replace)")
    log.info(f"  YEARLY   job: 1st of month at 01:00    ({SCRAPE_YEARLY_DAYS}d horizon, upsert)")

    schedule.every().day.at("03:00").do(daily_job)
    schedule.every().monday.at("02:00").do(biweekly_job)
    schedule.every().thursday.at("02:00").do(biweekly_job)
    schedule.every().day.at("01:00").do(lambda: yearly_job() if datetime.now().day == 1 else None)

    startup_yearly_check()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    run_scheduler()