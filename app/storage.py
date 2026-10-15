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
        CREATE TABLE IF NOT EXISTS ingestion_runs (
          id TEXT PRIMARY KEY, started_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
        );
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


def save_articles(articles, db=None):
    if db is None:
        with connection() as transaction:
            return save_articles(articles, db=transaction)
    counts = {'new_count':0, 'updated_count':0, 'unchanged_count':0, 'by_source':{}}
    # Retrieval timestamps and transport changes do not make old content new.
    def content(article):
        return {key:value for key,value in article.items()
                if key not in {'ingested_at','last_seen_at','retrieval_method'}}
    for article in articles:
        if article.get('is_demo'):
            raise ValueError('演示数据不能写入实时数据库')
        article = dict(article)
        previous = db.execute('SELECT ingested_at,payload FROM articles WHERE id=?', (article['id'],)).fetchone()
        category = ('new_count' if previous is None else
                    'unchanged_count' if content(json.loads(previous['payload'])) == content(article) else 'updated_count')
        counts[category] += 1
        source_counts = counts['by_source'].setdefault(article['source_id'],
            {'new_count':0,'updated_count':0,'unchanged_count':0})
        source_counts[category] += 1
        article['last_seen_at'] = article.get('ingested_at') or now()
        article['ingested_at'] = previous['ingested_at'] if previous else article['last_seen_at']
        db.execute('''INSERT INTO articles VALUES (?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
          published_at=excluded.published_at''',
          (article['id'], article['source_id'], article.get('published_at'),
           article.get('ingested_at') or now(), json.dumps(article, ensure_ascii=False)))
    return counts


def begin_ingestion_run(record):
    with connection() as db:
        db.execute('INSERT INTO ingestion_runs VALUES (?,?,?,?)',
                   (record['id'],record['started_at'],record['status'],json.dumps(record,ensure_ascii=False)))


def finish_ingestion_run(record, metadata, db=None):
    """Commit source states and the run, optionally in the article transaction."""
    if db is None:
        with connection() as transaction:
            return finish_ingestion_run(record, metadata, db=transaction)
    db.execute('INSERT INTO ingestion_runs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE '
               'SET status=excluded.status,payload=excluded.payload',
               (record['id'],record['started_at'],record['status'],json.dumps(record,ensure_ascii=False)))
    db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
               ('ingestion',json.dumps(metadata,ensure_ascii=False)))
    db.execute('DELETE FROM ingestion_runs WHERE id NOT IN '
               '(SELECT id FROM ingestion_runs ORDER BY started_at DESC LIMIT 100)')


def list_ingestion_runs(limit=20):
    with connection() as db:
        rows = db.execute('SELECT payload FROM ingestion_runs ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
    return [json.loads(row['payload']) for row in rows]


def recover_ingestion_runs():
    """A previous process cannot still be collecting in our single-instance setup."""
    with connection() as db:
        for row in db.execute("SELECT payload FROM ingestion_runs WHERE status='running'").fetchall():
            record = json.loads(row['payload'])
            record.update(status='interrupted',finished_at=now(),
                          error='上次采集因服务重启而中断；启动后会重新检查来源。')
            db.execute('UPDATE ingestion_runs SET status=?,payload=? WHERE id=?',
                       ('interrupted',json.dumps(record,ensure_ascii=False),record['id']))


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
