import hashlib
import json
import logging as log
import os
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scraper_interface import Event, ScraperConfig, ScraperInterface

# Config
BASE_URL        = "https://www.mvgm.de"
LIST_URL        = "https://www.mvgm.de/de/events"
MAX_MONTHS      = 12
DELAY_SECONDS   = 1.5
REQUEST_TIMEOUT = 15
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer":         "https://www.mvgm.de/",
}

VENUE_COORDS: dict[str, tuple[float, float]] = {
    "AMO Kulturhaus":       (52.1322, 11.6459),
    "Avnet Arena":          (52.1269, 11.6364),
    "Elbauenpark":          (52.1197, 11.5978),
    "GETEC-Arena":          (52.1269, 11.6364),
    "Hyparschale":          (52.1312, 11.6389),
    "Jahrtausendturm":      (52.1197, 11.5978),
    "Johanniskirche":       (52.1237, 11.6378),
    "Messe Magdeburg":      (52.1312, 11.6389),
    "MDCC-Parkbühne":       (52.1197, 11.5978),
    "Seebühne":             (52.1197, 11.5978),
    "Stadthalle Magdeburg": (52.1312, 11.6389),
    "Albinmüller-Turm":     (52.1197, 11.5978),
}

session = requests.Session()
session.headers.update(HEADERS)


class MvgmScraper(ScraperInterface):
    def __init__(self):
        super().__init__()

    @property
    def source_name(self) -> str:
        return "mvgm.de"

    def parse_listing_page(self, html: str) -> list[dict]:
        soup  = BeautifulSoup(html, "html.parser")
        stubs = []

        for tile in soup.select("div.event.event-tiles"):
            title_a = tile.select_one(".textbox .title a[itemprop='url']")
            if not title_a:
                continue

            name_tag   = title_a.select_one("h3[itemprop='name']")
            name       = name_tag.get_text(" ", strip=True) if name_tag else title_a.get_text(strip=True)
            detail_url = urljoin(BASE_URL, str(title_a.get("href", "")))

            time_tag  = tile.select_one("time[itemprop='startDate']")
            start_raw = str(time_tag.get("datetime", "")) if time_tag else ""
            time_text = time_tag.get_text(strip=True) if time_tag else ""

            start_time, end_time = self.parse_time_range(time_text)

            if start_time and re.match(r"\d{4}-\d{2}-\d{2}$", start_raw):
                start_iso = f"{start_raw}T{start_time}:00"
            else:
                start_iso = self.standardize_iso(start_raw)

            end_iso_listing = None
            if end_time and start_iso and len(start_iso) >= 10:
                end_iso_listing = self.standardize_iso(f"{start_iso[:10]}T{end_time}:00")

            loc_div  = tile.select_one(".info.location")
            venue    = loc_div.get_text(strip=True) if loc_div else ""
            cat_div  = tile.select_one(".category")
            category = cat_div.get_text(strip=True) if cat_div else ""
            img_tag  = tile.select_one("figure img")
            img_url  = urljoin(BASE_URL, str(img_tag.get("src", ""))) if img_tag else ""

            stubs.append({
                "name":        name,
                "detail_url":  detail_url,
                "start_iso":   start_iso,
                "start_date":  start_raw[:10] if start_raw else "",
                "end_iso":     end_iso_listing,
                "venue":       venue,
                "category":    category,
                "image_url":   img_url,
            })

        return stubs
    
    def listing_url(self, page: int) -> str:
        # scraper for MVGM uses month-based listing, not page-based pagination.
        raise NotImplementedError("scraper for MVGM uses month-based listing, not page-based pagination.")

    def has_next_page(self, soup: BeautifulSoup) -> bool:
        # scraper for MVGM uses month-based listing, not page-based pagination.
        raise NotImplementedError("scraper for MVGM uses month-based listing, not page-based pagination.")

    def parse_detail_page(self, html: str, fallback_end_iso: str | None = None) -> dict:
        soup   = BeautifulSoup(html, "html.parser")
        detail: dict = {"end_iso": fallback_end_iso, "description": "", "price": None}

        meta = soup.select_one("meta[name='description']")
        if meta:
            detail["description"] = str(meta.get("content", "")).strip()

        for script in soup.select("script[type='application/ld+json']"):
            try:
                data   = json.loads(script.string or "")
                graphs = data.get("@graph", [data])
                for obj in graphs:
                    if obj.get("@type") == "Event":
                        if obj.get("endDate"):
                            detail["end_iso"] = self.standardize_iso(obj["endDate"])
                        detail["price"] = obj.get("offers", {}).get("price")
                        if not detail["description"] and obj.get("description"):
                            detail["description"] = obj["description"][:500]
            except (json.JSONDecodeError, AttributeError):
                pass

        return detail

    def scrape_events(self, config: ScraperConfig) -> List[Event]:
        # PHASE A: Collect listing stubs across N months
        now          = datetime.now()
        all_stubs:  list[dict] = []

        for i in range(MAX_MONTHS):
            d     = now.replace(day=1) + timedelta(days=32 * i)
            month = f"{d.year}{d.month:02d}"
            url   = f"{LIST_URL}?month={month}"
            log.info(f"[mvgm] Listing month {month} → {url}")

            html = self.fetch(url, session)
            if not html:
                log.warning(f"Failed to fetch listing for month {month} — skipping.")
                time.sleep(DELAY_SECONDS)
                continue

            stubs = self.parse_listing_page(html)
            log.info(f"  → {len(stubs)} events")
            all_stubs.extend(stubs)
            time.sleep(DELAY_SECONDS)

        # Dedup by (detail_url + start_date)
        seen:         set[str]   = set()
        unique_stubs: list[dict] = []
        for s in all_stubs:
            key = f"{s['detail_url']}|{s['start_date']}"
            if key not in seen:
                seen.add(key)
                unique_stubs.append(s)

        total = len(unique_stubs)
        log.info(f"[mvgm] {len(all_stubs)} total → {total} unique. Starting detail extraction...")

        # Detail extraction → intermediate dicts
        intermediate: list[dict] = []
        seen_hash_ids: set[str]  = set()

        for i, stub in enumerate(unique_stubs, 1):
            log.info(f"[{i}/{total}] {stub['name'][:70]}")
            detail_html = self.fetch(stub["detail_url"], session)
            time.sleep(DELAY_SECONDS)

            detail = self.parse_detail_page(detail_html, stub["end_iso"]) if detail_html else {}

            start_iso = stub["start_iso"]
            end_iso   = detail.get("end_iso") or stub["end_iso"] or ""

            # scrape_until filter
            if start_iso:
                try:
                    event_dt = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc)
                    if event_dt > config.scrape_until:
                        log.debug(f"Skipping (beyond scrape_until): {start_iso}")
                        continue
                except ValueError:
                    pass

            # Same-day duplicate guard
            hash_id = hashlib.sha256(f"{stub['detail_url']}|{stub['start_date']}".encode()).hexdigest()[:12]
            if hash_id in seen_hash_ids:
                log.debug(f"Skipping same-day duplicate: {stub['name']} @ {stub['start_date']}")
                continue
            seen_hash_ids.add(hash_id)

            # Group key: clean name + venue + year (strips embedded dates)
            clean_name = re.sub(r"[-|/ ]*\b\d{1,2}\.\d{1,2}\.(?:\d{2,4})?\b\s*", "", stub["name"]).strip()
            year       = (start_iso or "")[:4]
            raw_group  = f"{self.normalize(clean_name)}|{self.normalize(stub['venue'])}|{year}"
            group_key  = hashlib.sha256(raw_group.encode()).hexdigest()[:10]

            intermediate.append({
                "group_key":   group_key,
                "detail_url":  stub["detail_url"],
                "name":        stub["name"],
                "description": detail.get("description", ""),
                "category":    stub["category"],
                "start_iso":   start_iso,
                "end_iso":     end_iso,
                "venue":       stub["venue"],
                "image_url":   stub["image_url"],
                "price":       detail.get("price"),
            })

        # Series linking + Event creation
        groups: dict[str, list[int]] = defaultdict(list)
        for idx, item in enumerate(intermediate):
            groups[item["group_key"]].append(idx)

        final_events: list[Event] = []

        for group_key, indices in groups.items():
            series_id = group_key if len(indices) > 1 else None

            for idx in indices:
                item        = intermediate[idx]
                coords      = VENUE_COORDS.get(item["venue"])
                image_local = self.download_image(item["image_url"], config.image_dir, session) if item["image_url"] else None

                try:
                    start_dt = datetime.fromisoformat(item["start_iso"]).replace(tzinfo=timezone.utc) if item["start_iso"] else None
                except ValueError:
                    start_dt = None

                try:
                    end_dt = datetime.fromisoformat(item["end_iso"]).replace(tzinfo=timezone.utc) if item["end_iso"] else None
                except ValueError:
                    end_dt = None

                final_events.append(Event(
                    event_name =       item["name"],
                    source =           [self.source_name],
                    source_url =       [item["detail_url"]],
                    scraped_at =       datetime.now(timezone.utc),
                    description =      item["description"],
                    keywords =         [item["category"]] if item["category"] else [],
                    start_iso =        start_dt,
                    end_iso =          end_dt,
                    venue_name =       item["venue"],
                    address =          "",
                    geo_lat =          coords[0] if coords else None,
                    geo_lon =          coords[1] if coords else None,
                    price =            item["price"],
                    image_url =        [item["image_url"]] if item["image_url"] else [],
                    image_local_path = [image_local] if image_local else [],
                    series_id =        [series_id] if series_id else [],
                ))

        log.info(f"[mvgm] Done. {len(final_events)} Event objects created.")
        return ScraperInterface._dedup_exact_url(final_events)