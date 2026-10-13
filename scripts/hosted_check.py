"""Validate hosted startup, browser authentication and restart persistence locally."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
PYTHON = os.environ.get('SIGNAL_ATLAS_TEST_PYTHON', sys.executable)


def launch(environment, log, base):
    process=subprocess.Popen([PYTHON,str(ROOT/'scripts/serve.py')],cwd='/',env=environment,stdout=log,stderr=log)
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError('Hosted process exited during startup; inspect the test log')
        try:
            with urllib.request.urlopen(base+'/api/health',timeout=0.5) as response:
                if json.load(response).get('status')=='ok':
                    return process
        except (urllib.error.URLError,TimeoutError):
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=10)
    raise RuntimeError('Hosted startup did not become ready')


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        process.wait(timeout=15)


with tempfile.TemporaryDirectory(prefix='signal-atlas-hosted-') as directory:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    password=secrets.token_urlsafe(32)
    environment=dict(os.environ,PORT=str(port),SIGNAL_ATLAS_ENV='production',
                     SIGNAL_ATLAS_ACCESS_PASSWORD=password,SIGNAL_ATLAS_ACCESS_USER='research',
                     SIGNAL_ATLAS_SYNC_MINUTES='0',SIGNAL_ATLAS_DB=str(Path(directory)/'persistent.sqlite3'))
    process=None
    with (Path(directory)/'server.log').open('w+') as log:
        try:
            process=launch(environment,log,base)
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
                anonymous=browser.new_context()
                assert anonymous.request.get(base+'/api/health').status==200
                assert anonymous.request.get(base+'/api/watchlist').status==401
                anonymous.close()
                context=browser.new_context(http_credentials={'username':'research','password':password},
                                           viewport={'width':390,'height':844},reduced_motion='reduce')
                page=context.new_page()
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base,wait_until='networkidle')
                expect(page.locator('.topic-card').first).to_be_visible()
                expect(page.locator('#mode-label')).to_contain_text('DEMO')
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                response=context.request.post(base+'/api/watchlist',data={'symbol':'HOSTED-CHECK','name':'Restart persistence','asset_class':'equity','region':'GLOBAL'})
                assert response.status==201
                stop(process)
                process=launch(environment,log,base)
                assert any(item['symbol']=='HOSTED-CHECK' for item in context.request.get(base+'/api/watchlist').json()['items'])
                page.reload(wait_until='networkidle')
                expect(page.locator('.topic-card').first).to_be_visible()
                assert not errors,errors
                browser.close()
            print(json.dumps({'status':'passed','checks':['clean hosted startup from different directory','PORT binding','public health check','private API denies anonymous access','mobile authenticated page','responsive layout','persistent watchlist after process restart','authenticated reload']},ensure_ascii=False))
        except Exception:
            log.flush()
            log.seek(0)
            print(log.read()[-6000:])
            raise
        finally:
            stop(process)
