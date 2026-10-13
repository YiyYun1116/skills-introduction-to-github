"""Curated, bounded public feed ingestion. A failed feed never becomes sample news."""

from __future__ import annotations

import concurrent.futures
import hashlib
import html
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser


MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
USER_AGENT = "AtlasFinancialResearch/1.0 (public RSS and Atom reader)"
_ARXIV_REQUEST_LOCK = threading.Lock()
_ARXIV_LAST_REQUEST = 0.0


def _source(source_id, name, url, feed_url, region, description, *, kind="official",
            assets=("macro",), group=None, enabled=True):
    return {
        "id": source_id, "name": name, "url": url, "feed_url": feed_url,
        "kind": kind, "tier": "academic" if kind == "research" else "primary",
        "region": region, "language": "en", "asset_classes": list(assets),
        "independence_group": group or source_id, "description": description,
        "enabled": enabled, "feed_verification": "unverified_network",
        "verification_note": "候选官方订阅地址；初次探测被环境网络策略拒绝，尚未验证实时可用性。",
    }


# Separate feeds from one publisher intentionally share an independence_group.
# Source authority is provenance, not proof that every statement is correct.
SOURCE_CATALOG = [
    _source("fed_press", "美联储 · 新闻公告", "https://www.federalreserve.gov/",
            "https://www.federalreserve.gov/feeds/press_all.xml", "US",
            "美国货币政策、监管和机构公告原文。", group="federal_reserve"),
    _source("fed_speeches", "美联储 · 官员讲话", "https://www.federalreserve.gov/",
            "https://www.federalreserve.gov/feeds/speeches.xml", "US",
            "官员讲话是本人观点；与新闻公告属于同一机构，不能重复计为独立证据。",
            group="federal_reserve"),
    _source("ecb_press", "欧洲央行", "https://www.ecb.europa.eu/",
            "https://www.ecb.europa.eu/rss/press.html", "EU",
            "欧元区货币政策及欧洲央行官方公告。", group="ecb"),
    _source("boe_news", "英格兰银行", "https://www.bankofengland.co.uk/",
            "https://www.bankofengland.co.uk/rss/news", "GLOBAL",
            "英国央行政策与金融稳定公告。", group="bank_of_england"),
    _source("bis_press", "国际清算银行", "https://www.bis.org/",
            "https://www.bis.org/doclist/all_pressrels.rss", "GLOBAL",
            "全球央行合作、银行监管及金融稳定公告。", group="bis"),
    _source("boj_news", "日本银行", "https://www.boj.or.jp/",
            "https://www.boj.or.jp/en/rss/whatsnew.xml", "GLOBAL",
            "日本央行英文公告和政策材料。", group="bank_of_japan"),
    _source("arxiv_qfin", "arXiv · 量化金融", "https://arxiv.org/",
            "https://export.arxiv.org/rss/q-fin", "GLOBAL",
            "量化金融预印本；未经同行评审，作者结论需另行核验。", kind="research",
            assets=("equity", "futures", "options", "crypto", "macro"), group="arxiv"),
    _source("arxiv_econ", "arXiv · 经济学", "https://arxiv.org/",
            "https://export.arxiv.org/rss/econ", "GLOBAL",
            "经济学预印本；收录不代表学术背书或投资结论成立。", kind="research", group="arxiv"),
    _source("sec_press", "美国证券交易委员会", "https://www.sec.gov/",
            "https://www.sec.gov/news/pressreleases.rss", "US",
            "证券监管与执法原始公告；包含上市公司、投资产品和数字资产监管事项。",
            assets=("equity", "options", "crypto"), group="sec"),
]

# These statements record a completed read-only probe, not future uptime promises.
_VERIFIED_SOURCE_IDS = frozenset({
    "fed_press", "fed_speeches", "ecb_press", "boe_news", "bis_press", "boj_news",
    "arxiv_qfin", "arxiv_econ", "sec_press",
})
for _catalog_source in SOURCE_CATALOG:
    if _catalog_source["id"] in _VERIFIED_SOURCE_IDS:
        _catalog_source.update({
            "feed_verification": "verified_format",
            "feed_verified_on": "2026-10-11",
            "verification_note": "已实测官方订阅返回有效 XML；当前可用性及条目数以最近同步结果为准。",
        })
    if _catalog_source["id"].startswith("arxiv_"):
        _category = "q-fin.*" if _catalog_source["id"] == "arxiv_qfin" else "econ.*"
        _catalog_source["history_feed_url"] = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
            "search_query": "cat:" + _category, "start": 0, "max_results": 10,
            "sortBy": "submittedDate", "sortOrder": "descending",
        })
        _catalog_source["verification_note"] = (
            "已验证 RSS 及官方 Atom 历史接口；RSS 无当期更新时读取最近论文，保留真实发表时间。"
        )

_SOURCE_BY_ID = {source["id"]: source for source in SOURCE_CATALOG}
APPROVED_FEED_HOSTS = frozenset(
    {urllib.parse.urlsplit(source["feed_url"]).hostname for source in SOURCE_CATALOG}
    | {"rss.arxiv.org", "arxiv.org"}
)


class SourceFetchError(RuntimeError):
    """A safe public error that does not include proxy URLs or credentials."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style", "iframe", "object", "template"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li", "section", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag in {"script", "style", "iframe", "object", "template"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value, limit=1600):
    """Remove markup and executable element contents; output is still text, not HTML."""
    parser = _PlainText()
    # Decode before parsing so entity-encoded markup cannot become active downstream.
    value = str(value or "")[:MAX_PAYLOAD_BYTES]
    for _ in range(3):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    parser.feed(value)
    parser.close()
    result = " ".join("".join(parser.parts).split())
    result = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", result)
    # Angular brackets never survive into the plain-text API contract.
    return result.replace("<", "").replace(">", "")[:limit]


def _published_at(value):
    if not value or not str(value).strip():
        return None
    value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _element_text(element):
    if element is None:
        return ""
    # Preserve inner markup until the HTML stripper can remove script/style bodies.
    return (element.text or "") + "".join(
        ET.tostring(child, encoding="unicode") for child in element
    )


def _children(element, names):
    return [child for child in element if _local_name(child.tag) in names]


def _child_text(element, *names):
    for name in names:
        children = _children(element, {name})
        if children:
            return _element_text(children[0])
    return ""


def _paper_metadata(item, source):
    authors = []
    for child in _children(item, {"author", "creator"}):
        name = plain_text(_child_text(child, "name") or _element_text(child), 200)
        if name and name not in authors:
            authors.append(name)
    metadata = {"authors": authors[:30]}
    if source["kind"] == "research":
        categories = []
        # Atom primary_category is preferred over secondary categories.
        for element in _children(item, {"primary_category"}) + _children(item, {"category"}):
            category = element.attrib.get("term") or _element_text(element).strip()
            if re.fullmatch(r"[a-z][a-z0-9.-]{0,60}", category, re.I) and category not in categories:
                categories.append(category)
        metadata["paper_category"] = categories[0] if categories else None
        metadata["paper_categories"] = categories[:20]
    return metadata


def _article_dates(item, source, summary, *, is_atom):
    """Keep a paper's first publication distinct from feed announcement/revision dates."""
    if source["id"].startswith("arxiv_"):
        if is_atom:
            return {
                "published_at": _published_at(_child_text(item, "published")),
                "updated_at": _published_at(_child_text(item, "updated")),
                "announced_at": None,
                "announcement_type": None,
                "date_basis": "original_publication",
            }
        # RSS/RDF pubDate is the announcement day, including replacements of old
        # papers. Even a "new" announcement does not establish the first submission date.
        announcement = re.search(
            r"\bAnnounce\s+Type\s*:\s*(replace-cross|replace|cross|new)\b", summary, re.I
        )
        return {
            "published_at": None,
            "updated_at": None,
            "announced_at": _published_at(_child_text(item, "pubdate", "date")),
            "announcement_type": announcement.group(1).lower() if announcement else None,
            "date_basis": "feed_announcement",
        }
    # Official news RSS dates remain publication dates. Atom updated is never substituted.
    return {"published_at": _published_at(_child_text(item, "published", "pubdate", "date"))}


def _safe_article_url(value, source):
    value = str(value or "").strip()
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        return None
    value = urllib.parse.urljoin(source["url"], value)
    try:
        parsed = urllib.parse.urlsplit(value)
        hosts = {urllib.parse.urlsplit(source[key]).hostname for key in ("url", "feed_url")}
        if source["id"].startswith("arxiv_"):
            hosts |= {"arxiv.org", "export.arxiv.org", "rss.arxiv.org"}
        if (parsed.scheme not in {"https", "http"} or parsed.hostname not in hosts
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
        # Older official RSS items sometimes carry HTTP links to the same official host.
        return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return None


_ENTITY_RULES = {
    "Federal Reserve": ("federal reserve", "fomc", "美联储"),
    "European Central Bank": ("european central bank", "ecb", "欧洲央行"),
    "Bank of England": ("bank of england", "英格兰银行"),
    "Bank of Japan": ("bank of japan", "日本银行", "日本央行"),
    "SEC": ("securities and exchange commission", "sec", "美国证券交易委员会"),
    "NVIDIA": ("nvidia", "nvda", "英伟达"),
    "Apple": ("apple inc", "aapl", "苹果公司"),
    "Microsoft": ("microsoft", "msft", "微软"),
    "Tesla": ("tesla", "tsla", "特斯拉"),
    "Bitcoin": ("bitcoin", "btc", "比特币"),
    "Ethereum": ("ethereum", "ether", "eth", "以太坊"),
    "OPEC": ("opec", "欧佩克"),
}
_TAG_RULES = {
    "monetary_policy": ("monetary policy", "interest rate", "fomc", "利率", "货币政策"),
    "inflation": ("inflation", "consumer price", "cpi", "通胀", "物价"),
    "regulation": ("regulation", "regulatory", "enforcement", "监管", "执法"),
    "earnings": ("earnings", "quarterly results", "财报", "盈利"),
    "trade": ("tariff", "tariffs", "trade policy", "关税", "贸易政策"),
    "energy": ("oil", "crude", "natural gas", "原油", "能源"),
    "ai": ("artificial intelligence", "machine learning", "人工智能", "机器学习"),
    "employment": ("employment", "unemployment", "payroll", "就业", "失业"),
    "growth": ("gdp", "economic growth", "经济增长"),
    "credit": ("credit risk", "default", "信用风险", "违约"),
    "digital_assets": ("crypto", "cryptocurrency", "bitcoin", "ethereum", "数字资产", "比特币"),
}
_ASSET_RULES = {
    "equity": ("equity", "equities", "stock", "stocks", "shares", "股票"),
    "futures": ("futures", "commodity", "commodities", "期货", "大宗商品"),
    "options": ("option", "options", "derivatives", "期权", "衍生品"),
    "crypto": ("crypto", "cryptocurrency", "bitcoin", "ethereum", "比特币", "加密货币"),
    "macro": ("inflation", "monetary", "gdp", "interest rate", "通胀", "宏观", "利率"),
}
_REGION_RULES = {
    "US": ("united states", "u.s.", "美国", "federal reserve"),
    "CN": ("china", "chinese", "中国", "中国人民银行"),
    "HK": ("hong kong", "香港"),
    "EU": ("euro area", "european union", "european central bank", "欧盟", "欧元区"),
}


def _matches(text, keywords):
    for keyword in keywords:
        if keyword.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text):
                return True
        elif keyword in text:
            return True
    return False


def classify_article(title, summary, source):
    text = (title + " " + summary).lower()
    tags = sorted(key for key, terms in _TAG_RULES.items() if _matches(text, terms))
    assets = set(key for key, terms in _ASSET_RULES.items() if _matches(text, terms))
    # Feed-wide coverage is not evidence that an individual article concerns every asset.
    if not assets:
        assets.add("macro")
    regions = {source["region"]}
    regions.update(key for key, terms in _REGION_RULES.items() if _matches(text, terms))
    kind = "research" if source["kind"] == "research" else "news"
    if kind != "research" and source["kind"] == "official" and set(tags) & {"monetary_policy", "regulation", "trade"}:
        kind = "policy"
    return {
        "entities": sorted(key for key, terms in _ENTITY_RULES.items() if _matches(text, terms)),
        "tags": tags, "asset_classes": sorted(assets), "regions": sorted(regions), "kind": kind,
    }


def parse_feed(xml, source, limit=25):
    """Parse RSS 2, RDF/RSS 1, and Atom without inventing publication dates or URLs."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    if not isinstance(xml, bytes) or len(xml) > MAX_PAYLOAD_BYTES:
        raise SourceFetchError("订阅内容超过 2 MB 上限或格式无效。")
    # Removing NULs also detects the declarations in UTF-16/UTF-32 XML.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", xml.replace(b"\x00", b""), re.I):
        raise SourceFetchError("拒绝含 DTD 或实体声明的 XML。")
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError):
        raise SourceFetchError("来源未返回有效的 RSS/Atom XML。") from None
    if _local_name(root.tag) not in {"rss", "rdf", "feed"}:
        raise SourceFetchError("来源内容不是受支持的 RSS/Atom 订阅。")
    entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    articles, seen = [], set()
    ingested_at = _utc_now()
    for item in entries:
        title = plain_text(_child_text(item, "title"), 300)
        if not title:
            continue
        url = None
        for link in _children(item, {"link"}):
            if link.attrib.get("rel", "alternate") != "alternate":
                continue
            url = _safe_article_url(link.attrib.get("href") or _element_text(link), source)
            if url:
                break
        if not url:
            identifier = _child_text(item, "guid", "id").strip()
            # An opaque GUID is not a relative article URL; never manufacture a link.
            if identifier.startswith(("https://", "http://")):
                url = _safe_article_url(identifier, source)
        if not url or url in seen:
            continue
        seen.add(url)
        summary = plain_text(_child_text(item, "description", "summary", "encoded", "content"))
        article = {
            "id": hashlib.sha256((source["id"] + "|" + url).encode()).hexdigest()[:24],
            "title": title, "summary": summary, "url": url, "source_id": source["id"],
            "ingested_at": ingested_at,
            "language": source["language"], "is_demo": False,
        }
        article.update(_article_dates(item, source, summary, is_atom=_local_name(root.tag) == "feed"))
        article.update(classify_article(title, summary, source))
        article.update(_paper_metadata(item, source))
        articles.append(article)
        if len(articles) >= limit:
            break
    return articles


def _validate_feed_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in APPROVED_FEED_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or any(ord(char) < 32 for char in url)):
            raise ValueError
    except ValueError:
        raise SourceFetchError("拒绝非 HTTPS 或未批准域名的订阅/重定向地址。") from None


class _ApprovedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_feed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_feed(source):
    _validate_feed_url(source["feed_url"])
    request = urllib.request.Request(source["feed_url"], headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(_ApprovedRedirectHandler())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            _validate_feed_url(response.geturl())
            try:
                length = int(response.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                length = 0
            if length > MAX_PAYLOAD_BYTES:
                raise SourceFetchError("订阅内容超过 2 MB 上限。")
            payload = response.read(MAX_PAYLOAD_BYTES + 1)
            if len(payload) > MAX_PAYLOAD_BYTES:
                raise SourceFetchError("订阅内容超过 2 MB 上限。")
            return payload
    except urllib.error.HTTPError as exc:
        raise SourceFetchError(f"来源返回 HTTP {exc.code}；未生成替代新闻。") from None
    except (socket.timeout, TimeoutError):
        raise SourceFetchError("来源请求超时；未生成替代新闻。") from None
    except urllib.error.URLError:
        raise SourceFetchError("无法连接来源：请检查网络白名单、TLS 或来源可用性。") from None
    except OSError:
        raise SourceFetchError("来源连接失败；请检查网络配置。") from None


class _FetchedArticles(list):
    """A normal JSON-serializable list with metadata for fetch_all's source result."""
    def __init__(self, articles, retrieval):
        super().__init__(articles)
        self.retrieval = retrieval


def _read_arxiv_history(source):
    global _ARXIV_LAST_REQUEST
    # arXiv asks API clients to leave at least three seconds between requests.
    # The lock is shared across research feeds and manual/background invocations.
    with _ARXIV_REQUEST_LOCK:
        remaining = 3.0 - (time.monotonic() - _ARXIV_LAST_REQUEST)
        if remaining > 0:
            time.sleep(remaining)
        _ARXIV_LAST_REQUEST = time.monotonic()
    return _read_feed(dict(source, feed_url=source["history_feed_url"]))


def fetch_source(source_id, limit=25):
    if not isinstance(source_id, str) or source_id not in _SOURCE_BY_ID:
        raise ValueError("Unknown source_id")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    source = _SOURCE_BY_ID[source_id]
    articles = parse_feed(_read_feed(source), source, limit=limit)
    retrieval = {"retrieval_method": "rss", "history_fallback": False}
    if not articles and source.get("history_feed_url"):
        try:
            articles = parse_feed(_read_arxiv_history(source), source, limit=limit)
        except SourceFetchError as exc:
            raise SourceFetchError("RSS 无当期论文；官方历史接口未能读取：" + str(exc)) from None
        retrieval = {"retrieval_method": "atom_history", "history_fallback": True,
                     "detail": "RSS 无当期更新；使用官方 Atom 接口的最近历史论文，非今日新发布。"}
    for article in articles:
        article["retrieval_method"] = retrieval["retrieval_method"]
    return _FetchedArticles(articles, retrieval)


def fetch_all(source_ids=None):
    if source_ids is None:
        source_ids = [source["id"] for source in SOURCE_CATALOG if source["enabled"]]
    if not isinstance(source_ids, (list, tuple)) or any(
        not isinstance(source_id, str) or source_id not in _SOURCE_BY_ID for source_id in source_ids
    ):
        raise ValueError("Unknown source_id or invalid source_ids")
    source_ids = list(dict.fromkeys(source_ids))

    def run(source_id):
        try:
            articles = fetch_source(source_id)
            return articles, {"source_id": source_id, "status": "ok" if articles else "empty",
                              "article_count": len(articles), "error": None, "checked_at": _utc_now(),
                              **getattr(articles, "retrieval", {})}
        except SourceFetchError as exc:
            return [], {"source_id": source_id, "status": "error", "article_count": 0,
                        "error": str(exc), "checked_at": _utc_now()}
        except Exception:
            # No remote exception text (including proxy credentials) enters the public API.
            return [], {"source_id": source_id, "status": "error", "article_count": 0,
                        "error": "订阅处理失败；未生成替代新闻。", "checked_at": _utc_now()}

    articles, results = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for batch, result in executor.map(run, source_ids):
            articles.extend(batch)
            results.append(result)
    return {"articles": articles, "results": results}
