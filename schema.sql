CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    
    event_name TEXT NOT NULL,
    _name_normalized TEXT DEFAULT NULL,

    source TEXT[] NOT NULL,
    source_url TEXT[] NOT NULL,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    
    description TEXT[],

    keywords TEXT[],

    start_iso TIMESTAMPTZ,
    end_iso TIMESTAMPTZ,

    venue_name TEXT,
    address TEXT,
    geo_lat DOUBLE PRECISION,
    geo_lon DOUBLE PRECISION,

    price TEXT,
    image_url TEXT[],
    image_local_path TEXT[],
    series_id TEXT[]  DEFAULT NULL,

    CONSTRAINT events_unique_event
        UNIQUE (_name_normalized, start_iso, venue_name)
);


CREATE INDEX IF NOT EXISTS idx_events_start_iso ON events (start_iso);