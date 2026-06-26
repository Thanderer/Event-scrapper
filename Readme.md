# Magdeburg Events Scraper

A dockerized Python scraper pipeline that collects events from three Magdeburg sources, merges and deduplicates them, and stores them in a PostgreSQL database.

***

## Sources

| Scraper | URL |
|---|---|
| `magdeburg_tourist` | https://veranstaltungen.magdeburg-tourist.de/magdeburg |
| `mvgm` | https://www.mvgm.de/de/events |
| `dates-md` | https://www.dates-md.de/search/event/veranstaltungen-magdeburg/ |

***

## Project Structure

```
.
├── scraper/
│   ├── scraper_interface.py          # Event dataclass + ScraperInterface ABC
│   ├── magdeburg_tourist_scraper.py
│   ├── mvgm_scraper.py
│   ├── dates_md_scraper.py
│   ├── merge.py                      # URL-based dedup + field merge
│   ├── find_and_handle_duplicates.py # Name/venue/date fuzzy dedup
│   ├── db.py                         # PostgreSQL read/write
│   ├── schema.sql                    # Table definition
│   └── Dockerfile
├── web/
│   └── Dockerfile
├── docker-compose.yml
├── .env                              # ← you create this (see below)
├── .env.example                      # ← committed template
└── .gitignore
```

***

## Setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd <repo>
```

### 2. Create your `.env` file

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Then edit `.env`:

```env
DB_HOST=db
DB_USER=postgres
DB_PASSWORD=your_secure_password
DB_NAME=magdeburg_events
DB_PORT=5432

# ── Scraper schedule ───────────────────────────────────────────────
# How many days ahead to scrape on the daily lightweight run
SCRAPE_DAILY_DAYS=7

# How many days ahead to scrape on the deep bi-weekly run
SCRAPE_DEEP_DAYS=30

# ── Image storage ──────────────────────────────────────────────────
IMAGE_DIR=./images
```

### 3. Start the stack

```bash
docker compose up --build
```

This starts three services:

| Service | Description |
|---|---|
| `db` | PostgreSQL 17 with a persistent volume |
| `scraper` | Runs the scrape pipeline; restarts nightly |
| `web` | API / frontend on port `8000` |

### 4. Initialise the database schema

On first run, apply the schema manually (only needed once):

```bash
docker compose exec db psql -U $MY_USER -d $MY_DB -f /docker-entrypoint-initdb.d/schema.sql
```

Or if running locally without Docker:

```bash
psql -U postgres -d magdeburg_events -f scraper/schema.sql
```

***

## Running the scraper locally (without Docker)

```bash
cd scraper
pip install -r requirements.txt

# Set env vars or source your .env
export $(cat ../.env | xargs)

python scrape.py
```

***

## Pipeline overview

```
magdeburg_tourist_scraper ─┐
mvgm_scraper               ├─► merge_events() ─► find_and_handle_duplicates() ─► replace_events()
dates_md_scraper           ─┘
```

1. **Each scraper** runs Phase A (listing) → Phase B (detail) → Phase C (series linking)
2. **`merge_events()`** deduplicates by canonical URL and merges fields from multiple sources
3. **`find_and_handle_duplicates()`** catches same-event duplicates across sources using `_name_normalized + venue + date`
4. **`replace_events()`** deletes the scraped date window from the DB and inserts fresh records

***

## Scrape window logic

| Condition | Scrape until |
|---|---|
| DB is empty (cold start) | `now + 1 year` |
| DB has events | `min(latest_event_date, now + SCRAPE_DAILY_DAYS)` |

***

## Environment variable reference

| Variable | Used by | Description |
|---|---|---|
| `MY_USER` | `docker-compose` → `db` | PostgreSQL superuser name |
| `MY_PASSWORD` | `docker-compose` → `db` | PostgreSQL superuser password |
| `MY_DB` | `docker-compose` → `db` | PostgreSQL database name |
| `DB_HOST` | `scraper`, `web` | Database hostname (use `db` inside Docker) |
| `DB_PORT` | `scraper`, `web` | Database port (default `5432`) |
| `DB_USER` | `scraper`, `web` | Database user |
| `DB_PASSWORD` | `scraper`, `web` | Database password |
| `DB_NAME` | `scraper`, `web` | Database name |
| `SCRAPE_DAILY_DAYS` | `scraper` | Days ahead for daily run |
| `SCRAPE_DEEP_DAYS` | `scraper` | Days ahead for deep bi-weekly run |
| `IMAGE_DIR` | `scraper` | Local path for downloaded event images |