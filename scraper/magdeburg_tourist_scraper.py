import hashlib
import os
import re
import requests
import time, random
import logging as log
from datetime import datetime, timezone
from urllib.parse import unquote
from scraper_interface import ScraperInterface, ScraperConfig, Event
from typing import List
from bs4 import BeautifulSoup
from collections import defaultdict

BASE_LISTING_URL = "https://veranstaltungen.magdeburg-tourist.de/magdeburg"
PAGE_SIZE = 20
MAX_PAGES = 5000    
DELAY_SECONDS = 1.5

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

HEADERS = {
    "User-Agent":random.choice(USER_AGENTS),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Referer": "https://www.magdeburg-tourist.de/",
    "Cache-Control":    "no-cache",
    "Pragma":           "no-cache",
    "Connection":       "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":   "document",
    "Sec-Fetch-Mode":   "navigate",
    "Sec-Fetch-Site":   "none",
    "Sec-Fetch-User":   "?1",
    "sec-ch-ua":        '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
     "sec-ch-ua-platform": '"Windows"',
}

session = requests.Session()
session.headers.update(HEADERS)

class MagdeburgTouristScraper(ScraperInterface):
    def __init__(self):
        super().__init__()

    @property
    def source_name(self) -> str:
        return "magdeburg_tourist"

    def listing_url(self, page: int) -> str:
        if page == 0:
            return BASE_LISTING_URL
        return f"{BASE_LISTING_URL}?os={page}&sendfoo"

    def has_next_page(self, soup: BeautifulSoup | None = None) -> bool:
        s = soup or getattr(self, "_last_listing_soup", None)
        if s is None:
            return False
        return bool(s.find_all("a", href=re.compile(r"os=\d+")))
    
    def parse_listing_page(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        events = []
        seen_urls: set[str] = set()

        for row in soup.find_all("div", class_="row"):
            img_tag = row.find("img")
            if not img_tag:
                continue
            src = str(img_tag.get("src", ""))
            if not any(d in src for d in ["rce-event", "magdeburg-tourist.de", "gfs-magdeburg", "eventim.de"]):
                continue

            detail_url = str("")
            for a in row.find_all("a", href=re.compile(r"/magdeburg/")):
                href = str(a.get("href", ""))
                if re.search(r"-[a-f0-9]{20,}", href):
                    detail_url = str(href)
                    break
            if not detail_url or detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)

            if detail_url.startswith("http://"):
                detail_url = "https://" + detail_url[7:]
            elif detail_url.startswith("/"):
                detail_url = "https://veranstaltungen.magdeburg-tourist.de" + detail_url

            h3 = row.find("h3")
            title = h3.get_text(strip=True) if h3 else ""

            category = ""
            col = row.find("div", class_=lambda c: bool(c and "col-md-8" in c))
            if col:
                spans = col.find_all("span", class_="text-color-1")
                if spans:
                    category = spans[0].get_text(strip=True)

            thumbnail_url = ""
            raw_src = str(img_tag.get("src", ""))
            proxy_match = re.search(r"[?&]src=([^&]+)", raw_src)
            if proxy_match:
                thumbnail_url = unquote(proxy_match.group(1))
            elif raw_src.startswith("http"):
                thumbnail_url = raw_src
            elif raw_src.startswith("/"):
                thumbnail_url = "https://veranstaltungen.magdeburg-tourist.de" + raw_src

            events.append({
                "source":        "magdeburg-tourist",
                "name":          title,
                "category":      category,
                "source_urls":   [detail_url],
                "thumbnail_url": thumbnail_url,
            })
        self._last_listing_soup = soup
        return events
    
    def parse_detail_page(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        details: dict = {}

        desc_div = soup.find("div", class_="description")
        if desc_div:
            details["description"] = desc_div.get_text(separator="\n", strip=True)

        lat_input = soup.find("input", id="geob")
        lon_input = soup.find("input", id="geol")
        if lat_input and lon_input and lat_input.get("value") and lon_input.get("value"):
            try:
                details["location_coords"] = [
                    float(str(lon_input["value"])),
                    float(str(lat_input["value"])),
                ]
            except ValueError:
                pass

        ort_header = soup.find(string=lambda t: bool(t and "Ort" in t))
        if ort_header:
            address_tag = ort_header.find_next("address")
            if address_tag:
                venue_name = address_tag.find("strong")
                if venue_name:
                    details["location_name_raw"] = venue_name.get_text(strip=True)
                address_p = address_tag.find("p")
                if address_p:
                    details["address"] = address_p.get_text(separator=" ", strip=True)

        for box in soup.find_all("div", class_="box-content"):
            text = box.get_text(" ", strip=True)
            if "start_date" not in details:
                d = re.search(
                    r"(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag),?\s+(\d{2}\.\d{2}\.\d{2,4})",
                    text,
                )
                if d:
                    raw = d.group(1)
                    if len(raw) == 8:
                        raw = raw[:6] + "20" + raw[6:]
                    details["start_date"] = raw

            if "start_time" not in details:
                t = re.search(
                    r"(?:ab|von)\s+(\d{2}:\d{2})(?:\s+bis\s+(\d{2}:\d{2}))?\s+Uhr",
                    text,
                )
                if t:
                    details["start_time"] = t.group(1)
                    if t.group(2):
                        details["end_time"] = t.group(2)

        return details

    def scrape_events(self, config: ScraperConfig) -> List[Event]:
        # Listing
        offset = 0
        page_num = 0
        listing_events: dict[str, dict] = {}

        while page_num < MAX_PAGES:
            url = self.listing_url(offset)
            log.info(f"Listing page {page_num + 1} (os={offset}) → {url}")
            html = self.fetch(url, session)
            if not html:
                log.warning(f"Failed to fetch listing page {page_num + 1} (os={offset})")
                break  

            events_batch = self.parse_listing_page(html)
            if not events_batch:
                log.info("No events on this page — stopping pagination.")
                break

            new_count = 0
            for e in events_batch:
                key = e["source_urls"][0]
                if key not in listing_events:
                    listing_events[key] = e
                    new_count += 1

            log.info(f"  {len(events_batch)} events on page ({new_count} new) | {len(listing_events)} total")

            if not self.has_next_page():
                log.info("No next page link — pagination complete.")
                break

            offset += PAGE_SIZE
            page_num += 1
            time.sleep(DELAY_SECONDS)

        total = len(listing_events)
        log.info(f"Listing complete: {total} unique events. Starting detail extraction...")

        intermediate: list[dict] = []
        seen_hash_ids: set[str] = set()

        for i, (url_key, event) in enumerate(listing_events.items(), 1):
            log.info(f"[{i}/{total}] {url_key[:90]}")
            detail_html = self.fetch(url_key, session)
            time.sleep(DELAY_SECONDS)

            detail = self.parse_detail_page(detail_html) if detail_html else {}
            merged = {**event, **detail}

            # Build ISO dates
            start_raw  = merged.get("start_date", "")
            start_time = merged.get("start_time", "00:00")
            start_iso  = ""
            if start_raw and len(start_raw) == 10:
                start_iso = f"{start_raw[6:10]}-{start_raw[3:5]}-{start_raw[0:2]}T{start_time}:00"

            end_time = merged.get("end_time", "")
            end_iso  = ""
            if start_iso and end_time:
                end_iso = f"{start_iso[:11]}{end_time}:00"

            # scrape_until filter
            if start_iso:
                event_dt = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc)
                if event_dt > config.scrape_until:
                    log.debug(f"Skipping (beyond scrape_until): {start_iso}")
                    continue

            # Same-day duplicate guard
            hash_id = hashlib.sha256(f"{url_key}|{start_raw}".encode()).hexdigest()[:12]
            if hash_id in seen_hash_ids:
                log.debug(f"Skipping same-day duplicate: {merged.get('name')} @ {start_raw}")
                continue
            seen_hash_ids.add(hash_id)

            # Group key for series detection (name + venue, date stripped)
            clean_name = re.sub(r"[- ]*\b\d{1,2}\.\d{1,2}\.\d{2,4}\b", "", merged.get("name", "")).strip()
            raw_group  = f"{self.normalize(clean_name)}|{self.normalize(merged.get('location_name_raw', ''))}"
            group_key  = hashlib.sha256(raw_group.encode()).hexdigest()[:10]

            intermediate.append({
                "group_key":  group_key,
                "url_key":    url_key,
                "merged":     merged,
                "start_iso":  start_iso,
                "end_iso":    end_iso,
                "thumb":      merged.get("thumbnail_url", ""),
            })

        groups: dict[str, list[int]] = defaultdict(list)
        for idx, item in enumerate(intermediate):
            groups[item["group_key"]].append(idx)

        final_events: list[Event] = []

        for group_key, indices in groups.items():
            series_id = group_key if len(indices) > 1 else None

            for idx in indices:
                item   = intermediate[idx]
                merged = item["merged"]
                thumb  = item["thumb"]
                coords = merged.get("location_coords", [None, None])
                image_local = self.download_image(thumb, config.image_dir, session) if thumb else None

                final_events.append(Event(
                    event_name =       merged.get("name", ""),
                    source =           [self.source_name],
                    source_url =       [item["url_key"]],
                    scraped_at =       datetime.now(timezone.utc),
                    description =      [d] if (d := merged.get("description", "").strip()) else None,
                    keywords =         [merged.get("category", "")] if merged.get("category") else [],
                    start_iso =        datetime.fromisoformat(item["start_iso"]) if item["start_iso"] else None,
                    end_iso =          datetime.fromisoformat(item["end_iso"]) if item["end_iso"] else None,
                    venue_name =       merged.get("location_name_raw"),
                    address =          merged.get("address", ""),
                    geo_lat =          coords[1] if len(coords) > 1 else None,
                    geo_lon =          coords[0] if coords else None,
                    price =            None,
                    image_url =        [thumb] if thumb else [],
                    image_local_path = [image_local] if image_local else [],
                    series_id =        [series_id] if series_id else None,
                ))
        log.info(f"[magdeburg_tourist] Done. {len(final_events)} Event objects created.")
        return ScraperInterface._dedup_exact_url(final_events)