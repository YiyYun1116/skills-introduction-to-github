"""Signal Atlas local application. No secrets or model credentials required."""
import asyncio
import csv
import io
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import storage
from .access import check_access, validate_access_config
from .analysis import analyze_articles, market_summary
from .briefing import build_briefing
from .demo import DEMO_SOURCES, demo_articles
from .diagnostics import build_diagnostics
from .ingestion import collector
from .sources import SOURCE_CATALOG

ROOT = Path(__file__).resolve().parent


def sync_minutes():
    try:
        minutes = int(os.environ.get('SIGNAL_ATLAS_SYNC_MINUTES','30'))
    except ValueError:
        raise ValueError('SIGNAL_ATLAS_SYNC_MINUTES 必须是整数，0 表示禁用自动采集') from None
    if minutes < 0:
        raise ValueError('SIGNAL_ATLAS_SYNC_MINUTES 不能为负数')
    return 0 if minutes == 0 else max(5,minutes)


@asynccontextmanager
async def lifespan(app):
    validate_access_config()
    storage.initialize()
    storage.recover_ingestion_runs()
    task = None
    minutes = sync_minutes()
    collector.configure(minutes)
    if minutes:
        task = asyncio.create_task(collector.run_schedule(minutes))
    yield
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title='溯源 · Signal Atlas', version='0.1.0', lifespan=lifespan)


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = check_access(request)
    # A browser must not accept cross-site writes from unrelated websites.
    if response is None and request.method in {'POST','PUT','PATCH','DELETE'}:
        origin = request.headers.get('origin')
        if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and urlparse(origin).netloc != request.headers.get('host')):
            response = Response('Cross-origin write rejected', status_code=403)
    if response is None:
        response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/api/health')
def health():
    storage.get_metadata('health')
    return {'status':'ok','version':'0.1.0','database':'ok','quotes':'not_connected'}


def build_dashboard(mode=None):
    live = storage.list_articles()
    selected = mode or 'live'
    demo = selected == 'demo'
    articles = demo_articles() if demo else live
    sources = SOURCE_CATALOG + DEMO_SOURCES if demo else SOURCE_CATALOG
    clusters = analyze_articles(articles, sources)
    for cluster in clusters:
        cluster['briefing'] = build_briefing(cluster)
    ingestion = collector.status()
    status_by_id = {item['source_id']:item for item in ingestion.get('results',[])}
    catalog = [dict(source, status=status_by_id.get(source['id'],{}).get('status','not_checked'),
                    last_check=status_by_id.get(source['id'])) for source in sources]
    traced_originals = [a for c in clusters for a in c['articles']
                        if not a.get('duplicate_of') and a.get('source_domain_match') and a.get('source_known')]
    return {
        'mode':selected,'generated_at':storage.now(),'clusters':clusters,'sources':catalog,
        'stats':{'articles':len(articles),'clusters':len(clusters),
                 'duplicates':sum(c.get('duplicate_count',0) for c in clusters),
                 'primary_sources':len({a['independence_group'] for a in traced_originals if a['source_tier']=='primary'}),
                 'independent_sources':len({a['independence_group'] for a in traced_originals})},
        'market_summary':market_summary(clusters),'ingestion':ingestion,
        'diagnostics':build_diagnostics(live,ingestion),
        'coverage':{'asset_classes':['equity','futures','options','crypto','macro'],
                    'regions':['US','CN','HK','EU','GLOBAL'], 'quotes_connected':False,
                    'research':'公开论文订阅；未建立作者权威排名，预印本不等于同行评审。',
                    'verification':'来源证据评分只衡量可追溯性，不是真实性概率；转载不算独立证据。',
                    'sentiment':'基于新闻措辞的词典指标，不代表投资者持仓或实时市场情绪。',
                    'limitations':['仅覆盖已配置且成功抓取的公开订阅，非全网或全资产实时覆盖。',
                                   '跨语言聚类、矛盾识别和全文事实核验仍需人工复核。',
                                   '未接入证券行情、期权链、付费新闻或收益预测模型。'],
                    'live_article_count':len(live), 'article_window_limit':2000}
    }


@app.get('/api/dashboard')
def dashboard(mode: Literal['live','demo'] | None = None):
    return build_dashboard(mode)


@app.get('/api/diagnostics')
def diagnostics():
    return build_diagnostics(storage.list_articles(),collector.status())


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_ids: list[str] | None = Field(default=None,max_length=20)


@app.post('/api/ingest',status_code=202)
def ingest(body: IngestRequest, tasks: BackgroundTasks):
    known = {s['id'] for s in SOURCE_CATALOG}
    if body.source_ids is not None and (not body.source_ids or not set(body.source_ids).issubset(known)):
        raise HTTPException(422,'请选择有效的数据源')
    if not collector.reserve():
        return {'status':'running'}
    tasks.add_task(collector.run_reserved,body.source_ids)
    return {'status':'started'}


class WatchItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str = Field(min_length=1,max_length=40)
    name: str = Field(min_length=1,max_length=80)
    asset_class: Literal['equity','futures','options','crypto','macro']
    region: Literal['US','CN','HK','EU','GLOBAL'] = 'GLOBAL'
    note: str = Field(default='',max_length=500)

    @field_validator('symbol','name','note')
    @classmethod
    def clean(cls,value,info):
        value = value.strip()
        if info.field_name != 'note' and not value:
            raise ValueError('不能为空')
        if any(ord(c)<32 for c in value):
            raise ValueError('不允许控制字符')
        return value.upper() if info.field_name == 'symbol' else value


@app.get('/api/watchlist')
def watchlist():
    return {'items':storage.list_watchlist()}


@app.post('/api/watchlist',status_code=201)
def add_watchlist(item: WatchItem):
    try:
        return storage.add_watchlist(item.model_dump())
    except sqlite3.IntegrityError:
        raise HTTPException(409,'该资产已在关注列表中')


@app.delete('/api/watchlist/{item_id}')
def remove_watchlist(item_id: str):
    if not storage.delete_watchlist(item_id):
        raise HTTPException(404,'关注项不存在')
    return {'status':'deleted'}


def csv_safe(value):
    value = str(value or '')
    return "'"+value if value.lstrip().startswith(('=','+','-','@','\t','\r')) else value


@app.get('/api/export')
def export(mode: Literal['live','demo'] | None = None):
    data = build_dashboard(mode)
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['模式','话题','来源证据状态（非真实性概率）','独立来源数','重复内容数','措辞情绪','资料日期类型','资料日期','原文链接'])
    for cluster in data['clusters']:
        writer.writerow([csv_safe(v) for v in [data['mode'],cluster['title'],cluster['verification']['label'],
                         cluster['independent_sources'],cluster['duplicate_count'],cluster['sentiment']['label'],
                         {'feed_announcement':'订阅公告（非原始发表）','filing_accepted':'SEC 受理时间（非报告期）'}.get(cluster.get('latest_date_basis'),'发布'),
                         cluster.get('latest_at'), ' | '.join(a['url'] for a in cluster['articles'])]])
    return Response('\ufeff'+stream.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="signal-atlas-{data["mode"]}.csv"'})


@app.get('/')
def index():
    return FileResponse(ROOT / 'static' / 'index.html')


app.mount('/static', StaticFiles(directory=ROOT/'static'),name='static')
