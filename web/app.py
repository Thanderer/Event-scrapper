import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template_string

app = Flask(__name__)

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Magdeburg Events</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: system-ui, sans-serif; background: #f5f5f5; color: #222; padding: 2rem; }
        h1 { margin-bottom: 1rem; font-size: 1.5rem; }
        .meta { color: #666; font-size: 0.875rem; margin-bottom: 1.5rem; }
        table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
        thead { background: #1a1a2e; color: #fff; }
        th { padding: 0.75rem 1rem; text-align: left; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }
        td { padding: 0.7rem 1rem; font-size: 0.875rem; border-bottom: 1px solid #eee; vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: #f9f9ff; }
        .source-badge { display: inline-block; background: #e8f0fe; color: #1a56db; padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.75rem; margin: 0.1rem; }
        .price { color: #15803d; font-weight: 500; }
        .no-data { color: #aaa; font-style: italic; }
        a { color: #1a56db; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .empty { text-align: center; padding: 4rem; color: #888; }
    </style>
</head>
<body>
    <h1>📅 Magdeburg Events</h1>
    <p class="meta">{{ count }} event{{ 's' if count != 1 else '' }} found &mdash; ordered by start date</p>

    {% if events %}
    <table>
        <thead>
            <tr>
                <th>#</th>
                <th>Event</th>
                <th>Start</th>
                <th>End</th>
                <th>Venue</th>
                <th>Address</th>
                <th>Price</th>
                <th>Source</th>
                <th>Keywords</th>
            </tr>
        </thead>
        <tbody>
        {% for e in events %}
            <tr>
                <td>{{ loop.index }}</td>
                <td>
                    {% if e.source_url %}
                        <a href="{{ e.source_url[0] }}" target="_blank" rel="noopener">{{ e.event_name }}</a>
                    {% else %}
                        {{ e.event_name }}
                    {% endif %}
                </td>
                <td>{{ e.start_iso.strftime('%d.%m.%Y %H:%M') if e.start_iso else '<span class="no-data">—</span>' | safe }}</td>
                <td>{{ e.end_iso.strftime('%d.%m.%Y %H:%M') if e.end_iso else '<span class="no-data">—</span>' | safe }}</td>
                <td>{{ e.venue_name or '—' }}</td>
                <td>{{ e.address or '—' }}</td>
                <td>{% if e.price %}<span class="price">{{ e.price }}</span>{% else %}<span class="no-data">—</span>{% endif %}</td>
                <td>
                    {% for s in (e.source or []) %}
                        <span class="source-badge">{{ s }}</span>
                    {% endfor %}
                </td>
                <td>{{ (e.keywords or []) | join(', ') or '—' }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
        <div class="empty">No events in database yet. Scraper may still be running.</div>
    {% endif %}
</body>
</html>
"""

@app.route("/")
def index():
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM events ORDER BY start_iso ASC NULLS LAST;")
            events = cur.fetchall()
        conn.close()
    except Exception as e:
        return f"<pre>DB error: {e}</pre>", 500

    return render_template_string(HTML, events=events, count=len(events))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)