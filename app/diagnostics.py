"""Read-only operational checks. These are not financial fact verification."""
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .sources import SOURCE_CATALOG


def _age(value, current):
    try:
        timestamp = datetime.fromisoformat(value.replace('Z','+00:00'))
        if timestamp.tzinfo is None:
            return None
        return (current-timestamp).total_seconds()
    except (AttributeError,TypeError,ValueError):
        return None


def build_diagnostics(articles, ingestion, current=None):
    current = current or datetime.now(timezone.utc)
    checks = []
    def add(identifier,title,status,detail,action=None):
        checks.append({'id':identifier,'title':title,'status':status,'detail':detail,'action':action})

    schedule = ingestion.get('schedule',{})
    if not schedule.get('enabled'):
        add('scheduler','后台自动更新','warning','当前自动更新已关闭，资料只能手动同步。','开启自动采集后保持网站服务运行。')
    elif not schedule.get('scheduler_running'):
        add('scheduler','后台自动更新','error','自动更新已启用，但调度器当前未运行。','检查服务日志并重启采集服务。')
    else:
        due = _age(schedule.get('next_run_at'),current)
        overdue = due is not None and due > 120 and not ingestion.get('running')
        add('scheduler','后台自动更新','error' if overdue else 'pass',
            '下一次计划已逾期超过两分钟。' if overdue else '后台调度正在运行；关闭浏览器不会停止采集。',
            '检查服务状态及最近更新记录。' if overdue else None)
    add('storage','资料库读取','pass','已成功读取真实资料与采集记录。')

    last = ingestion.get('last_run_summary') or {}
    history = ingestion.get('history') or []
    completed = next((run for run in history if run.get('status') != 'running'),last)
    if ingestion.get('error') or completed.get('status') in {'error','interrupted'}:
        add('last_run','最近采集结果','error','最近一次采集失败或中断；旧来源成功记录不能代表本次已完成。','查看最近更新记录与服务日志，修复后重新同步。')
    elif completed.get('status') == 'partial':
        add('last_run','最近采集结果','warning','最近一次只有部分来源成功，资料覆盖可能不完整。','检查失败来源及重试记录。')
    elif completed.get('status') == 'ok':
        add('last_run','最近采集结果','pass','最近一次采集已完成，资料与批次统计一起保存。')
    else:
        add('last_run','最近采集结果','warning','尚无已完成的采集批次记录。','等待采集完成后重新检查。')

    states = {row['source_id']:row for row in ingestion.get('results',[])}
    enabled = [source for source in SOURCE_CATALOG if source['enabled']]
    failed = [source for source in enabled if states.get(source['id'],{}).get('status')=='error']
    unchecked = [source for source in enabled if source['id'] not in states]
    good = sum(states.get(source['id'],{}).get('status') in {'ok','empty'} for source in enabled)
    add('sources','来源最近连接','warning' if failed or unchecked else 'pass',
        f'{good} / {len(enabled)} 个来源最近读取成功；{len(failed)} 个失败，{len(unchecked)} 个尚未检查。',
        '查看下方各来源的错误与重试记录。' if failed or unchecked else None)
    stale = []
    threshold = max(7200,schedule.get('interval_minutes',30)*180)
    for source in enabled:
        row = states.get(source['id'],{})
        success = row.get('last_success_at') or (row.get('checked_at') if row.get('status') in {'ok','empty'} else None)
        age = _age(success,current)
        if age is None or age > threshold or age < -300:
            stale.append(source)
    add('freshness','来源检查时效','warning' if stale else 'pass',
        f'{len(stale)} 个来源缺少近期成功读取记录；依据读取时间检查，旧文章不等于采集失败。' if stale
        else '所有启用来源均有近期成功读取记录。',
        '确认网站持续运行，并检查受限来源。' if stale else None)

    add('live_data','真实材料','pass' if articles else 'warning',
        f'本次检查 {len(articles)} 条真实库材料，最多检查最近 2,000 条。' if articles else '真实资料库暂为空，页面不会自动填入演示内容。',
        None if articles else '等待首次采集，或在来源页手动同步。')
    demos = sum(bool(article.get('is_demo')) for article in articles)
    add('separation','演示与真实数据分离','error' if demos else 'pass',
        f'发现 {demos} 条演示标记，请停止将其用于研究。' if demos else '本次检查的真实库中没有演示标记。',
        '检查导入流程，隔离错误记录。' if demos else None)
    catalog = {source['id']:source for source in SOURCE_CATALOG}
    unknown,undated = 0,0
    for article in articles:
        source = catalog.get(article.get('source_id'))
        try:
            link = urlsplit(article.get('url',''))
            host = (link.hostname or '').removeprefix('www.')
            expected = (urlsplit(source['url']).hostname or '').removeprefix('www.') if source else ''
            explicit = source.get('article_hosts',[]) if source else []
            traceable = bool(source and host and expected and (host==expected or host.endswith('.'+expected) or link.hostname in explicit)
                             and link.scheme=='https' and not link.username and not link.password)
        except (TypeError,ValueError):
            traceable = False
        unknown += not traceable
        undated += all(_age(article.get(key),current) is None for key in ('published_at','announced_at','accepted_at'))
    add('provenance','原始来源链接','warning' if unknown or not articles else 'pass',
        '暂无真实材料可检查。' if not articles else f'{unknown} 条材料缺少有效的登记来源 HTTPS 链接。' if unknown else '本次检查的材料链接均能对应登记来源；这不代表内容已证实。',
        '打开相关材料核对发布主体和原文。' if unknown else None)
    add('dates','资料时间口径','warning' if undated or not articles else 'pass',
        '暂无真实材料可检查。' if not articles else f'{undated} 条材料缺少可解析的发表、公告或受理时间。' if undated else '材料时间可解析；论文公告和财报受理时间分别保留。',
        '缺日期材料应查阅原文，不按抓取时间冒充新发布。' if undated else None)
    filings = sum(article.get('kind')=='filing' for article in articles)
    add('filings','公司财报覆盖','pass' if filings else 'warning',
        f'已收录 {filings} 条美国 SEC 申报索引；仅包含最近订阅，未解析完整报表或覆盖全球公司。' if filings
        else '已配置美国 SEC 10-K / 10-Q，当前检查窗口尚无成功收录的申报。',
        '全文财务指标、历史回补及其他市场公司披露仍待接入。')
    media = sum(catalog.get(article.get('source_id'),{}).get('kind')=='news' for article in articles)
    add('news','财经媒体覆盖','pass' if media else 'warning',
        f'检查窗口包含 {media} 条已登记媒体材料，覆盖范围以来源目录为准。' if media else '当前以官方公告、SEC 申报和论文为主，尚无成功收录的综合财经媒体订阅。',
        '接入可访问且许可明确的财经新闻订阅，保留原创与转载关系。')
    add('quotes','行情与投资预测','warning','实时行情、期权链、财务指标模型和经过回测的收益预测尚未接入。',
        '现阶段仅提供来源追踪与条件情景分析。')
    summary = {status:sum(check['status']==status for check in checks) for status in ('pass','warning','error')}
    return {'generated_at':current.isoformat(),'status':'needs_attention' if summary['warning'] or summary['error'] else 'ok',
            'summary':summary,'checks':checks,
            'scope':'检查当前服务、最近采集记录及最多 2,000 条真实材料；不代替全文事实核验、全历史审计或外部可用性探测。'}
