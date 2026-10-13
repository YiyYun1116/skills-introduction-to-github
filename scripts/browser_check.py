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
        expect(page.locator('.source-card')).to_have_count(9)
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
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#mobile-menu').click()
        page.locator('[data-view="overview"]').click()
        expect(page.locator('.topic-card').first).to_be_visible()
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Mobile horizontal overflow'
        page.screenshot(path=str(artifacts/'mobile.png'),full_page=True)
        assert not errors, errors
        print(json.dumps({'status':'passed','checks':['real dashboard','research authors and evidence','briefing drawer','single-source refresh','demo filters and search','CSV download','watchlist persistence','untrusted text escaping','related filtering','short ticker boundaries and region matching','watchlist deletion','mobile layout','no browser errors'],'screenshots':str(artifacts)},ensure_ascii=False))
    finally:
        if item_id:
            context.request.delete(base+'/api/watchlist/'+item_id)
        browser.close()
