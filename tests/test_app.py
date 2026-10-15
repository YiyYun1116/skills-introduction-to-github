import csv
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import storage
from app.main import app, csv_safe, sync_minutes


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'SIGNAL_ATLAS_DB':self.temp.name+'/test.sqlite3','SIGNAL_ATLAS_SYNC_MINUTES':'0'})
        self.environment.start()
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.environment.stop()
        self.temp.cleanup()

    def test_dashboard_demo_and_empty_live_are_separate(self):
        self.assertEqual(self.client.get('/api/dashboard').json()['mode'],'live')
        demo = self.client.get('/api/dashboard?mode=demo').json()
        self.assertEqual(demo['mode'],'demo')
        self.assertGreater(demo['stats']['clusters'],3)
        self.assertGreater(demo['stats']['duplicates'],0)
        self.assertTrue(all(a['is_demo'] for c in demo['clusters'] for a in c['articles']))
        live = self.client.get('/api/dashboard?mode=live').json()
        self.assertEqual(live['mode'],'live')
        self.assertEqual(live['stats']['articles'],0)
        self.assertEqual(live['clusters'],[])
        self.assertEqual(self.client.get('/api/dashboard?mode=fake').status_code,422)

    def test_watchlist_persistence_uniqueness_and_validation(self):
        item={'symbol':'aapl','name':'Apple','asset_class':'equity','region':'US','note':'观察财报'}
        response=self.client.post('/api/watchlist',json=item)
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.json()['symbol'],'AAPL')
        self.assertEqual(len(self.client.get('/api/watchlist').json()['items']),1)
        self.assertEqual(self.client.post('/api/watchlist',json=item).status_code,409)
        self.assertEqual(self.client.post('/api/watchlist',json=dict(item,asset_class='invalid')).status_code,422)
        self.assertEqual(self.client.post('/api/watchlist',json=dict(item,symbol='   ')).status_code,422)
        self.assertEqual(self.client.delete('/api/watchlist/'+response.json()['id']).status_code,200)
        self.assertEqual(self.client.get('/api/watchlist').json()['items'],[])

    def test_reject_cross_site_mutation(self):
        response=self.client.post('/api/ingest',json={},headers={'Origin':'https://malicious.example'})
        self.assertEqual(response.status_code,403)

    def test_export_has_mode_and_protects_spreadsheet_formulas(self):
        response=self.client.get('/api/export?mode=demo')
        self.assertEqual(response.status_code,200)
        rows=list(csv.reader(io.StringIO(response.text.lstrip('\ufeff'))))
        self.assertGreater(len(rows),3)
        self.assertEqual(rows[1][0],'demo')
        self.assertTrue(csv_safe('=HYPERLINK("x")').startswith("'"))
        self.assertTrue(csv_safe('  +1').startswith("'"))

    def test_ingest_failure_keeps_previous_data_and_records_result(self):
        from app.sources import SOURCE_CATALOG
        source_id=SOURCE_CATALOG[0]['id']
        result={'articles':[],'results':[{'source_id':source_id,'status':'error','article_count':0,
                                         'error':'network unavailable','checked_at':storage.now()}]}
        with patch('app.ingestion.fetch_all',return_value=result):
            self.assertEqual(self.client.post('/api/ingest',json={'source_ids':[source_id]}).status_code,202)
        data=self.client.get('/api/dashboard?mode=live').json()
        self.assertFalse(data['ingestion']['running'])
        self.assertEqual(data['ingestion']['results'][0]['status'],'error')
        self.assertEqual(data['stats']['articles'],0)
        self.assertEqual(self.client.post('/api/ingest',json={'source_ids':['unknown']}).status_code,422)

    def test_persist_live_article_and_auto_select_live(self):
        from app.sources import SOURCE_CATALOG
        article={'id':'live-test','source_id':SOURCE_CATALOG[0]['id'],'published_at':None,'ingested_at':storage.now(),
                 'title':'Policy update','summary':'A primary source update','url':'https://www.federalreserve.gov/newsevents.htm',
                 'is_demo':False,'asset_classes':['macro'],'regions':['US'],'entities':[],'tags':[],'kind':'policy','language':'en'}
        storage.save_articles([article])
        storage.save_articles([article])
        data=self.client.get('/api/dashboard').json()
        self.assertEqual(data['mode'],'live')
        self.assertEqual(data['stats']['articles'],1)
        with self.assertRaises(ValueError):
            storage.save_articles([dict(article,is_demo=True)])

    def test_health_and_security_headers(self):
        response=self.client.get('/api/health')
        self.assertEqual(response.json()['status'],'ok')
        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])

    def test_system_checks_use_live_data_even_in_demo_view(self):
        demo = self.client.get('/api/dashboard?mode=demo').json()
        checks = {row['id']:row for row in demo['diagnostics']['checks']}
        self.assertEqual(checks['live_data']['status'],'warning')
        self.assertIn('暂为空',checks['live_data']['detail'])
        response = self.client.get('/api/diagnostics')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['summary'],demo['diagnostics']['summary'])

    def test_repeat_fetch_preserves_first_seen_for_undated_article(self):
        item={'id':'undated','source_id':'fed_press','title':'Undated article','ingested_at':'2026-01-01T00:00:00Z'}
        storage.save_articles([item])
        storage.save_articles([dict(item,ingested_at='2026-01-02T00:00:00Z')])
        saved=storage.list_articles()[0]
        self.assertEqual(saved['ingested_at'],'2026-01-01T00:00:00Z')
        self.assertEqual(saved['last_seen_at'],'2026-01-02T00:00:00Z')

    def test_invalid_scheduler_config_fails_clearly(self):
        with patch.dict(os.environ,{'SIGNAL_ATLAS_SYNC_MINUTES':'invalid'}):
            with self.assertRaises(ValueError):
                sync_minutes()
        with patch.dict(os.environ,{'SIGNAL_ATLAS_SYNC_MINUTES':'1'}):
            self.assertEqual(sync_minutes(),5)

    def test_source_statistics_count_publishers_not_feeds_or_untraceable_claims(self):
        base={'published_at':'2026-01-01T00:00:00Z','ingested_at':storage.now(),'summary':'Source statement',
              'is_demo':False,'asset_classes':['macro'],'regions':['US'],'entities':[],'tags':[],'kind':'policy'}
        storage.save_articles([
            dict(base,id='a',source_id='fed_press',title='Federal Reserve policy announcement',url='https://www.federalreserve.gov/a'),
            dict(base,id='b',source_id='fed_speeches',title='A speech on payment infrastructure',url='https://www.federalreserve.gov/b'),
            dict(base,id='c',source_id='ecb_press',title='Untraceable statement',url='https://unrelated.example/c'),
        ])
        stats=self.client.get('/api/dashboard?mode=live').json()['stats']
        self.assertEqual(stats['primary_sources'],1)
        self.assertEqual(stats['independent_sources'],1)


if __name__=='__main__':
    unittest.main()
