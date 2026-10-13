import unittest
from app.briefing import build_briefing


class BriefingTests(unittest.TestCase):
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
