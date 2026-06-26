import os
import re
import hashlib
import requests
import time
import unicodedata
import logging as log
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass, field
from bs4 import BeautifulSoup
from abc import ABC, abstractmethod

@dataclass
class Event:
    event_name: str
    _name_normalized: str = field(init=False, repr=False)
    source: List[str]
    source_url: List[str]
    scraped_at: datetime
    description: Optional[List[str]]
    keywords: Optional[List[str]]
    start_iso: Optional[datetime]
    end_iso: Optional[datetime]
    venue_name: Optional[str]
    address: Optional[str]
    geo_lat: Optional[float]
    geo_lon: Optional[float]
    price: Optional[str]
    image_url: Optional[List[str]]
    image_local_path: Optional[List[str]]
    series_id: Optional[List[str]]

    def __post_init__(self):
        self._name_normalized = ScraperInterface.normalize(self.event_name)

@dataclass
class ScraperConfig:
    scrape_until: datetime
    image_dir: str

_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF  = [3, 6, 12, 25]
_REQUEST_TIMEOUT = 20

_session = requests.Session()

class ScraperInterface(ABC):

    # Must implement
    @property
    @abstractmethod
    def source_name(self) -> str:
        """Unique identifier string for this source e.g. 'magdeburg_tourist'"""
        pass

    @abstractmethod 
    def has_next_page(self, soup : BeautifulSoup) -> bool:
        """Build the listing page URL for a given page number or offset."""
        pass

    @abstractmethod
    def listing_url(self, page: int) -> str:
        """Return True if there is a next listing page."""
        pass

    @abstractmethod
    def parse_listing_page(self, html: str) -> List[dict]:
        """
        Parse a listing page HTML.
        Returns (list[dict], BeautifulSoup) — event stubs + the soup for has_next_page."""
        pass

    @abstractmethod
    def parse_detail_page(self, html: str) -> dict:
        """Parse a detail page HTML. Returns a dict of extracted fields."""
        pass

    @abstractmethod
    def scrape_events(self, config: ScraperConfig) -> List[Event]:
        """Full scrape orchestration. Returns list of Event objects."""
        pass

    # Shared defaults (override if needed) 

    def fetch(self, url: str) -> str | None:
        """
        HTTP GET with retry/backoff. Returns raw HTML string or None.
        Override if a scraper needs auth headers, sessions, or different
        retry logic.
        """
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                r = _session.get(url, timeout=_REQUEST_TIMEOUT)
                if r.status_code == 200:
                    r.encoding = "utf-8"
                    return r.text
                elif r.status_code in (503, 429, 502, 504):
                    wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    log.warning(f"HTTP {r.status_code} — retrying in {wait}s (attempt {attempt+1}): {url}")
                    time.sleep(wait)
                else:
                    log.warning(f"HTTP {r.status_code} — skipping: {url}")
                    return None
            except requests.RequestException as e:
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                log.warning(f"Request error ({e}) — retrying in {wait}s")
                time.sleep(wait)
        log.error(f"All {_RETRY_ATTEMPTS} attempts failed for {url}")
        return None

    def download_image(self, url: str, images_dir: str) -> str | None:
        """
        Download image from url into images_dir/<hash>.<ext>.
        Skips if already exists. Returns local path or None.
        Override if a scraper needs special auth or different naming.
        """
        if not url:
            return None
        img_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        ext = os.path.splitext(url.split("?")[0])[-1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
            ext = ".jpg"
        local_path = os.path.join(images_dir, f"{img_hash}{ext}")
        if os.path.exists(local_path):
            return local_path
        try:
            r = _session.get(url, timeout=_REQUEST_TIMEOUT, stream=True)
            r.raise_for_status()
            os.makedirs(images_dir, exist_ok=True)
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            return local_path
        except Exception as e:
            log.warning(f"Image download failed ({url}): {e}")
            return None  
        
    # Shared static utilities

    @staticmethod
    def normalize(text: str) -> str:
        """
        Lowercase, strip accents, collapse whitespace.
        Use for _name_normalized and dedup/group keys.
        """
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c))
        return re.sub(r"\s+", " ", text).strip().lower()

    @staticmethod
    def standardize_iso(dt_str: str) -> str:
        if not dt_str:
            return ""
        dt_str = dt_str.strip()[:19]
        if len(dt_str) == 10:
            return dt_str
        if len(dt_str) == 16:
            return dt_str + ":00"
        return dt_str

    @staticmethod
    def parse_time_range(time_text: str) -> tuple[str | None, str | None]:
        time_text = (time_text or "").strip()
        matches = re.findall(r"(\d{1,2}[:.:]\d{2}|\d{3,4})", time_text)

        def norm(t: str) -> str:
            t = re.sub(r"[.:]", "", t).zfill(4)
            return f"{t[:2]}:{t[2:]}"

        if len(matches) >= 2:
            return norm(matches[0]), norm(matches[1])
        elif len(matches) == 1:
            return norm(matches[0]), None
        return None, None   
    
    @staticmethod
    def _dedup_exact_url(events: List[Event]) -> List[Event]:
        """
        This is to remove duplicate Event objects that share the same canonical URL.
        Keeps the first occurrence. Call this as the last step in scrape_events():

            return ScraperInterface._dedup_exact_url(final_events)

        This only catches duplicates produced by a single scraper (e.g. the
        same event appearing on multiple listing pages). Cross-source duplicates
        are handled later by find_and_handle_duplicates().
        """
        seen_urls: set[str] = set()
        result: list[Event] = []

        for event in events:
            canonical_urls = {
                url.split("?")[0].rstrip("/").lower()
                for url in event.source_url
                if url
            }
            # Skip if ALL canonical URLs have already been seen
            if canonical_urls and canonical_urls.issubset(seen_urls):
                log.debug(f"_dedup_exact_url: dropping duplicate {event.source_url}")
                continue

            seen_urls.update(canonical_urls)
            result.append(event)

        before, after = len(events), len(result)
        if before != after:
            log.info(f"_dedup_exact_url: {before} → {after} events ({before - after} duplicates removed)")
        return result