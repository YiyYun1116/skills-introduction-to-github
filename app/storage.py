"""SQLite persistence; article content is always treated as untrusted input."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def database_path():
    return Path(os.environ.get('SIGNAL_ATLAS_DB', Path(__file__).resolve().parent.parent / '.local' / 'signal-atlas.sqlite3'))


@contextmanager
def connection():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL')
        yield db
        db.commit()
    finally:
        db.close()


def initialize():
    with connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS articles (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL, published_at TEXT,
          ingested_at TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS article_date ON articles(published_at);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watchlist (
          id TEXT PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL,
          asset_class TEXT NOT NULL, region TEXT NOT NULL, note TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(symbol, asset_class, region)
        );
        ''')
        # Correct older local records without confusing a feed update with a
        # paper's original publication. Raw article content remains intact.
        for row in db.execute("SELECT id,payload FROM articles WHERE source_id LIKE 'arxiv_%'").fetchall():
            article = json.loads(row['payload'])
            if not article.get('date_basis') and article.get('retrieval_method') == 'rss':
                article['announced_at'] = article.get('published_at')
                article['published_at'] = None
                article['date_basis'] = 'feed_announcement'
                db.execute('UPDATE articles SET published_at=NULL,payload=? WHERE id=?',
                           (json.dumps(article,ensure_ascii=False),row['id']))


def save_articles(articles):
    with connection() as db:
        for article in articles:
            if article.get('is_demo'):
                raise ValueError('演示数据不能写入实时数据库')
            article = dict(article)
            previous = db.execute('SELECT ingested_at FROM articles WHERE id=?', (article['id'],)).fetchone()
            article['last_seen_at'] = article.get('ingested_at') or now()
            article['ingested_at'] = previous['ingested_at'] if previous else article['last_seen_at']
            db.execute('''INSERT INTO articles VALUES (?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
              published_at=excluded.published_at''',
              (article['id'], article['source_id'], article.get('published_at'),
               article.get('ingested_at') or now(), json.dumps(article, ensure_ascii=False)))


def list_articles(limit=2000):
    with connection() as db:
        rows = db.execute("SELECT payload FROM articles ORDER BY COALESCE(json_extract(payload,'$.announced_at'), published_at, ingested_at) DESC LIMIT ?", (limit,)).fetchall()
    return [json.loads(row['payload']) for row in rows]


def set_metadata(key, value):
    with connection() as db:
        db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                   (key, json.dumps(value, ensure_ascii=False)))


def get_metadata(key, default=None):
    with connection() as db:
        row = db.execute('SELECT payload FROM metadata WHERE key=?', (key,)).fetchone()
    return json.loads(row['payload']) if row else default


def list_watchlist():
    with connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM watchlist ORDER BY created_at DESC')]


def add_watchlist(item):
    record = dict(item, id=uuid4().hex, created_at=now())
    with connection() as db:
        db.execute('INSERT INTO watchlist VALUES (:id,:symbol,:name,:asset_class,:region,:note,:created_at)', record)
    return record


def delete_watchlist(item_id):
    with connection() as db:
        return db.execute('DELETE FROM watchlist WHERE id=?', (item_id,)).rowcount > 0
