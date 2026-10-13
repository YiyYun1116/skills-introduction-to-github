import unittest
from unittest.mock import patch

from app.sources import (
    SOURCE_CATALOG, SourceFetchError, _ApprovedRedirectHandler, _validate_feed_url,
    fetch_all, fetch_source, parse_feed, plain_text,
)


FED = next(source for source in SOURCE_CATALOG if source["id"] == "fed_press")
ARXIV = next(source for source in SOURCE_CATALOG if source["id"] == "arxiv_qfin")


class FeedParsingTests(unittest.TestCase):
    def test_rss_dates_and_plain_text(self):
        xml = """<rss><channel><item>
        <title>Federal Reserve interest rate statement</title>
        <link>https://www.federalreserve.gov/newsevents/example.htm</link>
        <description><![CDATA[<p>Inflation &amp; employment.</p><script>alert(1)</script>]]></description>
        <pubDate>Wed, 09 Sep 2026 12:00:00 -0400</pubDate>
        </item></channel></rss>"""
        articles = parse_feed(xml, FED)
        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["published_at"], "2026-09-09T16:00:00Z")
        self.assertEqual(article["summary"], "Inflation & employment.")
        self.assertEqual(article["kind"], "policy")
        self.assertIn("Federal Reserve", article["entities"])
        self.assertIn("inflation", article["tags"])
        self.assertFalse(article["is_demo"])

    def test_atom_alternate_url_and_edit_time_not_publication(self):
        xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>https://arxiv.org/abs/2609.01234</id><title>Options and Bitcoin risk</title>
        <link rel="self" href="https://export.arxiv.org/api/2609.01234"/>
        <link rel="alternate" href="http://arxiv.org/abs/2609.01234"/>
        <summary>Research on derivatives.</summary><updated>2026-09-09T12:00:00Z</updated>
        </entry></feed>"""
        article = parse_feed(xml, ARXIV)[0]
        self.assertEqual(article["url"], "https://arxiv.org/abs/2609.01234")
        self.assertIsNone(article["published_at"])
        self.assertEqual(article["kind"], "research")
        self.assertEqual(article["asset_classes"], ["crypto", "options"])

    def test_atom_authors_primary_category_and_publication_date(self):
        xml = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
        <id>https://arxiv.org/abs/2609.01234</id><title>Portfolio optimization</title>
        <author><name>Ada Researcher</name></author><author><name>Grace Scholar</name></author>
        <category term="math.OC"/><arxiv:primary_category term="q-fin.PM"/>
        <published>2026-09-08T12:00:00Z</published><updated>2026-09-09T12:00:00Z</updated>
        </entry></feed>"""
        article = parse_feed(xml, ARXIV)[0]
        self.assertEqual(article["authors"], ["Ada Researcher", "Grace Scholar"])
        self.assertEqual(article["paper_category"], "q-fin.PM")
        self.assertEqual(article["paper_categories"], ["q-fin.PM", "math.OC"])
        self.assertEqual(article["published_at"], "2026-09-08T12:00:00Z")
        self.assertEqual(article["updated_at"], "2026-09-09T12:00:00Z")
        self.assertEqual(article["date_basis"], "original_publication")
        self.assertIsNone(article["announced_at"])

    def test_rdf_and_dc_date(self):
        xml = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
        <item><title>Market research</title><link>https://arxiv.org/abs/2609.11111</link>
        <dc:date>2026-09-09T09:00:00+02:00</dc:date></item></rdf:RDF>"""
        article = parse_feed(xml, ARXIV)[0]
        self.assertIsNone(article["published_at"])
        self.assertEqual(article["announced_at"], "2026-09-09T07:00:00Z")
        self.assertEqual(article["date_basis"], "feed_announcement")

    def test_old_paper_replacement_announcement_is_not_original_publication(self):
        xml = """<rss><channel><item><title>A revised finance paper</title>
        <link>https://arxiv.org/abs/2509.00212v2</link>
        <description><![CDATA[<p>arXiv:2509.00212v2 Announce Type: replace</p>
        <p>Abstract: Revised results for an older paper.</p>]]></description>
        <pubDate>Mon, 12 Oct 2026 00:00:00 -0400</pubDate></item></channel></rss>"""
        article = parse_feed(xml, ARXIV)[0]
        self.assertIsNone(article["published_at"])
        self.assertIsNone(article["updated_at"])
        self.assertEqual(article["announced_at"], "2026-10-12T04:00:00Z")
        self.assertEqual(article["announcement_type"], "replace")
        self.assertEqual(article["date_basis"], "feed_announcement")

    def test_new_and_cross_announcements_also_do_not_invent_publication_dates(self):
        for announce_type in ("new", "cross", "replace-cross"):
            with self.subTest(announce_type=announce_type):
                xml = f"""<rss><channel><item><title>A finance paper</title>
                <link>https://arxiv.org/abs/2610.12345v1</link>
                <description>Announce Type: {announce_type} Abstract: Research.</description>
                <pubDate>Mon, 12 Oct 2026 04:00:00 GMT</pubDate></item></channel></rss>"""
                article = parse_feed(xml, ARXIV)[0]
                self.assertIsNone(article["published_at"])
                self.assertEqual(article["announcement_type"], announce_type)
                self.assertEqual(article["announced_at"], "2026-10-12T04:00:00Z")

    def test_bad_missing_and_ambiguous_dates_stay_null(self):
        for date in ("", "not a date", "2026-99-09", "2026-09-09T10:00:00"):
            with self.subTest(date=date):
                xml = f"""<rss><channel><item><title>News</title>
                <link>https://www.federalreserve.gov/news</link><pubDate>{date}</pubDate>
                </item></channel></rss>"""
                self.assertIsNone(parse_feed(xml, FED)[0]["published_at"])

    def test_rejects_dtd_and_entities_in_multiple_encodings(self):
        xml = '<!DOCTYPE rss [<!ENTITY bad "boom">]><rss><channel/></rss>'
        for payload in (xml, xml.encode("utf-16"), xml.encode("utf-32")):
            with self.assertRaises(SourceFetchError):
                parse_feed(payload, FED)

    def test_no_xss_snippets(self):
        self.assertEqual(plain_text('<img src=x onerror="alert(1)">Safe<script>bad()</script>'), "Safe")
        self.assertEqual(plain_text('&lt;script&gt;bad()&lt;/script&gt;Good'), "Good")
        self.assertNotIn("<", plain_text('<a href="javascript:alert(1)">Read</a>'))
        self.assertEqual(plain_text('<html:script>bad()</html:script>Good'), "Good")

    def test_unsafe_and_unrelated_links_not_attributed_to_official_source(self):
        for url in ("javascript:alert(1)", "https://evil.example/fake", "https://127.0.0.1/internal"):
            xml = f"<rss><channel><item><title>Fake</title><link>{url}</link></item></channel></rss>"
            self.assertEqual(parse_feed(xml, FED), [])

    def test_duplicate_urls_and_missing_titles_are_skipped(self):
        item = "<item><title>News</title><link>https://www.federalreserve.gov/news</link></item>"
        xml = f"<rss><channel>{item}{item}<item><title>No link</title></item></channel></rss>"
        self.assertEqual(len(parse_feed(xml, FED)), 1)

    def test_opaque_guid_does_not_create_a_fake_article_url(self):
        xml = "<rss><channel><item><title>News</title><guid>uuid-123</guid></item></channel></rss>"
        self.assertEqual(parse_feed(xml, FED), [])

    def test_invalid_xml_and_payload_limit(self):
        for xml in ("<html><body>Denied</body></html>", "broken", b"x" * (2 * 1024 * 1024 + 1)):
            with self.assertRaises(SourceFetchError):
                parse_feed(xml, FED)


class FeedFetchingTests(unittest.TestCase):
    def test_rejects_unsafe_feed_urls_and_redirects(self):
        for url in ("http://www.federalreserve.gov/feed", "https://localhost/", "https://www.federalreserve.gov:444/feed",
                    "https://user:password@www.federalreserve.gov/feed", "https://example.com/feed"):
            with self.assertRaises(SourceFetchError):
                _validate_feed_url(url)
            with self.assertRaises(SourceFetchError):
                _ApprovedRedirectHandler().redirect_request(None, None, 302, "", {}, url)
        _validate_feed_url("https://rss.arxiv.org/rss/q-fin")

    def test_invalid_source_is_error_before_network(self):
        with self.assertRaises(ValueError):
            fetch_source("invented")
        with self.assertRaises(ValueError):
            fetch_all(["invented"])

    def test_failure_is_visible_and_has_no_fallback_articles(self):
        with patch("app.sources.fetch_source", side_effect=SourceFetchError("网络不可达")):
            result = fetch_all(["fed_press"])
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertEqual(result["results"][0]["article_count"], 0)
        self.assertEqual(result["results"][0]["error"], "网络不可达")

    def test_unexpected_errors_are_redacted(self):
        with patch("app.sources.fetch_source", side_effect=RuntimeError("secret-proxy-token")):
            result = fetch_all(["fed_press"])
        self.assertNotIn("secret-proxy-token", str(result))

    def test_empty_research_rss_uses_visible_official_history(self):
        atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Historical paper</title><id>https://arxiv.org/abs/2609.01234</id>
        <published>2026-09-08T12:00:00Z</published></entry></feed>"""
        with patch("app.sources._read_feed", return_value=b"<rss><channel/></rss>"), \
                patch("app.sources._read_arxiv_history", return_value=atom):
            result = fetch_all(["arxiv_qfin"])
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertTrue(result["results"][0]["history_fallback"])
        self.assertEqual(result["articles"][0]["retrieval_method"], "atom_history")
        self.assertEqual(result["articles"][0]["published_at"], "2026-09-08T12:00:00Z")

    def test_failed_rss_does_not_silently_switch_to_history(self):
        with patch("app.sources._read_feed", side_effect=SourceFetchError("RSS unavailable")), \
                patch("app.sources._read_arxiv_history") as history:
            result = fetch_all(["arxiv_qfin"])
        history.assert_not_called()
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertEqual(result["articles"], [])

    def test_failed_history_identifies_both_empty_rss_and_failed_api(self):
        with patch("app.sources._read_feed", return_value=b"<rss><channel/></rss>"), \
                patch("app.sources._read_arxiv_history", side_effect=SourceFetchError("API unavailable")):
            result = fetch_all(["arxiv_qfin"])
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertIn("RSS 无当期论文", result["results"][0]["error"])
        self.assertIn("API unavailable", result["results"][0]["error"])

    def test_correlated_feeds_share_independence_group(self):
        by_id = {source["id"]: source for source in SOURCE_CATALOG}
        self.assertEqual(by_id["fed_press"]["independence_group"], by_id["fed_speeches"]["independence_group"])
        self.assertEqual(by_id["arxiv_qfin"]["independence_group"], by_id["arxiv_econ"]["independence_group"])


if __name__ == "__main__":
    unittest.main()
