from datetime import datetime, timezone
import unittest

from app.diagnostics import build_diagnostics
from app.sources import SOURCE_CATALOG


class DiagnosticsTests(unittest.TestCase):
    current = datetime(2026,10,15,8,tzinfo=timezone.utc)

    def check(self,articles,ingestion):
        data = build_diagnostics(articles,ingestion,self.current)
        self.assertEqual(sum(data['summary'].values()),len(data['checks']))
        return {row['id']:row for row in data['checks']}

    def healthy_schedule(self):
        return {'schedule':{'enabled':True,'scheduler_running':True,'interval_minutes':30,
                            'next_run_at':'2026-10-15T08:30:00Z'},'results':[
            {'source_id':s['id'],'status':'empty','last_success_at':'2026-10-15T07:55:00Z'} for s in SOURCE_CATALOG]}

    def test_empty_live_never_reports_complete_coverage(self):
        checks = self.check([],{'schedule':{'enabled':False}})
        for key in ('scheduler','live_data','provenance','dates','filings','news','quotes'):
            self.assertEqual(checks[key]['status'],'warning',key)

    def test_fresh_feed_with_old_article_is_not_a_failed_update(self):
        checks = self.check([{'source_id':'fed_press','url':'https://www.federalreserve.gov/a',
                              'published_at':'2020-01-01T00:00:00Z'}],self.healthy_schedule())
        for key in ('scheduler','sources','freshness','provenance','dates'):
            self.assertEqual(checks[key]['status'],'pass',key)

    def test_stopped_scheduler_stale_sources_and_unsafe_records_detected(self):
        data = self.healthy_schedule()
        data['schedule']['scheduler_running'] = False
        data['results'][0]['last_success_at'] = '2020-01-01T00:00:00Z'
        checks = self.check([{'source_id':'fed_press','url':'https://www.federalreserve.gov.attacker.example/a',
                              'is_demo':True,'published_at':'invalid'}],data)
        self.assertEqual(checks['scheduler']['status'],'error')
        self.assertEqual(checks['separation']['status'],'error')
        for key in ('freshness','dates','provenance'):
            self.assertEqual(checks[key]['status'],'warning',key)

    def test_overdue_is_not_flagged_while_collection_is_running(self):
        data = self.healthy_schedule()
        data['schedule']['next_run_at'] = '2026-10-15T07:00:00Z'
        self.assertEqual(self.check([],data)['scheduler']['status'],'error')
        data['running'] = True
        self.assertEqual(self.check([],data)['scheduler']['status'],'pass')

    def test_filing_accepted_date_does_not_require_fictional_publication(self):
        checks = self.check([{'source_id':'sec_10k','kind':'filing','url':'https://www.sec.gov/Archives/a',
                              'accepted_at':'2026-10-15T07:00:00Z'}],self.healthy_schedule())
        self.assertEqual(checks['dates']['status'],'pass')
        self.assertEqual(checks['filings']['status'],'pass')
        self.assertIn('未解析完整报表',checks['filings']['detail'])

    def test_recent_old_success_cannot_hide_current_batch_failure(self):
        state = self.healthy_schedule()
        state['last_run_summary'] = {'status':'error'}
        checks = self.check([],state)
        self.assertEqual(checks['sources']['status'],'pass')
        self.assertEqual(checks['last_run']['status'],'error')
        state['last_run_summary'] = {'status':'ok'}
        state['history'] = [{'status':'running'},{'status':'interrupted'}]
        self.assertEqual(self.check([],state)['last_run']['status'],'error')


if __name__=='__main__':
    unittest.main()
