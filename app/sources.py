"""Curated, bounded public feed ingestion. A failed feed never becomes sample news."""

from __future__ import annotations

import concurrent.futures
import hashlib
import html
import http.client
import re
import socket
import ssl
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
MAX_FETCH_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 5
USER_AGENT = "AtlasFinancialResearch/1.0 (public RSS and Atom reader)"
_ARXIV_REQUEST_LOCK = threading.Lock()
_ARXIV_LAST_REQUEST = 0.0


def _source(source_id, name, url, feed_url, region, description, *, kind="official",
            assets=("macro",), group=None, enabled=True):
    return {
        "id": source_id, "name": name, "url": url, "feed_url": feed_url,
        "kind": kind, "tier": {"research": "academic", "news": "secondary"}.get(kind, "primary"),
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
    _source("sec_10k", "SEC EDGAR · 公司年报", "https://www.sec.gov/",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-K&owner=include&count=100&output=atom", "US",
            "美国 EDGAR 最新 10-K、10-KT 过渡年报及修订申报索引，默认每轮最多读取 100 条，不是完整历史或全球公司覆盖；申报内容由公司负责，SEC 收录不代表担保。",
            assets=("equity",), group="sec"),
    _source("sec_10q", "SEC EDGAR · 公司季报", "https://www.sec.gov/",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-Q&owner=include&count=100&output=atom", "US",
            "美国 EDGAR 最新 10-Q、10-QT 过渡季报及修订申报索引，默认每轮最多读取 100 条；接收时间不等于财报覆盖期间，未自动提取报表数值或回补完整历史。",
            assets=("equity",), group="sec"),
    _source("bbc_business", "BBC · 全球商业新闻", "https://www.bbc.co.uk/news/business",
            "https://feeds.bbci.co.uk/news/business/rss.xml", "GLOBAL",
            "BBC 英文商业新闻标题与摘要，属媒体二手报道；覆盖由公开订阅决定，需结合原始公告核验。",
            kind="news", assets=("macro", "equity"), group="bbc"),
    _source("guardian_business", "The Guardian · 全球商业新闻", "https://www.theguardian.com/business",
            "https://www.theguardian.com/business/rss", "GLOBAL",
            "Guardian 英文商业新闻标题与摘要，属媒体二手报道；区域版订阅可能偏重英国、美国及国际经济。",
            kind="news", assets=("macro", "equity"), group="guardian"),
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
    if _catalog_source["id"] in {"sec_10k", "sec_10q"}:
        _catalog_source.update({
            "content_kind": "filing", "filing_form": "10-K" if _catalog_source["id"] == "sec_10k" else "10-Q",
            "fetch_limit": 100, "coverage_scope": "recent_feed_only",
            "feed_verification": "verified_format", "feed_verified_on": "2026-10-13",
            "verification_note": "已实测 SEC 官方 Atom 申报索引；仅覆盖最近条目，非完整财报历史。",
        })
    if _catalog_source["id"] in {"bbc_business", "guardian_business"}:
        _catalog_source.update({
            "fetch_limit": 100, "coverage_scope": "recent_feed_only",
            "feed_verification": "verified_format", "feed_verified_on": "2026-10-15",
            "verification_note": "已实测媒体公开 RSS；可读取不代表每项报道已经独立核实。",
            "article_hosts": (["www.bbc.co.uk", "bbc.co.uk", "www.bbc.com", "bbc.com"]
                              if _catalog_source["id"] == "bbc_business" else ["www.theguardian.com"]),
        })

_SOURCE_BY_ID = {source["id"]: source for source in SOURCE_CATALOG}
APPROVED_FEED_HOSTS = frozenset(
    {urllib.parse.urlsplit(source["feed_url"]).hostname for source in SOURCE_CATALOG}
    | {"rss.arxiv.org", "arxiv.org"}
)


class SourceFetchError(RuntimeError):
    """A safe public error that does not include proxy URLs or credentials."""

    def __init__(self, message, *, error_type="parse_error", attempts=1, retryable=False,
                 http_status=None, retry_after_seconds=None, retry_errors=None):
        super().__init__(message)
        self.error_type = error_type
        self.attempts = attempts
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.retry_errors = list(retry_errors or [])


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
    if source.get("content_kind") == "filing":
        accepted_at = _published_at(_child_text(item, "updated"))
        return {"published_at": None, "accepted_at": accepted_at, "announced_at": accepted_at,
                "date_basis": "filing_accepted"}
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


def _filing_metadata(item, title, summary, source):
    if source.get("content_kind") != "filing":
        return {}
    forms = [node.attrib.get("term", "") for node in _children(item, {"category"})]
    form = next((form for form in forms if re.fullmatch(r"10-[KQ]T?(?:/A)?", form)), None)
    # A query filter is not evidence that a specific returned entry is the requested form.
    if not form:
        return {}
    company_match = re.match(r"^10-[KQ]T?(?:/A)?\s+-\s+(.+?)\s+\((\d{10})\)(?:\s|$)", title)
    accession = re.search(r"\b\d{10}-\d{2}-\d{6}\b", summary)
    filing_day = re.search(r"\bFiled:\s*(\d{4}-\d{2}-\d{2})\b", summary)
    metadata = {"kind": "filing", "filing_form": form, "asset_classes": ["equity"],
                "tags": ["company_filings"], "company_name": None, "company_cik": None,
                "accession_number": accession.group(0) if accession else None, "filing_date": None}
    if company_match:
        metadata.update({"company_name": company_match.group(1), "company_cik": company_match.group(2),
                         "entities": [company_match.group(1)]})
    if filing_day:
        try:
            metadata["filing_date"] = datetime.strptime(filing_day.group(1), "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return metadata


def _safe_article_url(value, source):
    value = str(value or "").strip()
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        return None
    value = urllib.parse.urljoin(source["url"], value)
    try:
        parsed = urllib.parse.urlsplit(value)
        hosts = {urllib.parse.urlsplit(source[key]).hostname for key in ("url", "feed_url")}
        hosts.update(source.get("article_hosts", []))
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
        article.update(_filing_metadata(item, title, summary, source))
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
        raise SourceFetchError("拒绝非 HTTPS 或未批准域名的订阅/重定向地址。", error_type="blocked_url") from None


class _ApprovedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_feed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _retry_after_seconds(headers):
    value = str(headers.get("Retry-After", "")).strip()[:128] if headers else ""
    if re.fullmatch(r"\d{1,10}", value):
        return int(value)
    timestamp = _published_at(value)
    if timestamp:
        return max(0, (datetime.fromisoformat(timestamp.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
    return None


def _read_feed_once(source):
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
                raise SourceFetchError("订阅内容超过 2 MB 上限。", error_type="payload_too_large")
            payload = response.read(MAX_PAYLOAD_BYTES + 1)
            if len(payload) > MAX_PAYLOAD_BYTES:
                raise SourceFetchError("订阅内容超过 2 MB 上限。", error_type="payload_too_large")
            return payload
    except urllib.error.HTTPError as exc:
        error_type = {403: "access_denied", 404: "not_found", 408: "timeout", 429: "rate_limited"}.get(
            exc.code, "server_error" if 500 <= exc.code <= 599 else "http_error"
        )
        raise SourceFetchError(f"来源返回 HTTP {exc.code}；未生成替代新闻。", error_type=error_type,
                               retryable=exc.code in {408, 429} or 500 <= exc.code <= 599,
                               http_status=exc.code, retry_after_seconds=_retry_after_seconds(exc.headers)) from None
    except (socket.timeout, TimeoutError):
        raise SourceFetchError("来源请求超时；未生成替代新闻。", error_type="timeout", retryable=True) from None
    except ssl.SSLError:
        raise SourceFetchError("TLS 校验或握手失败；保留安全校验，请检查来源证书。", error_type="tls_error") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        # Reason strings are inspected locally only; none is exposed in API output.
        if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            raise SourceFetchError("TLS 校验失败；保留证书校验。", error_type="tls_error") from None
        if re.search(r"Tunnel connection failed:\s*(403|404)\b", str(reason)):
            raise SourceFetchError("网络代理拒绝连接；请检查环境域名白名单。", error_type="proxy_access_denied") from None
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise SourceFetchError("来源请求超时；未生成替代新闻。", error_type="timeout", retryable=True) from None
        raise SourceFetchError("暂时无法连接来源；请检查网络或来源可用性。", error_type="network_error", retryable=True) from None
    except http.client.IncompleteRead:
        raise SourceFetchError("来源响应中断，未收到完整订阅。", error_type="incomplete_response", retryable=True) from None
    except PermissionError:
        raise SourceFetchError("环境拒绝访问来源。", error_type="access_denied") from None
    except OSError:
        raise SourceFetchError("来源连接失败；请检查网络配置。", error_type="network_error", retryable=True) from None


class _FeedPayload(bytes):
    def __new__(cls, payload, attempts, retry_errors):
        value = super().__new__(cls, payload)
        value.attempts = attempts
        value.retry_errors = list(retry_errors)
        return value


def _wait_for_arxiv_api_slot():
    global _ARXIV_LAST_REQUEST
    with _ARXIV_REQUEST_LOCK:
        remaining = 3.0 - (time.monotonic() - _ARXIV_LAST_REQUEST)
        if remaining > 0:
            time.sleep(remaining)
        _ARXIV_LAST_REQUEST = time.monotonic()


def _read_feed(source, max_attempts=MAX_FETCH_ATTEMPTS):
    """Retry transient transport failures only, with at most three total requests."""
    retry_errors = []
    max_attempts = max(1, min(MAX_FETCH_ATTEMPTS, max_attempts))
    for attempt in range(1, max_attempts + 1):
        if source["feed_url"].startswith("https://export.arxiv.org/api/"):
            _wait_for_arxiv_api_slot()
        try:
            return _FeedPayload(_read_feed_once(source), attempt, retry_errors)
        except SourceFetchError as exc:
            exc.attempts = attempt
            exc.retry_errors = list(retry_errors)
            if not exc.retryable or attempt >= max_attempts:
                raise
            delay = max(0.5 * (2 ** (attempt - 1)), exc.retry_after_seconds or 0)
            # Respect a long server Retry-After by leaving retry to the next sync,
            # rather than retrying earlier or blocking a worker for minutes.
            if delay > MAX_RETRY_DELAY_SECONDS:
                raise
            retry_errors.append({"attempt": attempt, "error_type": exc.error_type, "http_status": exc.http_status})
            time.sleep(delay)


def _parse_fetched_feed(payload, source, limit):
    try:
        return parse_feed(payload, source, limit=limit)
    except SourceFetchError as exc:
        exc.attempts = getattr(payload, "attempts", 1)
        exc.retry_errors = getattr(payload, "retry_errors", [])
        raise


class _FetchedArticles(list):
    """A normal JSON-serializable list with metadata for fetch_all's source result."""
    def __init__(self, articles, retrieval):
        super().__init__(articles)
        self.retrieval = retrieval


def _read_arxiv_history(source, max_attempts=MAX_FETCH_ATTEMPTS):
    # API request pacing is applied to every attempt in _read_feed.
    return _read_feed(dict(source, feed_url=source["history_feed_url"]), max_attempts=max_attempts)


def fetch_source(source_id, limit=None):
    if not isinstance(source_id, str) or source_id not in _SOURCE_BY_ID:
        raise ValueError("Unknown source_id")
    source = _SOURCE_BY_ID[source_id]
    if limit is None:
        limit = source.get("fetch_limit", 25)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    payload = _read_feed(source)
    articles = _parse_fetched_feed(payload, source, limit)
    attempts = getattr(payload, "attempts", 1)
    retry_errors = list(getattr(payload, "retry_errors", []))
    retrieval = {"retrieval_method": "atom" if source.get("content_kind") == "filing" else "rss", "history_fallback": False,
                 "attempts": attempts, "retry_count": len(retry_errors), "retry_errors": retry_errors,
                 "error_type": None}
    if not articles and source.get("history_feed_url") and attempts >= MAX_FETCH_ATTEMPTS:
        retrieval["detail"] = "RSS 无当期更新；本轮最多 3 次请求预算已用尽，暂未读取历史论文。"
    elif not articles and source.get("history_feed_url"):
        try:
            history = _read_arxiv_history(source, max_attempts=MAX_FETCH_ATTEMPTS - attempts)
            articles = _parse_fetched_feed(history, source, limit)
        except SourceFetchError as exc:
            history_errors = [dict(error, attempt=attempts + error["attempt"]) for error in exc.retry_errors]
            raise SourceFetchError("RSS 无当期论文；官方历史接口未能读取：" + str(exc), error_type=exc.error_type,
                                   attempts=attempts + exc.attempts, retryable=exc.retryable, http_status=exc.http_status,
                                   retry_after_seconds=exc.retry_after_seconds, retry_errors=retry_errors + history_errors) from None
        retry_errors += [dict(error, attempt=attempts + error["attempt"]) for error in getattr(history, "retry_errors", [])]
        retrieval = {"retrieval_method": "atom_history", "history_fallback": True, "error_type": None,
                     "attempts": attempts + getattr(history, "attempts", 1),
                     "retry_count": len(retry_errors), "retry_errors": retry_errors,
                     "detail": "RSS 无当期更新；使用官方 Atom 接口的最近历史论文，非今日新发布。"}
    if source.get("content_kind") == "filing":
        retrieval.update({"requested_limit": limit, "limit_reached": len(articles) >= limit,
                          "coverage_scope": "recent_feed_only",
                          "detail": f"仅采集 SEC 最新申报索引，最多 {limit} 条；达到上限时可能遗漏更多条目。未回补完整历史或下载财报全文，接收时间不等于报告期。"})
    elif source["kind"] == "news":
        retrieval.update({"requested_limit": limit, "limit_reached": len(articles) >= limit,
                          "coverage_scope": "recent_feed_only",
                          "detail": f"仅采集公开媒体 RSS 的标题及摘要，最多 {limit} 条；未抓取付费全文或回补完整历史，媒体报道需结合原始来源核验。"})
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
                        "error": str(exc), "checked_at": _utc_now(), "attempts": exc.attempts,
                        "error_type": exc.error_type, "http_status": exc.http_status,
                        "retry_count": len(exc.retry_errors), "retry_errors": exc.retry_errors,
                        "retry_after_seconds": exc.retry_after_seconds}
        except Exception:
            # No remote exception text (including proxy credentials) enters the public API.
            return [], {"source_id": source_id, "status": "error", "article_count": 0,
                        "error": "订阅处理失败；未生成替代新闻。", "checked_at": _utc_now(),
                        "attempts": 1, "error_type": "internal_error", "retry_count": 0, "retry_errors": []}

    articles, results = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for batch, result in executor.map(run, source_ids):
            articles.extend(batch)
            results.append(result)
    return {"articles": articles, "results": results}
