from __future__ import annotations
from datetime import timezone
import hashlib
import logging as log

import re
from typing import List, Optional
from Levenshtein import distance as lev_distance
from haversine import haversine, Unit
from scraper_interface import Event

MATCH_THRESHOLD = 0.80   # minimum similarity to be considered a candidate
TIME_MAX_HOURS  = 48.0   # time distance at which time_sim reaches 0 %
LOC_MAX_KM      = 50.0   # geo distance at which location_sim reaches 0 %


def _title_sim(a: Event, b: Event) -> float:
    """Levenshtein similarity on normalised event names."""
    na = (a._name_normalized or "").strip()
    nb = (b._name_normalized or "").strip()
    if not na or not nb:
        return 0.0
    max_len = max(len(na), len(nb))
    if max_len == 0:
        return 1.0
    return 1.0 - lev_distance(na, nb) / max_len


def _time_sim(a: Event, b: Event) -> Optional[float]:
    """
    Proximity of start_iso timestamps.
    Returns None if either event has no start_iso (treated as unknown).
    """
    if a.start_iso is None or b.start_iso is None:
        return 0.0
    
    def _to_naive(dt):
        """Strip tzinfo after converting to UTC."""
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    
    a_dt = _to_naive(a.start_iso)
    b_dt = _to_naive(b.start_iso)

    delta_hours = abs((a_dt - b_dt).total_seconds()) / 3600.0
    return 1.0 - min(delta_hours / TIME_MAX_HOURS, 1.0)


def _location_sim(a: Event, b: Event) -> Optional[float]:
    """
    Haversine similarity between geo coordinates.
    Returns None if either event is missing coords.
    """
    if None in (a.geo_lat, a.geo_lon, b.geo_lat, b.geo_lon):
        return None
    km = haversine((a.geo_lat, a.geo_lon), (b.geo_lat, b.geo_lon), unit=Unit.KILOMETERS)
    return 1.0 - min(km / LOC_MAX_KM, 1.0)


def _classify(
    t_sim: float,
    ti_sim: Optional[float],
    lo_sim: Optional[float],
) -> str:
    """
    Returns 'duplicate', 'recurrent', or 'uncertain'.

    duplicate  – title matches, time matches, location matches (or unknown)
    recurrent  – title matches, location matches (or unknown), time differs
    uncertain  – everything else that still triggered the candidate threshold
    """
    time_match     = ti_sim is not None and ti_sim > MATCH_THRESHOLD
    time_differs   = ti_sim is not None and ti_sim <= MATCH_THRESHOLD
    time_unknown   = ti_sim is None

    loc_match      = lo_sim is not None and lo_sim > MATCH_THRESHOLD
    loc_unknown    = lo_sim is None

    title_match    = t_sim > MATCH_THRESHOLD

    if title_match and time_match and (loc_match or loc_unknown):
        return "duplicate"

    if title_match and (loc_match or loc_unknown) and time_differs:
        return "recurrent"

    return "uncertain"


def _union(a: list | None, b: list | None) -> list:
    seen:   set  = set()
    result: list = []
    for item in (a or []) + (b or []):
        key = str(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _prefer(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    return a if len(a) >= len(b) else b


def _merge_duplicate(base: Event, other: Event) -> Event:
    """
    Merge a confirmed duplicate into the base event.
    List fields are unioned; scalar fields prefer the richer value.
    """
    return Event(
        event_name        = _prefer(base.event_name,        other.event_name),
        source            = _union(base.source,             other.source),
        source_url        = _union(base.source_url,         other.source_url),
        scraped_at        = max(base.scraped_at, other.scraped_at),
        description       = _union(base.description,        other.description)       or None,
        keywords          = _union(base.keywords,           other.keywords)          or None,
        start_iso         = base.start_iso  or other.start_iso,
        end_iso           = base.end_iso    or other.end_iso,
        venue_name        = _prefer(base.venue_name or "",  other.venue_name or "")  or None,
        address           = _prefer(base.address    or "",  other.address    or "")  or None,
        geo_lat           = base.geo_lat  if base.geo_lat  is not None else other.geo_lat,
        geo_lon           = base.geo_lon  if base.geo_lon  is not None else other.geo_lon,
        price             = _prefer(base.price or "",       other.price or "")       or None,
        image_url         = _union(base.image_url,          other.image_url)         or None,
        image_local_path  = _union(base.image_local_path,   other.image_local_path)  or None,
        series_id         = _union(base.series_id,          other.series_id)         or None,
    )

def _build_groups(pairs: list[tuple[int, int]]) -> list[list[int]]:
    """Union-find: turn (idx_a, idx_b) pairs into connected groups."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for a, b in pairs:
        union(a, b)

    groups: dict[int, list[int]] = {}
    for node in parent:
        root = find(node)
        groups.setdefault(root, []).append(node)

    return [sorted(g) for g in groups.values() if len(g) > 1]

def find_and_handle_duplicates(events: List[Event]) -> List[Event]:
    """
    Scan a merged List[Event] for fuzzy duplicates and handle them:

    - duplicate  → merge fields, remove the secondary event
    - recurrent  → keep both, series_id assigned (TODO: future)
    - uncertain  → keep both, log a warning

    Args:
        events: Output of the final merge_events() accumulation.

    Returns:
        Cleaned List[Event] with confirmed duplicates collapsed.
        Insertion order is preserved (first occurrence wins the slot).
    """
    n = len(events)
    if n < 2:
        return events

    log.info(f"find_and_handle_duplicates: scanning {n} events ...")

    dup_pairs:       list[tuple[int, int]] = []
    recurrent_pairs: list[tuple[int, int]] = []
    uncertain_pairs: list[tuple[int, int]] = []

    # O(n²) pair scan — acceptable for typical scrape sizes (< 2 000 events)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = events[i], events[j]

            if a.start_iso and b.start_iso:
                delta_days = abs((a.start_iso - b.start_iso).total_seconds()) / 86400
                if delta_days > 2:
                    continue

            t_sim  = _title_sim(a, b)
            ti_sim = _time_sim(a, b)
            lo_sim = _location_sim(a, b)

            # Gate: at least one dimension must exceed the threshold
            sims = [s for s in (t_sim, ti_sim, lo_sim) if s is not None]
            if not any(s > MATCH_THRESHOLD for s in sims):
                continue

            label = _classify(t_sim, ti_sim, lo_sim)

            if label == "duplicate":
                dup_pairs.append((i, j))
                log.debug(
                    f"  duplicate [{i}]↔[{j}]  "
                    f"title={t_sim:.0%} time={ti_sim:.0%} loc={'N/A' if lo_sim is None else f'{lo_sim:.0%}'}"
                )
            elif label == "recurrent":
                recurrent_pairs.append((i, j))
                log.debug(
                    f"  recurrent [{i}]↔[{j}]  "
                    f"title={t_sim:.0%} time={ti_sim:.0%} loc={'N/A' if lo_sim is None else f'{lo_sim:.0%}'}"
                )
            else:
                uncertain_pairs.append((i, j))
                log.warning(
                    f"  uncertain [{i}]↔[{j}]  "
                    f"'{a.event_name}' vs '{b.event_name}'  "
                    f"title={t_sim:.0%} time={'N/A' if ti_sim is None else f'{ti_sim:.0%}'} "
                    f"loc={'N/A' if lo_sim is None else f'{lo_sim:.0%}'}"
                )

    # Handle duplicates
    dup_groups = _build_groups(dup_pairs)
    remove_indices: set[int] = set()

    result = list(events)  # mutable copy

    for group in dup_groups:
        base_idx = group[0]
        for other_idx in group[1:]:
            result[base_idx] = _merge_duplicate(result[base_idx], result[other_idx])
            remove_indices.add(other_idx)
            log.info(
                f"  merged duplicate idx {other_idx} → {base_idx}  "
                f"('{events[other_idx].event_name}')"
            )

    # Handle recurrents (series_id: TODO)
    # recurrent_pairs available for future series_id assignment
    if recurrent_pairs:
        log.info(f"  {len(recurrent_pairs)} recurrent pairs found — series_id assignment pending (TODO)")

    # Build final list
    final = [e for idx, e in enumerate(result) if idx not in remove_indices]

    log.info(
        f"find_and_handle_duplicates: "
        f"{n} in → {len(final)} out  "
        f"({len(remove_indices)} duplicates merged, "
        f"{len(recurrent_pairs)} recurrent pairs, "
        f"{len(uncertain_pairs)} uncertain pairs)"
    )
    return final