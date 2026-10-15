"""Optional Chromium smoke check against a running local server.

Requires Playwright and Chromium. Test-owned watchlist entries are removed.
"""
import json
import os
from pathlib import Path
from uuid import uuid4
from playwright.sync_api import sync_playwright, expect

base = os.environ.get('SIGNAL_ATLAS_TEST_URL','http://127.0.0.1:8000')
artifacts = Path(os.environ.get('SIGNAL_ATLAS_SCREENSHOTS','.local/screenshots'))
artifacts.mkdir(parents=True,exist_ok=True)

with sync_playwright() as playwright:
    browser=playwright.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
    context=browser.new_context(viewport={'width':1440,'height':1050},device_scale_factor=1,accept_downloads=True)
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
    item_id=None
    try:
        page.goto(base,wait_until='networkidle')
        expect(page.locator('.topic-card').first).to_be_visible()
        assert 'LIVE' in page.locator('#mode-label').inner_text(), 'Real feed dataset must be available for this check'
        expect(page.locator('.update-bar')).to_be_visible()
        page.screenshot(path=str(artifacts/'overview.png'),full_page=True)
        page.locator('[data-view="research"]').click()
        expect(page.locator('.research-card').first).to_be_visible()
        assert '作者：' in page.locator('#page-content').inner_text()
        page.locator('.research-card [data-action="open-cluster"]').first.click()
        expect(page.get_by_role('dialog')).to_be_visible()
        expect(page.get_by_text('组合解读与政策拆解',exact=False)).to_be_visible()
        assert page.locator('.detail-drawer a[target="_blank"]').count()>0
        page.screenshot(path=str(artifacts/'research-detail.png'),full_page=True)
        page.keyboard.press('Escape')
        expect(page.get_by_role('dialog')).to_have_count(0)
        page.locator('[data-view="sources"]').click()
        catalog=context.request.get(base+'/api/dashboard?mode=live').json()['sources']
        expect(page.locator('.source-card')).to_have_count(len([source for source in catalog if not source.get('is_demo')]))
        expect(page.locator('.schedule-panel')).to_be_visible()
        page.locator('[data-action="sync-source"][data-id="fed_press"]').click()
        expect(page.locator('.sync-banner')).to_have_count(0,timeout=25000)
        status=context.request.get(base+'/api/dashboard?mode=live').json()['ingestion']
        assert len(status['last_run_results'])==1
        assert status['last_run_results'][0]['status']=='ok'
        page.locator('#mode-switch').click()
        expect(page.locator('#mode-label')).to_contain_text('DEMO')
        page.locator('[data-view="themes"]').click()
        page.locator('[data-action="asset"][data-id="crypto"]').click()
        expect(page.locator('.topic-card')).to_have_count(1)
        page.locator('#global-search').fill('不存在的主题_ATLAS_8472')
        expect(page.locator('.topic-card')).to_have_count(0)
        page.locator('[data-action="reset-filters"]').click()
        expect(page.locator('.topic-card')).to_have_count(7)
        with page.expect_download() as download_info:
            page.locator('a[download]').click()
        download=download_info.value
        assert 'demo' in download.suggested_filename
        download.save_as(str(artifacts/'demo-export.csv'))
        page.locator('[data-view="watchlist"]').click()
        symbol='ATLAS-UI-'+uuid4().hex[:8].upper()
        page.locator('#watch-symbol').fill(symbol)
        page.locator('#watch-name').fill('<img src=x onerror="window.__unsafe=1">')
        page.locator('#watch-note').fill('浏览器验证临时条目')
        page.locator('#watch-form button[type="submit"]').click()
        expect(page.get_by_text(symbol,exact=True)).to_be_visible()
        assert page.evaluate('window.__unsafe') is None
        rows=context.request.get(base+'/api/watchlist').json()['items']
        item_id=next(item['id'] for item in rows if item['symbol']==symbol)
        page.reload(wait_until='networkidle')
        expect(page.get_by_text(symbol,exact=True)).to_be_visible()
        page.locator(f'[data-action="related"][data-id="{item_id}"]').click()
        expect(page.locator('.topic-card')).to_have_count(0)
        expect(page.locator('.search-active')).to_contain_text(symbol)
        page.locator('[data-view="watchlist"]').click()
        page.locator(f'[data-action="delete-watch"][data-id="{item_id}"]').click()
        expect(page.get_by_text(symbol,exact=True)).to_have_count(0)
        item_id=None
        fixture=context.request.get(base+'/api/dashboard?mode=demo').json()
        import copy
        candidates=[]
        for index,(title,region) in enumerate([('The industry outlook improves','US'),('NYSE:T quarterly filing','US'),('NYSE:T different market','CN')]):
            cluster=copy.deepcopy(fixture['clusters'][0])
            cluster.update(id='candidate-'+str(index),title=title,summary='A public company report.',entities=[],asset_classes=['equity'],regions=[region])
            candidates.append(cluster)
        fixture['clusters']=candidates
        fixture['stats']['clusters']=3
        check_page=context.new_page()
        check_page.route('**/api/dashboard*',lambda route:route.fulfill(json=fixture))
        check_page.route('**/api/watchlist',lambda route:route.fulfill(json={'items':[{'id':'short-symbol','symbol':'T','name':'AT&T','asset_class':'equity','region':'US','note':''}]}))
        check_page.goto(base+'/#watchlist',wait_until='networkidle')
        expect(check_page.locator('[data-action="related"]')).to_contain_text('1 个候选主题')
        check_page.locator('[data-action="related"]').click()
        expect(check_page.locator('.topic-card')).to_have_count(1)
        expect(check_page.locator('.topic-title-button')).to_contain_text('NYSE:T quarterly filing')
        check_page.close()
        # Search must still find an item beyond the first rendered page.
        paged=copy.deepcopy(fixture)
        paged['clusters']=[dict(copy.deepcopy(candidates[0]),id='page-'+str(index),
                              title='Unrendered target' if index==64 else 'News item '+str(index)) for index in range(65)]
        paging_page=context.new_page()
        paging_page.route('**/api/dashboard*',lambda route:route.fulfill(json=paged))
        paging_page.goto(base+'/#themes',wait_until='networkidle')
        expect(paging_page.locator('.topic-card')).to_have_count(30)
        paging_page.locator('[data-action="load-more"]').click()
        expect(paging_page.locator('.topic-card')).to_have_count(60)
        paging_page.locator('#global-search').fill('Unrendered target')
        expect(paging_page.locator('.topic-card')).to_have_count(1)
        expect(paging_page.locator('.topic-title-button')).to_contain_text('Unrendered target')
        paging_page.close()
        # Isolated API fixtures exercise states that are difficult to create on demand.
        ui_fixture=copy.deepcopy(fixture)
        ui_fixture['mode']='live'
        ui_fixture['sources']=[{'id':'sec_10q','name':'SEC 季度申报','kind':'official','tier':'primary','region':'US','language':'en','url':'https://www.sec.gov','description':'公开申报索引','independence_group':'sec','enabled':True}]
        filing=copy.deepcopy(candidates[0])
        filing.update(id='fixture-filing',is_demo=False,title='10-Q - Example Company',summary='Quarterly filing index.',asset_classes=['equity'],regions=['US'],source_ids=['sec_10q'],latest_at='2026-01-15T12:00:00Z',latest_date_basis='filing_accepted')
        filing['articles']=[{'id':'fixture-report','title':'10-Q - Example Company','summary':'Filing index only.','source_id':'sec_10q','url':'https://www.sec.gov/Archives/example','is_demo':False,'kind':'filing','company_name':'Example Company','company_cik':'0000123456','filing_form':'10-Q','accession_number':'0000123456-26-000001','filing_date':'2026-01-15','accepted_at':'2026-01-15T12:00:00Z','announced_at':'2026-01-15T12:00:00Z','published_at':None,'activity_date_basis':'filing_accepted'}]
        filing['briefing']['filing']={'items':[{'article_id':'fixture-report','company_name':'Example Company','company_cik':'0000123456','form':'10-Q','accession_number':'0000123456-26-000001','filing_date':'2026-01-15','accepted_at':'2026-01-15T12:00:00Z','reporting_period':'','status':'申报索引，财务数字尚未核对'}],'financial_metrics':{'status':'not_extracted','explanation':'报表数值尚未从正文提取和核对。'},'checks':['核对报告期间'],'limitations':['索引不是财务报表全文']}
        ui_fixture['clusters']=[filing]
        ui_fixture['diagnostics']={'generated_at':'2026-01-15T12:00:00Z','status':'needs_attention','summary':{'pass':1,'warning':1,'error':0},'checks':[{'id':'database','title':'真实资料','status':'pass','detail':'已收录公开资料'},{'id':'quotes','title':'行情覆盖','status':'warning','detail':'实时行情尚未接入','action':'添加有授权的数据来源'}],'scope':'检查当前服务及真实材料'}
        run={'id':'run-0','trigger':'scheduled','status':'partial','started_at':'2026-01-15T11:59:00Z','finished_at':'2026-01-15T12:00:00Z','new_count':2,'updated_count':1,'unchanged_count':12,'source_count':2,'successful_sources':1,'failed_sources':1}
        ui_fixture['ingestion']={'running':False,'last_run':'2026-01-15T12:00:00Z','schedule':{'enabled':True,'interval_minutes':30,'next_run_at':'2026-01-15T12:30:00Z','scheduler_running':True},'last_run_summary':run,'history':[dict(run,id='run-'+str(index)) for index in range(7)],'results':[{'source_id':'sec_10q','status':'error','error':'读取受限','last_success_at':'2026-01-15T10:00:00Z','checked_at':'2026-01-15T12:00:00Z','new_count':2,'updated_count':1,'unchanged_count':12,'consecutive_failures':2,'attempts':3,'retry_count':1}]}
        ui_page=context.new_page()
        ui_page.on('pageerror',lambda error:errors.append(str(error)))
        ui_page.route('**/api/dashboard*',lambda route:route.fulfill(json=ui_fixture))
        ui_page.goto(base,wait_until='networkidle')
        expect(ui_page.locator('.update-bar')).to_contain_text('自动更新已开启')
        expect(ui_page.locator('.update-bar')).to_contain_text('20:30')
        expect(ui_page.locator('.update-bar')).to_contain_text('非新发表')
        ui_page.locator('[data-view="sources"]').click()
        expect(ui_page.locator('.run-row')).to_have_count(5)
        expect(ui_page.locator('.source-progress')).to_contain_text('请求 3 次 · 失败后重试 1 次')
        expect(ui_page.locator('.source-progress')).to_contain_text('连续 2 次采集失败')
        ui_page.locator('.diagnostics-panel summary').click()
        expect(ui_page.locator('.diagnostics-list')).to_be_visible()
        expect(ui_page.locator('.diagnostics-list')).to_contain_text('实时行情尚未接入')
        ui_page.locator('[data-action="diagnostics-refresh"]').click()
        expect(ui_page.locator('.diagnostics-list')).to_be_visible()
        ui_page.locator('[data-view="filings"]').click()
        expect(ui_page.locator('.filing-card')).to_have_count(1)
        expect(ui_page.locator('.filing-card')).to_contain_text('20:00')
        expect(ui_page.locator('.filing-card')).to_contain_text('待查阅报告正文')
        ui_page.locator('[data-action="filing-form"][data-id="10-K"]').click()
        expect(ui_page.locator('.filing-card')).to_have_count(0)
        ui_page.locator('[data-action="reset-filings"]').click()
        ui_page.locator('.filing-card [data-action="open-cluster"]').click()
        expect(ui_page.locator('.filing-briefing')).to_contain_text('公司申报拆解')
        expect(ui_page.locator('.article-source')).to_contain_text('申报受理')
        ui_page.keyboard.press('Escape')
        for enabled,running,label in [(False,False,'自动更新已关闭'),(True,False,'定时更新已暂停'),(True,True,'自动更新已开启')]:
            ui_fixture['ingestion']['schedule'].update(enabled=enabled,scheduler_running=running)
            ui_page.goto(base+'/#overview',wait_until='networkidle')
            ui_page.reload(wait_until='networkidle')
            expect(ui_page.locator('.update-bar')).to_contain_text(label)
        ui_fixture['ingestion']['running']=True
        ui_fixture['ingestion']['schedule']['next_run_at']=None
        ui_page.reload(wait_until='networkidle')
        expect(ui_page.locator('.update-bar')).to_contain_text('本轮结束后安排下一次')
        ui_fixture['ingestion']['running']=False
        for width in (390,320):
            ui_page.set_viewport_size({'width':width,'height':844})
            for view in ('overview','filings','sources','research','watchlist'):
                ui_page.goto(base+'/#'+view,wait_until='networkidle')
                assert not ui_page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'{width}px overflow in {view}'
        ui_fixture['clusters']=[]
        ui_fixture['stats'].update(articles=0,clusters=0,duplicates=0,primary_sources=0,independent_sources=0)
        ui_page.goto(base+'/#overview',wait_until='networkidle')
        ui_page.reload(wait_until='networkidle')
        expect(ui_page.locator('.topic-card')).to_have_count(0)
        expect(ui_page.locator('.demo-banner')).to_have_count(0)
        expect(ui_page.locator('#mode-label')).to_contain_text('LIVE')
        ui_page.goto(base+'/#filings',wait_until='networkidle')
        expect(ui_page.locator('.empty-state')).to_contain_text('暂无已采集的公司财报')
        ui_page.close()
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#mobile-menu').click()
        page.locator('[data-view="overview"]').click()
        expect(page.locator('.topic-card').first).to_be_visible()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Mobile horizontal overflow'
        page.screenshot(path=str(artifacts/'mobile.png'),full_page=True)
        assert not errors, errors
        print(json.dumps({'status':'passed','checks':['real dashboard','research authors and evidence','briefing drawer','single-source refresh','dynamic source count','demo filters and search','CSV download','watchlist persistence','untrusted text escaping','related filtering','short ticker boundaries and region matching','watchlist deletion','schedule states and history','retry count fidelity','system diagnostics','filing dates and form filters','empty live dataset','320/390 mobile layout','no browser errors'],'screenshots':str(artifacts)},ensure_ascii=False))
    finally:
        if item_id:
            context.request.delete(base+'/api/watchlist/'+item_id)
        browser.close()
