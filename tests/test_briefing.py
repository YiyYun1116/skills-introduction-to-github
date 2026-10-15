import unittest
from app.briefing import build_briefing


class BriefingTests(unittest.TestCase):
    def test_filing_brief_retains_metadata_without_inventing_financials_or_period(self):
        article={'id':'filing','kind':'filing','title':'10-Q - ACME CORP','summary':'Quarterly report accepted.',
                 'url':'https://www.sec.gov/Archives/example-index.htm','company_name':'ACME CORP',
                 'company_cik':'0000000001','filing_form':'10-Q','accession_number':'0000000001-26-000001',
                 'filing_date':'2026-10-12','accepted_at':'2026-10-12T20:30:00Z',
                 'date_basis':'filing_accepted','published_at':None}
        brief=build_briefing({'articles':[article,dict(article,id='copy',duplicate_of='filing')],'independent_sources':1})
        self.assertIsNone(brief['policy'])
        self.assertEqual(len(brief['filing']['items']),1)
        self.assertEqual(brief['filing']['items'][0]['form'],'10-Q')
        self.assertEqual(brief['filing']['items'][0]['company_cik'],'0000000001')
        self.assertEqual(brief['filing']['items'][0]['reporting_period'],'')
        self.assertEqual(brief['filing']['items'][0]['accepted_at'],'2026-10-12T20:30:00Z')
        self.assertEqual(brief['filing']['financial_metrics']['status'],'not_extracted')
        self.assertNotIn('revenue',brief['filing']['financial_metrics'])
        self.assertTrue(any('所属季度' in note for note in brief['filing']['limitations']))

    def test_mixed_policy_and_filing_are_analyzed_in_separate_sections(self):
        articles=[{'id':'policy','kind':'policy','title':'Agency proposes a new rule','summary':'Proposed rule.', 'url':'https://example.org/policy'},
                  {'id':'filing','kind':'filing','title':'ACME quarterly report','summary':'Effective controls and financial report.',
                   'url':'https://example.org/filing','announced_at':'2026-10-12T20:30:00Z','date_basis':'filing_accepted',
                   'reporting_period':'2026-09-30'}]
        brief=build_briefing({'articles':articles,'independent_sources':1})
        self.assertEqual([item['article_id'] for item in brief['policy']['stages']],['policy'])
        self.assertEqual([item['article_id'] for item in brief['filing']['items']],['filing'])
        self.assertEqual(brief['filing']['items'][0]['accepted_at'],'2026-10-12T20:30:00Z')
        self.assertEqual(brief['filing']['items'][0]['reporting_period'],'2026-09-30')

    def test_policy_proposal_is_not_effective_law_and_copies_are_excluded(self):
        article={'id':'a','title':'Agency proposes a new rule','summary':'The agency seeks consultation on the proposal. A final rule has not been issued.',
                 'kind':'policy','url':'https://example.org/a','source_name':'Agency'}
        cluster={'articles':[article,dict(article,id='b',duplicate_of='a')],'independent_sources':1}
        brief=build_briefing(cluster)
        self.assertEqual(len(brief['points']),1)
        self.assertEqual(brief['points'][0]['article_id'],'a')
        self.assertIn('不能视作已生效',brief['policy']['stages'][0]['stage'])
        self.assertEqual(brief['policy']['effective_date'],'未从完整原文确认')

    def test_research_never_invents_methods_or_consensus(self):
        cluster={'articles':[{'id':'a','kind':'research','title':'Research paper','summary':'','url':'https://example.org/a'}],
                 'independent_sources':1}
        brief=build_briefing(cluster)
        self.assertIsNone(brief['policy'])
        self.assertEqual(brief['points'][0]['text'],'Research paper')
        self.assertTrue(any('同行评审' in q for q in brief['questions']))
