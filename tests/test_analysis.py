import copy
import unittest

from app.analysis import analyze_articles, canonicalize_url, market_summary, normalize_title


SOURCES = [
    {"id": "fed", "name": "Federal Reserve", "url": "https://example.org/", "tier": "primary", "kind": "official", "independence_group": "fed"},
    {"id": "paper", "name": "Research", "url": "https://example.org/", "tier": "academic", "kind": "research", "independence_group": "university"},
    {"id": "wire", "name": "Wire", "url": "https://example.org/", "tier": "secondary", "kind": "news", "independence_group": "wire-group"},
    {"id": "wire-sibling", "name": "Wire sibling", "url": "https://example.org/", "tier": "secondary", "kind": "news", "independence_group": "wire-group"},
]


def article(identifier, **kwargs):
    return {"id": identifier, "title": "Federal Reserve holds interest rate at 4.5%", "summary": "The policy decision follows a review of inflation and employment.",
            "url": f"https://example.org/story/{identifier}", "source_id": "fed", "published_at": "2026-01-21T12:00:00Z", "ingested_at": "2026-01-21T13:00:00Z",
            "language": "en", "asset_classes": ["macro", "equity"], "regions": ["US"], "entities": ["Federal Reserve"], "tags": ["interest rates"],
            "topic_key": "fed-rate", "kind": "policy", "is_demo": False, **kwargs}


def filing(identifier, **kwargs):
    values = dict(kind="filing", title="10-Q - ACME CORP", summary="Quarterly report filing index.",
                  source_id="fed", topic_key="", entities=["ACME CORP"], tags=["company_filings"],
                  asset_classes=["equity"], published_at=None, announced_at="2026-10-12T20:30:00Z",
                  accepted_at="2026-10-12T20:30:00Z", date_basis="filing_accepted",
                  filing_form="10-Q", company_name="ACME CORP", company_cik="0000000001",
                  accession_number="0000000001-26-000001", filing_date="2026-10-12")
    values.update(kwargs)
    return article(identifier, **values)


class AnalysisTests(unittest.TestCase):
    def test_publisher_alternative_hosts_must_be_explicit(self):
        source = dict(SOURCES[0],id='bbc',url='https://www.bbc.co.uk/news/business',
                      article_hosts=['www.bbc.co.uk','www.bbc.com'])
        for host,expected in [('www.bbc.com',True),('www.bbc.co.uk',True),('bbc.com.evil.example',False),('evil.bbc.com',False)]:
            row = article('report',source_id='bbc',url='https://'+host+'/news/report')
            result = analyze_articles([row],[source])[0]['articles'][0]
            self.assertEqual(result['source_domain_match'],expected,host)

    def test_filing_acceptance_retains_correct_time_basis(self):
        cluster=analyze_articles([filing('filing')],SOURCES)[0]
        self.assertEqual(cluster['latest_at'],'2026-10-12T20:30:00Z')
        self.assertEqual(cluster['latest_date_basis'],'filing_accepted')
        self.assertEqual(cluster['articles'][0]['activity_date_basis'],'filing_accepted')
        self.assertEqual(cluster['articles'][0]['published_at'],'')
        self.assertFalse(any('论文订阅' in note for note in cluster['verification']['limitations']))
        self.assertFalse(any('无法确认信息时效' in note for note in cluster['verification']['limitations']))
        self.assertTrue(any('报告期末' in note for note in cluster['verification']['limitations']))
        self.assertIn('索引',cluster['scenario']['base'])
        self.assertIn('暂不判断',cluster['scenario']['base'])

    def test_filing_announced_fallback_preserves_acceptance_basis(self):
        cluster=analyze_articles([filing('filing',accepted_at=None)],SOURCES)[0]
        self.assertEqual(cluster['latest_date_basis'],'filing_accepted')
        self.assertEqual(cluster['articles'][0]['accepted_at'],'')
        self.assertEqual(cluster['latest_at'],'2026-10-12T20:30:00Z')

    def test_distinct_filing_accessions_stay_separate_even_with_shared_topic_key(self):
        rows=[filing('q1',topic_key='acme-earnings'),
              filing('q2',topic_key='acme-earnings',accession_number='0000000001-26-000002')]
        clusters=analyze_articles(rows,SOURCES)
        self.assertEqual(len(clusters),2)
        self.assertEqual(len({item['id'] for item in clusters}),2)
        self.assertTrue(all(item['duplicate_count']==0 for item in clusters))

    def test_company_and_form_metadata_prevent_false_filing_identity(self):
        variants=[dict(company_cik='0000000002',company_name='ACRE CORP'),dict(filing_form='10-Q/A'),
                  dict(reporting_period='2026-06-30')]
        for variant in variants:
            with self.subTest(variant=variant):
                rows=[filing('one',reporting_period='2026-03-31'),filing('two',**variant)]
                clusters=analyze_articles(rows,SOURCES)
                self.assertEqual(len(clusters),2)

    def test_same_accession_index_copies_deduplicate_despite_different_headlines(self):
        rows=[filing('one'),filing('copy',title='ACME CORP Form 10-Q filed October 12, 2026',
                                   company_cik='1',accession_number='000000000126000001')]
        cluster=analyze_articles(rows,SOURCES)[0]
        self.assertEqual(cluster['unique_count'],1)
        self.assertEqual(cluster['duplicate_count'],1)
        self.assertEqual(cluster['independent_sources'],1)

    def test_filing_without_accession_uses_document_url_not_generic_title(self):
        clusters=analyze_articles([filing('a',accession_number=None),filing('b',accession_number=None)],SOURCES)
        self.assertEqual(len(clusters),2)
        self.assertTrue(all(item['unique_count']==1 for item in clusters))

    def test_generic_news_headline_cannot_collapse_two_filings(self):
        rows=[article('news',title='10-Q - ACME CORP',kind='news',published_at='2026-10-12T20:30:00Z',
                      entities=['ACME CORP'],topic_key='acme-earnings'),
              filing('one',topic_key='acme-earnings'),
              filing('two',topic_key='acme-earnings',accession_number='0000000001-26-000002')]
        clusters=analyze_articles(rows,SOURCES)
        self.assertEqual(sum(item['unique_count'] for item in clusters),3)
        self.assertFalse(any(sum(article['kind']=='filing' for article in item['articles'])>1 for item in clusters))

    def test_paper_announcement_is_not_original_publication(self):
        row=article('paper',source_id='paper',kind='research',published_at=None,
                    announced_at='2026-10-12T04:00:00Z',date_basis='feed_announcement')
        cluster=analyze_articles([row],SOURCES)[0]
        self.assertEqual(cluster['latest_at'],'2026-10-12T04:00:00Z')
        self.assertEqual(cluster['latest_date_basis'],'feed_announcement')
        self.assertEqual(cluster['articles'][0]['published_at'],'')
        self.assertTrue(any('不等于原始发表' in note for note in cluster['verification']['limitations']))

    def test_url_canonicalization_preserves_document_identity(self):
        self.assertEqual(canonicalize_url("https://EXAMPLE.org/story?id=8&utm_source=email&fbclid=foo#top"), "https://example.org/story?id=8")
        self.assertNotEqual(canonicalize_url("https://example.org/story?id=8"), canonicalize_url("https://example.org/story?id=9"))
        self.assertEqual(canonicalize_url("javascript:alert(1)"), "")
        self.assertEqual(canonicalize_url("https://secret@example.org/story"), "")

    def test_copies_do_not_corroborate_or_weight_sentiment(self):
        rows = [article("original"), article("copy", source_id="wire"), article("copy-2", source_id="paper")]
        cluster = analyze_articles(rows, SOURCES)[0]
        self.assertEqual(cluster["article_count"], 3)
        self.assertEqual(cluster["unique_count"], 1)
        self.assertEqual(cluster["duplicate_count"], 2)
        self.assertEqual(cluster["independent_sources"], 1)
        self.assertEqual(cluster["verification"]["status"], "traceable_single")
        self.assertIn("待独立核验", cluster["verification"]["label"])
        self.assertEqual({row["duplicate_of"] for row in cluster["articles"] if row["duplicate_of"]}, {"original"})
        self.assertEqual(sum(cluster["sentiment"][key] for key in ("positive", "negative", "neutral")), 1)

    def test_distinct_independent_sources_are_counted_once_per_owner(self):
        rows = [article("official"), article("news", source_id="wire", title="Markets review Federal Reserve policy and future growth"),
                article("news-sibling", source_id="wire-sibling", title="Policy watchers focus on employment after the rate decision")]
        cluster = analyze_articles(rows, SOURCES)[0]
        self.assertEqual(cluster["unique_count"], 3)
        self.assertEqual(cluster["independent_sources"], 2)
        self.assertEqual(cluster["verification"]["status"], "traceable_multiple")
        self.assertEqual(cluster["verification"]["score_type"], "provenance")
        self.assertTrue(all(item["status"] == "reported" for item in cluster["evidence"]))

    def test_numeric_differences_are_not_duplicates(self):
        cluster = analyze_articles([article("a"), article("b", title="Federal Reserve holds interest rate at 4.0%", source_id="wire")], SOURCES)[0]
        self.assertEqual(cluster["unique_count"], 2)
        self.assertEqual(cluster["duplicate_count"], 0)
        self.assertTrue(cluster["conflicts"])
        self.assertTrue(all(item["status"] == "needs_review" for item in cluster["evidence"]))
        self.assertNotEqual(normalize_title("同比增长 1.5%"), normalize_title("同比增长 15%"))

    def test_country_differences_are_not_duplicates(self):
        cluster = analyze_articles([article("a", title="中国通胀同比增长 2%"), article("b", title="美国通胀同比增长 2%")], SOURCES)[0]
        self.assertEqual(cluster["unique_count"], 2)

    def test_similarly_named_companies_remain_distinct(self):
        rows = [article("a", title="Acme reports quarterly revenue growth and improved outlook", entities=["Acme"], topic_key=""),
                article("b", title="Acre reports quarterly revenue growth and improved outlook", entities=["Acre"], topic_key="")]
        clusters = analyze_articles(rows, SOURCES)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(sum(cluster["duplicate_count"] for cluster in clusters), 0)

    def test_conflict_details_are_bounded_but_every_impacted_claim_is_flagged(self):
        rows = [article(f"variant-{index}", title=f"Federal Reserve holds interest rate at {index}%") for index in range(60)]
        cluster = analyze_articles(rows, SOURCES)[0]
        self.assertEqual(cluster["conflict_count"], 1770)
        self.assertEqual(len(cluster["conflicts"]), 50)
        self.assertTrue(all(claim["status"] == "needs_review" for claim in cluster["evidence"]))
        self.assertTrue(any("1770" in note for note in cluster["verification"]["limitations"]))

    def test_missing_and_invalid_dates_are_honest(self):
        cluster = analyze_articles([article("a", published_at=None, ingested_at="bad")], SOURCES)[0]
        self.assertEqual(cluster["latest_at"], "")
        self.assertEqual(cluster["articles"][0]["published_at"], "")
        self.assertTrue(any("发布时间" in note for note in cluster["verification"]["limitations"]))

    def test_demo_and_live_are_never_grouped_or_deduplicated(self):
        clusters = analyze_articles([article("same-id"), article("same-id", is_demo=True), article("demo-copy", is_demo=True)], SOURCES)
        self.assertEqual(len(clusters), 2)
        self.assertNotEqual(clusters[0]["id"], clusters[1]["id"])
        self.assertEqual({cluster["is_demo"] for cluster in clusters}, {True, False})
        self.assertTrue(all(cluster["unique_count"] == 1 for cluster in clusters))
        self.assertEqual(next(cluster for cluster in clusters if not cluster["is_demo"])["article_count"], 1)
        self.assertTrue(all(row["is_demo"] == cluster["is_demo"] for cluster in clusters for row in cluster["articles"]))

    def test_deterministic_and_input_immutable(self):
        rows = [article("a"), article("b", source_id="wire"), article("c", title="Rate policy influences employment outlook")]
        saved = copy.deepcopy(rows)
        self.assertEqual(analyze_articles(rows, SOURCES), analyze_articles(list(reversed(rows)), list(reversed(SOURCES))))
        self.assertEqual(rows, saved)

    def test_topic_overlap_requires_more_than_shared_entity(self):
        rows = [article("a", title="Acme announces electric vehicle factory", summary="New manufacturing capacity", entities=["Acme"], topic_key="", tags=["factory"]),
                article("b", title="Acme CEO faces unrelated privacy litigation", summary="Court hearing over user privacy", entities=["Acme"], topic_key="", tags=["litigation"])]
        self.assertEqual(len(analyze_articles(rows, SOURCES)), 2)

    def test_related_reports_group_without_fixture_key(self):
        rows = [article("a", topic_key=""), article("b", source_id="wire", title="Federal Reserve interest rate decision leaves policy unchanged", topic_key="")]
        clusters = analyze_articles(rows, SOURCES)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["unique_count"], 2)

    def test_repeated_old_headline_is_a_separate_observation(self):
        clusters = analyze_articles([article("a", published_at="2025-01-01T00:00:00Z"), article("b")], SOURCES)
        self.assertEqual(clusters[0]["unique_count"], 2)

    def test_unknown_or_unlinked_sources_do_not_count_as_independent(self):
        cluster = analyze_articles([article("a", source_id="made-up", url="")], SOURCES)[0]
        self.assertEqual(cluster["independent_sources"], 0)
        self.assertEqual(cluster["verification"]["status"], "untraceable")
        self.assertEqual(cluster["verification"]["score"], 0)

    def test_forged_official_source_id_does_not_inherit_provenance(self):
        cluster = analyze_articles([article("spoof", url="https://example.org.attacker.invalid/story")], SOURCES)[0]
        self.assertFalse(cluster["articles"][0]["source_domain_match"])
        self.assertEqual(cluster["independent_sources"], 0)
        self.assertEqual(cluster["verification"]["score"], 0)
        self.assertEqual(cluster["verification"]["status"], "untraceable")

    def test_empty_and_summary(self):
        self.assertEqual(analyze_articles([], SOURCES), [])
        self.assertEqual(market_summary([])["topic_count"], 0)
        clusters = analyze_articles([article("a"), article("b", source_id="wire")], SOURCES)
        summary = market_summary(clusters)
        self.assertEqual(summary["article_count"], 2)
        self.assertEqual(summary["unique_count"], 1)
        self.assertEqual(summary["duplicate_rate"], 50)
        self.assertIn("不构成已验证的预测", clusters[0]["scenario"]["limitations"][0])

    def test_research_scenario_does_not_infer_market_move_from_broad_asset_tags(self):
        cluster = analyze_articles([article("research", source_id="paper", kind="research", title="A model of financial risk", summary="A quantitative model of tail losses.",
                                           asset_classes=["macro", "equity", "crypto", "futures", "options"])], SOURCES)[0]
        self.assertIn("样本外检验", cluster["scenario"]["bull"])
        self.assertIn("复现失败", cluster["scenario"]["bear"])
        self.assertNotIn("资金", cluster["scenario"]["bull"])
        self.assertNotIn("商品", cluster["scenario"]["base"])


if __name__ == "__main__":
    unittest.main()
