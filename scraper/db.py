import psycopg2
import os

from datetime import datetime
from typing import Optional
from psycopg2.extras import execute_batch

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
        return result[0] if result[0] is not None else None

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