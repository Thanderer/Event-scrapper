import psycopg2
import os
import logging as log
from datetime import datetime, timezone
from typing import Optional
from psycopg2.extras import execute_batch
from scraper_interface import Event

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

def get_latest_date_for_a_source(conn, source_name: str) -> Optional[datetime]:

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT MAX(start_iso) FROM events WHERE source @> ARRAY[%s]::text[]",
            (source_name,)
        )
        result = cursor.fetchone()
        if result and result[0] is not None: 
            dt = result[0]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt 
    return None

def replace_events(conn, events: list[dict], start_date: datetime, end_date: datetime):
    INSERT_SQL = """
        INSERT INTO events (
            event_name,
            _name_normalized,
            source, 
            source_url, 
            scraped_at, 
            description, 
            keywords, 
            start_iso, 
            end_iso, 
            venue_name, 
            address, 
            geo_lat, 
            geo_lon,
            price,
            image_url,
            image_local_path,
            series_id
        ) VALUES (
            %(event_name)s,
            %(_name_normalized)s,
            %(source)s,
            %(source_url)s,
            %(scraped_at)s,
            %(description)s,
            %(keywords)s,
            %(start_iso)s,
            %(end_iso)s,
            %(venue_name)s,
            %(address)s,
            %(geo_lat)s,
            %(geo_lon)s,
            %(price)s,
            %(image_url)s,
            %(image_local_path)s,
            %(series_id)s
        )
        """
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM events WHERE start_iso >= %s  AND start_iso <%s", (start_date, end_date))
            execute_batch(cursor, INSERT_SQL, events, page_size=100)
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    
def upsert_events(conn, events: list[Event]):
    with conn.cursor() as cur:
        _insert_events(cur, events, on_conflict="DO NOTHING")
    conn.commit()
    log.info(f"upsert_events: {len(events)} rows upserted (yearly, no delete)")


def _insert_events(cur, events: list[Event], on_conflict: str = "DO NOTHING"):
    for e in events:
        row = {
            "event_name":       e.event_name or "",
            "_name_normalized": e._name_normalized or "",
            "source":           e.source or [],
            "source_url":       e.source_url or [],
            "scraped_at":       e.scraped_at,
            "description":      ([e.description] if isinstance(e.description, str) and e.description
                                  else e.description if isinstance(e.description, list) and e.description
                                  else None),
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
        cur.execute(f"""
            INSERT INTO events (
                event_name, _name_normalized, source, source_url,
                scraped_at, description, keywords, start_iso, end_iso,
                venue_name, address, geo_lat, geo_lon, price,
                image_url, image_local_path, series_id
            ) VALUES (
                %(event_name)s, %(_name_normalized)s, %(source)s, %(source_url)s,
                %(scraped_at)s, %(description)s, %(keywords)s, %(start_iso)s, %(end_iso)s,
                %(venue_name)s, %(address)s, %(geo_lat)s, %(geo_lon)s, %(price)s,
                %(image_url)s, %(image_local_path)s, %(series_id)s
            )
            ON CONFLICT {on_conflict}
        """, row)