from __future__ import annotations
import logging as log
from typing import List
from scraper_interface import Event

def _canonical(url: str) -> str:
    """Strip query-string noise so URLs from different sources can match."""
    return url.split("?")[0].rstrip("/").lower()


def _prefer(a: str, b: str) -> str:
    """Return the longer / non-empty string."""
    if not a:
        return b
    if not b:
        return a
    return a if len(a) >= len(b) else b


def _union(a: list | None, b: list | None) -> list:
    """Merge two lists preserving order, removing exact-string duplicates."""
    seen:   set  = set()
    result: list = []
    for item in (a or []) + (b or []):
        key = str(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _merge_two(base: Event, other: Event) -> Event:
    """
    Merge `other` into `base`, field by field.

    Rules:
    - List fields        → union (order-stable, no duplicates)
    - Scalar strings     → prefer longer / non-empty
    - Dates              → prefer whichever is set; keep base on conflict
    - Coords             → keep base if set, else take other
    - series_id          → union
    - scraped_at         → take the more recent timestamp
    """
    return Event(
        event_name        = _prefer(base.event_name,  other.event_name),
        source            = _union(base.source,        other.source),
        source_url        = _union(base.source_url,    other.source_url),
        scraped_at        = max(base.scraped_at, other.scraped_at),
        description       = _union(base.description,   other.description) or None,
        keywords          = _union(base.keywords,       other.keywords)    or None,
        start_iso         = base.start_iso  or other.start_iso,
        end_iso           = base.end_iso    or other.end_iso,
        venue_name        = _prefer(base.venue_name or "", other.venue_name or "") or None,
        address           = _prefer(base.address or "",    other.address or "")    or None,
        geo_lat           = base.geo_lat  if base.geo_lat  is not None else other.geo_lat,
        geo_lon           = base.geo_lon  if base.geo_lon  is not None else other.geo_lon,
        price             = _prefer(base.price or "",  other.price or "") or None,
        image_url         = _union(base.image_url,         other.image_url)         or None,
        image_local_path  = _union(base.image_local_path,  other.image_local_path)  or None,
        series_id         = _union(base.series_id,         other.series_id)         or None,
    )

def merge_events(accumulated: List[Event], new_events: List[Event]) -> List[Event]:
    """
    Merge `new_events` (from the latest scraper) into `accumulated`
    (events already collected from previous scrapers in this run).

    Deduplication key: canonical source URL overlap.
    If two events share at least one canonical URL they are merged into one.

    Args:
        accumulated:  Running list built up across all scrapers so far.
                      Pass [] on the first scraper call.
        new_events:   Output of one scraper's scrape_events() call,
                      already URL-deduped by _dedup_exact_url().

    Returns:
        New accumulated list — always pass this back as `accumulated`
        on the next scraper call.

    Example (in main.py):
        pool: list[Event] = []
        for scraper in SCRAPERS:
            events = scraper.scrape_events(config)
            pool   = merge_events(pool, events)
        # pool now holds merged, URL-deduped events from all scrapers
    """
    # Build URL → index map from the accumulated list
    url_index: dict[str, int] = {}
    for idx, event in enumerate(accumulated):
        for url in event.source_url:
            url_index[_canonical(url)] = idx

    result = list(accumulated)  # copy so we never mutate the input

    for event in new_events:
        canonical_urls = [_canonical(u) for u in event.source_url if u]

        target_idx: int | None = None
        for cu in canonical_urls:
            if cu in url_index:
                target_idx = url_index[cu]
                break

        if target_idx is None:
            # Genuinely new event — append
            target_idx = len(result)
            result.append(event)
        else:
            # Same event seen from another source — merge fields
            result[target_idx] = _merge_two(result[target_idx], event)

        # Register all URLs of this event to the same slot
        for cu in canonical_urls:
            url_index[cu] = target_idx

    before = len(accumulated) + len(new_events)
    after  = len(result)
    log.info(
        f"merge_events: +{len(new_events)} new | "
        f"{len(accumulated)} accumulated → {after} total "
        f"({before - after} merged)"
    )
    return result