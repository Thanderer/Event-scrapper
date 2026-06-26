import json
import os
import re
import hashlib
import requests
import time
import unicodedata
import logging as log
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from urllib.parse import unquote
from scraper_interface import ScraperInterface, ScraperConfig, Event
from typing import List, Optional

BASE_URL        = "https://www.dates-md.de"
LISTING_URL     = f"{BASE_URL}/search/event/veranstaltungen-magdeburg/"
MAX_PAGES       = 200
DELAY_SECONDS   = 1.5
MAX_DETAIL_ERR  = 10
RETRY_ATTEMPTS  = 3
RETRY_BACKOFF   = [3, 6, 12]
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer":         BASE_URL,
}

session = requests.Session()
session.headers.update(HEADERS)

class DatesMdScraper(ScraperInterface):

    def __init__(self):
        super().__init__()

    @property
    def source_name(self) -> str:
        return "dates-md"

    def listing_url(self, page: int) -> str:
        return f"{LISTING_URL}?page={page}"   

    def parse_listing_page(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        stubs = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if data.get("@type") in ("https://schema.org/Event", "Event"):
                    stubs.append(data)
            except (json.JSONDecodeError, AttributeError):
                continue
        self._last_listing_soup = soup
        return stubs

    def has_next_page(self, soup: BeautifulSoup | None = None) -> bool:
        s = soup or getattr(self, "_last_listing_soup", None)
        if s is None:
            return False
        nxt = s.select_one("a.next[rel='next']")
        if nxt:
            classes = nxt.get("class") or []
            if "hidden" not in classes:
                return True
        nxt2 = s.select_one("div.paginatorstatic a.next")
        return nxt2 is not None     
    
    def parse_detail_page(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        result: dict = {}

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if "eventSchedule" in data or "keywords" in data or "priceRange" in data:
                    result = data
                    break
            except (json.JSONDecodeError, AttributeError):
                continue

        # All occurrence datetimes from HTML
        result["_all_occurrences"] = [
            str(t.get("datetime", ""))
            for t in soup.select(
                "span.eventoccs span.datetime time[itemprop='startDate']"
            )
            if t.get("datetime")
        ]

        cats_tag = soup.select_one("p.cats")
        if cats_tag:
            result["_category"] = cats_tag.get_text(strip=True)

        venue_link = soup.select_one("div.eventtags label.url + a") or \
                     soup.select_one("div.eventinfos a[href^='http'][rel='noopener']")
        if venue_link:
            result["_venue_website"] = str(venue_link.get("href", ""))

        canonical = soup.select_one("link[rel='canonical']")
        result["_canonical_url"] = str(canonical["href"]) if canonical else ""

        return result
    
    @staticmethod
    def _standardize_iso(dt_str: str) -> str:
        if not dt_str:
            return ""
        dt_str = dt_str.strip()[:19]
        if len(dt_str) == 10:
            return dt_str
        if len(dt_str) == 16:
            return dt_str + ":00"
        return dt_str

    @staticmethod
    def _extract_loc(d: dict) -> dict:
        return d.get("location") or {}

    @staticmethod
    def _extract_geo(d: dict) -> dict:
        return (d.get("location") or {}).get("geo") or {}
    
    def scrape_events(self, config: ScraperConfig) -> List[Event]:
        listing_stubs: list[dict] = []
        seen_urls: set[str] = set()

        for page_num in range(1, MAX_PAGES + 1):
            page_url = self.listing_url(page_num)
            log.info(f"Listing page {page_num} → {page_url}")
            html = self.fetch(page_url)
            if not html:
                log.error(f"Could not fetch listing page {page_num} — stopping.")
                break

            stubs= self.parse_listing_page(html)
            if not stubs:
                log.info(f"No events on page {page_num} — end of listing.")
                break

            for stub in stubs:
                event_url = stub.get("url", "")
                if not event_url:
                    continue
                if event_url.startswith("http://"):
                    event_url = "https://" + event_url[7:]
                if not event_url.startswith("http"):
                    event_url = BASE_URL + event_url
                slug_url = re.sub(r"\?occdtstart=.*$", "", event_url)
                if slug_url not in seen_urls:
                    seen_urls.add(slug_url)
                    stub["_detail_url"] = slug_url
                    stub["_page_url"]   = page_url
                    listing_stubs.append(stub)

            log.info(f"  {len(stubs)} stubs on page ({len(listing_stubs)} unique total)")

            if not self.has_next_page():
                log.info("No next page — pagination complete.")
                break

            time.sleep(DELAY_SECONDS)

        total = len(listing_stubs)
        log.info(f"Listing complete: {total} unique URLs. Starting detail extraction...")
        intermediate: list[dict] = []
        seen_canonicals: set[str] = set()
        consecutive_errors = 0

        for i, stub in enumerate(listing_stubs, 1):
            slug_url = stub["_detail_url"]
            time.sleep(DELAY_SECONDS)

            detail_html = self.fetch(slug_url)
            if not detail_html:
                consecutive_errors += 1
                log.warning(f"Detail fetch failed ({consecutive_errors} consecutive): {slug_url}")
                if consecutive_errors >= MAX_DETAIL_ERR:
                    log.error("Too many consecutive detail errors — aborting.")
                    break
                detail = {}
            else:
                consecutive_errors = 0
                detail = self.parse_detail_page(detail_html)

            canonical = detail.get("_canonical_url") or slug_url
            if canonical in seen_canonicals:
                log.debug(f"Skipping canonical duplicate: {canonical}")
                continue
            seen_canonicals.add(canonical)

            # Merge listing stub + detail
            src = detail if detail else stub
            loc = self._extract_loc(src) or self._extract_loc(stub)
            geo = self._extract_geo(src) or self._extract_geo(stub)

            name        = str(src.get("name") or stub.get("name", ""))
            description = str(src.get("description") or stub.get("description", ""))
            category    = str(detail.get("_category", ""))
            keywords    = src.get("keywords", [])
            start_iso   = self._standardize_iso(str(src.get("startDate") or stub.get("startDate", "")))
            end_iso     = self._standardize_iso(str(src.get("endDate") or stub.get("endDate", "")))
            venue_name  = str(loc.get("name", ""))
            address     = str(loc.get("address", ""))
            geo_lat     = geo.get("latitude")
            geo_lon     = geo.get("longitude")
            price       = str(loc.get("priceRange") or src.get("priceRange", "") or "")
            image_url   = str(src.get("image") or stub.get("image", "") or "")
            source_url  = canonical

            # Occurrence dates
            all_occs = detail.get("_all_occurrences", [])
            if not all_occs and start_iso:
                all_occs = [start_iso[:10]]
            occ_dates = [dt[:10] for dt in all_occs if dt]

            # scrape_until filter — skip entire event if earliest occurrence is beyond cutoff
            if occ_dates:
                earliest = min(occ_dates)
                try:
                    earliest_dt = datetime.fromisoformat(earliest).replace(tzinfo=timezone.utc)
                    if earliest_dt > config.scrape_until:
                        log.debug(f"Skipping (beyond scrape_until): {earliest}")
                        continue
                except ValueError:
                    pass

            # Group key for series detection (name + venue, date-stripped)
            clean_name = re.sub(r"[- ]*\b\d{1,2}\.\d{1,2}\.\d{2,4}\b", "", name).strip()
            raw_group  = f"{self.normalize(clean_name)}|{self.normalize(venue_name)}"
            group_key  = hashlib.sha256(raw_group.encode()).hexdigest()[:10]

            for occ_date in occ_dates:
                # Per-occurrence scrape_until filter
                try:
                    occ_dt = datetime.fromisoformat(occ_date).replace(tzinfo=timezone.utc)
                    if occ_dt > config.scrape_until:
                        continue
                except ValueError:
                    pass

                intermediate.append({
                    "group_key":   group_key,
                    "source_url":  source_url,
                    "name":        name,
                    "description": description,
                    "category":    category,
                    "keywords":    keywords,
                    "occ_date":    occ_date,
                    "end_iso":     end_iso,
                    "venue_name":  venue_name,
                    "address":     address,
                    "geo_lat":     geo_lat,
                    "geo_lon":     geo_lon,
                    "price":       price,
                    "image_url":   image_url,
                })

            log.info(f"  [{i}/{total}] {name[:60]} ({len(occ_dates)} occurrences)")

        # series linking + Event creation
        # Group by group_key → assign shared series_id to recurring events
        from collections import defaultdict
        groups: dict[str, list[int]] = defaultdict(list)
        for idx, item in enumerate(intermediate):
            groups[item["group_key"]].append(idx)

        final_events: list[Event] = []

        for group_key, indices in groups.items():
            # series_id = group_key itself if >1 occurrence, else None
            is_series  = len(indices) > 1
            series_id  = group_key if is_series else None

            for idx in indices:
                item = intermediate[idx]

                occ_date   = item["occ_date"]
                image_url  = item["image_url"]
                image_local = self.download_image(image_url, config.image_dir) if image_url else None

                try:
                    start_dt: datetime | None = datetime.fromisoformat(occ_date).replace(tzinfo=timezone.utc)
                except ValueError:
                    start_dt = None

                try:
                    end_dt: datetime | None = datetime.fromisoformat(item["end_iso"]).replace(tzinfo=timezone.utc) if item["end_iso"] else None
                except ValueError:
                    end_dt = None

                event = Event(
                    event_name =       item["name"],
                    source =           [self.source_name],
                    source_url =       [item["source_url"]],
                    scraped_at =       datetime.now(timezone.utc),
                    description =      item["description"],
                    keywords =         item["keywords"] if isinstance(item["keywords"], list) else [item["keywords"]],
                    start_iso =        start_dt,
                    end_iso =          end_dt,
                    venue_name =       item["venue_name"],
                    address =          item["address"],
                    geo_lat =          item["geo_lat"],
                    geo_lon =          item["geo_lon"],
                    price =            item["price"] or None,
                    image_url =        [image_url] if image_url else [],
                    image_local_path = [image_local] if image_local else [],
                    series_id =        [series_id] if series_id else None,
                )
                final_events.append(event)

        log.info(f"Done. {len(final_events)} Event objects created.")
        return ScraperInterface._dedup_exact_url(final_events)
