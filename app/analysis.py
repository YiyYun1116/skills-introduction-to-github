"""Deterministic, auditable news grouping; no factual or investment oracle.

All scores describe the supplied records, never truth or future returns. A
publisher's trust tier is a catalogue entry, not an authentication certificate.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref_src"}
_STOP = {"the", "and", "for", "with", "from", "that", "this", "into", "over", "its", "are", "new", "has", "have", "will", "says", "said", "after", "before", "about", "their", "more", "than", "news", "update"}
_COUNTRIES = {
    "US": ("美国", "美联储", "united states", "u.s.", "us", "usa", "federal reserve"),
    "CN": ("中国", "人民银行", "china", "chinese", "pboc"),
    "JP": ("日本", "日央行", "japan", "japanese", "boj"),
    "GB": ("英国", "英央行", "united kingdom", "uk", "u.k.", "britain", "british", "bank of england"),
    "EU": ("欧洲", "欧盟", "欧央行", "europe", "european", "ecb"),
    "IN": ("印度", "india", "indian"),
    "HK": ("香港", "hong kong"),
}
_POSITIVE = ("增长", "上涨", "上调", "改善", "盈利", "突破", "创新高", "回升", "复苏", "降息", "growth", "grows", "gain", "gains", "rise", "rises", "rally", "profit", "profits", "improve", "improves", "upgrade", "upgraded", "recovery", "record high", "rate cut")
_NEGATIVE = ("下跌", "下降", "下调", "亏损", "衰退", "违约", "风险", "制裁", "收缩", "加息", "fraud", "fall", "falls", "decline", "declines", "loss", "losses", "recession", "default", "risk", "risks", "sanction", "sanctions", "downgrade", "downgraded", "rate hike")
_TIER_RANK = {"primary": 0, "academic": 1, "secondary": 2}


def _text(value):
    return str(value or "").strip()


def _fold(value):
    return unicodedata.normalize("NFKC", _text(value)).casefold()


def _strings(value):
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({_text(item) for item in value if _text(item)})


def canonicalize_url(value):
    """Drop only known tracking parameters; preserve document identifiers."""
    try:
        parsed = urlsplit(_text(value))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        # Credentials are never accepted as evidence links.
        if parsed.username is not None or parsed.password is not None:
            return ""
        host = parsed.hostname.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port and not ((port == 80 and parsed.scheme.lower() == "http") or (port == 443 and parsed.scheme.lower() == "https")):
            host += f":{port}"
        query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                 if not key.lower().startswith("utm_") and key.lower() not in _TRACKING]
        path = parsed.path or "/"
        return urlunsplit((parsed.scheme.lower(), host, path, urlencode(sorted(query)), ""))
    except (TypeError, ValueError, UnicodeError):
        return ""


def normalize_title(value):
    # Do not discard digits, decimal points, percentages or Chinese characters.
    return " ".join(re.findall(r"\d+(?:[.,]\d+)*(?:%|‰)?|[a-z]+|[\u3400-\u9fff]+", _fold(value)))


def _tokens(value):
    value = _fold(value)
    tokens = {word for word in re.findall(r"[a-z][a-z0-9]{1,}", value) if word not in _STOP}
    for phrase in re.findall(r"[\u3400-\u9fff]+", value):
        if len(phrase) == 1:
            tokens.add(phrase)
        else:
            tokens.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return tokens


def _number_signature(value):
    return tuple(re.findall(r"(?<![a-z])[-+]?\d+(?:[.,]\d+)*(?:\s*(?:%|‰|万亿|亿|万|million|billion|trillion|bps|bp|basis points?))?", _fold(value)))


def _country_signature(value):
    value = _fold(value)
    return frozenset(country for country, names in _COUNTRIES.items() if any(_mentions(value, name) for name in names))


def _timestamp(value):
    try:
        timestamp = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        return ""


def _overlap(left, right):
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _same_period(left, right):
    first, second = left["_analysis"]["published_epoch"], right["_analysis"]["published_epoch"]
    if first is None or second is None:
        return True
    return abs(first - second) <= 7 * 86400


def _same_filing(left, right):
    """Match document identities, never a generic quarterly-report headline."""
    one, two = left["_analysis"], right["_analysis"]
    if one["filing_key"] != two["filing_key"]:
        return False
    for field in ("filing_form", "reporting_period"):
        if one[field] and two[field] and one[field] != two[field]:
            return False
    if one["company_cik"] and two["company_cik"]:
        return one["company_cik"] == two["company_cik"]
    return not (one["company_name"] and two["company_name"] and one["company_name"] != two["company_name"])


def _duplicate(left, right):
    if left["is_demo"] != right["is_demo"]:
        return False
    if left["kind"] == right["kind"] == "filing":
        return _same_filing(left, right)
    if "filing" in (left["kind"], right["kind"]) and left["canonical_url"] != right["canonical_url"]:
        return False
    # A recurring headline about a different period is a separate observation.
    if not _same_period(left, right):
        return False
    left_features, right_features = left["_analysis"], right["_analysis"]
    if left_features["numbers"] != right_features["numbers"]:
        return False
    if left_features["countries"] != right_features["countries"]:
        return False
    if left["canonical_url"] and left["canonical_url"] == right["canonical_url"]:
        return True
    one, two = left_features["normalized_title"], right_features["normalized_title"]
    if not one or not two:
        return False
    if one == two:
        # Very short headlines such as "Market update" are not an identity.
        return len(one) >= 12 or len(left_features["title_tokens"]) >= 4
    if left_features["entities"] and right_features["entities"] and not left_features["entities"] & right_features["entities"]:
        return False
    if len(one) < 20 or len(two) < 20:
        return False
    # Character overlap is an upper bound on SequenceMatcher's ratio. Reject
    # impossible fuzzy matches cheaply before computing an alignment.
    common = sum((left_features["characters"] & right_features["characters"]).values())
    if 2 * common / (len(one) + len(two)) < 0.94:
        return False
    if SequenceMatcher(None, one, two, autojunk=False).ratio() < 0.94:
        return False
    # Fuzzy duplicates need matching body content, not just similar headlines.
    return bool(left["summary"] and right["summary"] and
                left_features["summary_numbers"] == right_features["summary_numbers"] and
                _overlap(left_features["summary_tokens"], right_features["summary_tokens"]) >= 0.86)


def _related(left, right):
    if left["is_demo"] != right["is_demo"]:
        return False
    if left["kind"] == right["kind"] == "filing":
        return _same_filing(left, right)
    if left["topic_key"] and right["topic_key"]:
        return left["topic_key"] == right["topic_key"]
    if not _same_period(left, right):
        return False
    left_features, right_features = left["_analysis"], right["_analysis"]
    entities_one, entities_two = left_features["entities"], right_features["entities"]
    entity_match = bool(entities_one & entities_two)
    if entities_one and entities_two and not entity_match:
        return False
    tags_match = bool(left_features["tags"] & right_features["tags"])
    title_overlap = _overlap(left_features["title_tokens"], right_features["title_tokens"])
    all_overlap = _overlap(left_features["all_tokens"], right_features["all_tokens"])
    # Shared geography or asset class alone never defines an event.
    if entity_match:
        return title_overlap >= 0.22 or (tags_match and all_overlap >= 0.15)
    return title_overlap >= 0.60 and (tags_match or all_overlap >= 0.50)


def _source_map(sources):
    if isinstance(sources, dict):
        sources = sources.values()
    return {_text(source.get("id")): source for source in sources if isinstance(source, dict) and source.get("id")}


def _source_domain_matches(url, source):
    if not url:
        return False
    if urlsplit(url).hostname in source.get('article_hosts', []):
        return True
    expected = []
    for field in ("url", "feed_url"):
        source_url = canonicalize_url(source.get(field))
        if source_url:
            expected.append(urlsplit(source_url).hostname.removeprefix("www."))
    if not expected:
        return False
    hostname = urlsplit(url).hostname.removeprefix("www.")
    return any(hostname == domain or hostname.endswith("." + domain) for domain in expected)


def _prepare(article, sources):
    source_id = _text(article.get("source_id"))
    source = sources.get(source_id, {})
    title, summary = _text(article.get("title")), _text(article.get("summary"))
    url = canonicalize_url(article.get("url"))
    published = _timestamp(article.get("published_at"))
    announced = _timestamp(article.get("announced_at"))
    accepted = _timestamp(article.get("accepted_at"))
    kind = _text(article.get("kind")) or "news"
    filing_activity = (accepted or (announced if article.get("date_basis") == "filing_accepted" else "")) if kind == "filing" else ""
    activity = filing_activity or announced or published
    activity_basis = "filing_accepted" if filing_activity else "feed_announcement" if announced else "publication"
    demo = bool(article.get("is_demo", False))
    identity = "\n".join((source_id, url, title, published, str(demo)))
    identifier = _text(article.get("id")) or "a-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    normalized_title = normalize_title(title)
    accession = re.sub(r"[^0-9]", "", _text(article.get("accession_number")))
    filing_key = "accession:" + accession if len(accession) == 18 else "url:" + url if url else "id:" + identifier
    company_cik = _text(article.get("company_cik")).lstrip("0")
    # Per-call features are released with the result computation. A global
    # cache of thousands of long news summaries would retain too much memory.
    features = {
        "normalized_title": normalized_title, "numbers": _number_signature(title),
        "countries": _country_signature(title), "characters": Counter(normalized_title),
        "title_tokens": _tokens(title), "summary_tokens": _tokens(summary),
        "summary_numbers": _number_signature(summary), "all_tokens": _tokens(title + " " + summary),
        "entities": set(map(_fold, _strings(article.get("entities")))),
        "tags": set(map(_fold, _strings(article.get("tags")))),
        "numeric_skeleton": re.sub(r"[-+]?\d+(?:[.,]\d+)*", "#", normalized_title),
        "published_epoch": datetime.fromisoformat(activity.replace("Z", "+00:00")).timestamp() if activity else None,
        "filing_key": filing_key, "company_cik": company_cik,
        "company_name": _fold(article.get("company_name")),
        "filing_form": _fold(article.get("filing_form")),
        "reporting_period": _fold(article.get("reporting_period") or article.get("report_period") or article.get("period_of_report")),
    }
    return {
        **article,
        "id": identifier, "title": title, "summary": summary,
        "url": url, "canonical_url": url,
        "source_id": source_id, "source_name": _text(source.get("name")) or source_id or "未知来源",
        "source_tier": _text(source.get("tier")) or "unknown",
        "source_kind": _text(source.get("kind")) or "unknown",
        "independence_group": _text(source.get("independence_group")) or source_id or "unknown",
        "source_known": bool(source),
        "source_domain_match": _source_domain_matches(url, source),
        "published_at": published, "announced_at": announced, "accepted_at": accepted, "activity_at": activity,
        "activity_date_basis": activity_basis,
        "ingested_at": _timestamp(article.get("ingested_at")),
        "language": _text(article.get("language")) or "und",
        "asset_classes": _strings(article.get("asset_classes")),
        "regions": _strings(article.get("regions")), "entities": _strings(article.get("entities")),
        "tags": _strings(article.get("tags")), "topic_key": _text(article.get("topic_key")),
        "kind": kind, "is_demo": demo,
        "duplicate_of": None,
        "_analysis": features,
    }


def _article_order(article):
    return (not article["source_domain_match"], _TIER_RANK.get(article["source_tier"], 3), article["activity_at"] or "9999", article["source_id"], article["id"], article["title"])


def _mentions(value, keyword):
    if keyword.isascii():
        return bool(re.search(r"(?<![a-z])" + re.escape(keyword) + r"(?![a-z])", value))
    return keyword in value


def _sentiment(articles):
    counts = Counter({"positive": 0, "negative": 0, "neutral": 0})
    scores = []
    for article in articles:
        words = _fold(article["title"] + " " + article["summary"])
        positive = sum(_mentions(words, word) for word in _POSITIVE)
        negative = sum(_mentions(words, word) for word in _NEGATIVE)
        score = (positive - negative) / max(1, positive + negative)
        counts["positive" if score > 0 else "negative" if score < 0 else "neutral"] += 1
        scores.append(score)
    score = round(100 * sum(scores) / max(1, len(scores)))
    return {
        "label": "措辞偏积极" if score > 15 else "措辞偏消极" if score < -15 else "措辞中性 / 混合",
        "score": score, **counts,
        "method": "对去重后的标题及摘要做中英文关键词计数；范围 -100 至 100，仅衡量文本措辞，不能代表市场情绪或收益。未处理全部否定、讽刺及语境。",
    }


def _numeric_conflicts(articles):
    conflicts = []
    conflict_count = 0
    conflict_ids = set()
    skeleton_groups = {}
    for article in articles:
        if not article["_analysis"]["numbers"]:
            continue
        skeleton_groups.setdefault(article["_analysis"]["numeric_skeleton"], []).append(article)
    for group in skeleton_groups.values():
        number_counts = Counter(article["_analysis"]["numbers"] for article in group)
        if len(number_counts) <= 1:
            continue
        conflict_count += len(group) * (len(group) - 1) // 2 - sum(count * (count - 1) // 2 for count in number_counts.values())
        conflict_ids.update(article["id"] for article in group)
        if len(conflicts) >= 50:
            continue
        for index, article in enumerate(group):
            for other in group[index + 1:]:
                if article["_analysis"]["numbers"] != other["_analysis"]["numbers"]:
                    conflicts.append({"article_ids": [article["id"], other["id"]], "reason": "相似标题包含不同数值；可能是时间或统计口径不同，需要查阅原文。"})
                    if len(conflicts) >= 50:
                        break
            if len(conflicts) >= 50:
                break
    return conflicts, conflict_count, conflict_ids


def _verification(articles, independent_count, conflicts, demo):
    traceable = [article for article in articles if article["source_known"] and article["source_domain_match"]]
    primary = any(article["source_tier"] == "primary" for article in traceable)
    academic = any(article["source_tier"] == "academic" for article in traceable)
    limitations = ["来源评级依据预设目录；来源身份、原文内容和具体主张尚未人工核验。", "多来源报道同一话题不等于每项主张已获得独立证实；转载不能增加独立证据。"]
    if not traceable:
        status, label, score = "untraceable", "缺少可追溯来源", 0
        explanation = "没有同时具备来源目录记录及匹配该目录域名的原文链接的独立条目。"
    elif independent_count >= 2:
        status, label = "traceable_multiple", "多来源线索 · 待交叉核验"
        score = min(90, (65 if primary else 58 if academic else 45) + 8 * min(independent_count - 1, 3))
        explanation = f"去重后可追溯至 {independent_count} 个来源主体，具备交叉核验线索；尚未完成逐项事实核查。"
    elif primary:
        status, label, score = "traceable_single", "一手来源 · 待独立核验", 68
        explanation = "存在目录标注的一手材料，但单一来源不构成独立核实。"
    elif academic:
        status, label, score = "traceable_single", "学术来源 · 待复核", 58
        explanation = "存在学术材料；论文发表或预印本公开不等于结论已获验证。"
    else:
        status, label, score = "limited", "单来源线索 · 待核验", 38
        explanation = "可追溯来源有限，需要寻找原始材料和独立报道。"
    if any(not article["published_at"] and article["activity_date_basis"] != "filing_accepted" for article in articles):
        score = max(0, score - 8)
        limitations.append("部分条目缺少有效发布时间，无法确认信息时效。")
    if any(article["kind"] == "research" and article["activity_date_basis"] == "feed_announcement" for article in articles):
        limitations.append("论文订阅公告日期可能代表修订或交叉收录，不等于原始发表日期；仅用于活动排序。")
    if any(article["kind"] == "filing" for article in articles):
        limitations.append("SEC 申报受理时间不等于报告期末或财报首次发布日；申报日期也不能用于推算所属季度。")
        limitations.append("当前申报材料为索引或摘要，尚未提取核对财务报表正文；SEC 收录不代表财务数字已获监管认可或独立核实。")
    if len(traceable) != len(articles):
        score = max(0, score - 10)
        limitations.append("部分条目来源未登记、缺少有效原文链接或链接域名不匹配来源目录。")
    if conflicts:
        score = max(0, score - 15)
        label += " · 数值待核对"
        limitations.append("相似标题出现数值差异，不能自动认定为矛盾或选择其中一个数值。")
    if demo:
        limitations.insert(0, "演示数据；该分数仅展示方法，不代表当前新闻或真实核验结果。")
    return {"status": status, "label": label, "score": score,
            "score_type": "provenance", "score_label": "来源可追溯度（非真实性概率）",
            "explanation": explanation, "limitations": limitations}


def scenario_for_cluster(cluster):
    """Qualitative conditional pathways, never predicted prices/probabilities."""
    text = _fold(" ".join([cluster.get("title", ""), cluster.get("summary", "")] + list(cluster.get("tags", []))))
    asset_classes = set(cluster.get("asset_classes", []))
    articles = cluster.get("articles", [])
    research_only = bool(articles) and all(article.get("kind") == "research" for article in articles)
    filing_present = any(article.get("kind") == "filing" for article in articles)
    if filing_present:
        bull = "只有在申报正文确认营收、利润及经营现金流改善，并核对报告期间和可比口径后，才可讨论公司基本面改善；当前索引不能支持这一结论。"
        base = "目前只确认存在申报索引线索，尚未核对财务数字、报告期及管理层讨论，暂不判断业绩变化或股票方向。"
        bear = "若正文显示盈利质量下降、现金流不足或重大风险增加，需进一步评估；这些情况尚未由当前索引证实。"
        watch = ["公司名称、CIK 与申报编号是否对应", "10-K / 10-Q 正文及报告期末", "营收、利润、现金流及单位和同比口径", "修订申报、重述、审计意见与风险因素"]
    elif research_only:
        bull = "若样本外检验、可复现方法及独立研究支持论文结论，相关方法可能帮助理解风险或改进研究流程；不能直接推出资产涨跌。"
        base = "若结论目前仅在特定样本和假设下成立，保留为研究线索，并核对方法、数据和适用市场。"
        bear = "若复现失败、存在前视或选择偏差，或忽略交易成本和流动性约束，结论的实际适用性可能显著下降。"
        watch = ["论文原文、版本及发表状态", "作者、机构和利益冲突披露", "数据来源、样本外检验及可复现代码", "独立复现、交易成本和适用条件"]
    elif any(word in text for word in ("利率", "降息", "加息", "通胀", "央行", "美联储", "federal reserve", "inflation", "interest rate", "monetary")):
        bull = "若后续通胀及政策原文确认融资条件改善，且企业盈利保持稳定，利率敏感资产可能受益。"
        base = "若政策路径与市场预期接近，关注期限结构及盈利差异，资产表现可能分化。"
        bear = "若通胀再度走高、政策更紧或增长明显放缓，估值及信用风险可能承压。"
        watch = ["央行原文与会议纪要", "通胀及就业后续发布", "国债收益率曲线", "政策实施日期和适用范围"]
    elif any(word in text for word in ("财报", "盈利", "业绩", "earnings", "revenue", "profit")):
        bull = "若正式财报确认盈利及现金流改善，并在后续季度持续，相关公司的基本面可能改善。"
        base = "若业绩符合预期，需结合估值、现金流及行业需求观察；本平台没有足够价格数据判断股价方向。"
        bear = "若收入质量、利润率或指引弱于原始预期，相关公司可能面临估值及经营压力。"
        watch = ["正式财报与监管披露", "收入及现金流口径", "管理层指引和执行情况", "同行业可比数据"]
    elif "crypto" in asset_classes:
        bull = "若相关政策正式生效、资金及使用需求持续增加，相关数字资产可能受益；需核对适用资产和司法辖区。"
        base = "若消息尚待落地，关注官方文本、链上数据口径和流动性变化，避免把消息热度视作资金流入。"
        bear = "若监管限制扩大、流动性收紧或发生技术安全事件，相关数字资产可能承压。"
        watch = ["监管机构及项目官方原文", "生效日期与司法辖区", "可复核的链上活动与资金流", "流动性、杠杆和托管风险"]
    elif "futures" in asset_classes or any(word in text for word in ("能源", "石油", "原油", "commodity", "oil", "supply")):
        bull = "若可核实的需求增长或供应约束持续，相关商品可能获得价格支撑。"
        base = "若供需变化相互抵消，关注库存、期限结构和现货基差；不同合约表现可能不同。"
        bear = "若需求减弱、库存累积或供给恢复，相关商品可能承压。"
        watch = ["官方供需和库存数据", "合约代码、交割地与到期日", "现货基差及期限结构", "政策执行和运输情况"]
    else:
        bull = "若原始材料获得核实，且后续数据确认需求、盈利或融资条件改善，相关行业可能受益。"
        base = "若消息尚未改变可量化的基本面，持续观察实施进度和后续披露，暂不判断价格方向。"
        bear = "若政策执行、需求或盈利与积极预期相反，相关行业及公司可能承压。"
        watch = ["原始文件和正式披露", "独立来源的后续核验", "政策生效日期和适用对象", "可量化的需求、盈利及现金流数据"]
    if "options" in asset_classes:
        watch += ["期权到期日、行权价、隐含波动率及 Greeks；标的方向不能单独决定期权盈亏"]
    return {"horizon": "事件后 1–3 个月的条件观察框架", "bull": bull, "base": base, "bear": bear,
            "watch": watch, "limitations": ["规则模板生成的条件情景，没有使用实时行情、估值模型或回测，不构成已验证的预测。", "不提供目标价格、涨跌概率或交易指令；需人工核验原始材料及资产对应关系。"]}


def analyze_articles(articles, sources):
    """Group related records without mutating inputs; return JSON-ready clusters.

    Demo/live partitions are strict. Source independence is conservative: a
    duplicate copy cannot add a vote, and related publishers share one vote.
    """
    catalog = _source_map(sources)
    prepared = [_prepare(article, catalog) for article in articles if isinstance(article, dict)]
    prepared.sort(key=_article_order)
    # Repeated ingestion of the identical ID contributes one record, not noise.
    seen = set()
    records = []
    for article in prepared:
        identity = (article["id"], article["is_demo"])
        if identity not in seen:
            records.append(article)
            seen.add(identity)

    representatives = []
    duplicate_buckets = {}
    filing_buckets = {}
    copies = {}
    for article in records:
        features = article["_analysis"]
        bucket_key = (article["is_demo"], features["numbers"], features["countries"])
        candidates = duplicate_buckets.setdefault(bucket_key, [])
        filing_candidates = filing_buckets.setdefault((article["is_demo"], features["filing_key"]), []) if article["kind"] == "filing" else []
        original = next((prior for prior in filing_candidates + candidates if _duplicate(article, prior)), None)
        if original:
            article["duplicate_of"] = original["id"]
            copies[(original["id"], original["is_demo"])].append(article)
        else:
            representatives.append(article)
            candidates.append(article)
            if article["kind"] == "filing":
                filing_candidates.append(article)
            copies[(article["id"], article["is_demo"])] = []

    groups = []
    for article in representatives:
        # Check against the stable group representative; do not let a vague
        # bridge item merge two otherwise unrelated topics transitively.
        group = next((group for group in groups if _related(article, group[0]) and
                      all(article["kind"] != "filing" or prior["kind"] != "filing" or _same_filing(article, prior)
                          for prior in group)), None)
        if group is None:
            groups.append([article])
        else:
            group.append(article)

    clusters = []
    for originals in groups:
        all_articles = [entry for original in originals for entry in [original] + copies[(original["id"], original["is_demo"])]]
        all_articles.sort(key=lambda article: (article["activity_at"], article["id"]), reverse=True)
        source_groups = {article["independence_group"] for article in originals if article["source_known"] and article["source_domain_match"]}
        conflicts, conflict_count, conflict_ids = _numeric_conflicts(originals)
        lead = originals[0]
        latest_article = max(originals, key=lambda article: article["activity_at"])
        topic_keys = sorted({article["topic_key"] for article in originals if article["topic_key"]})
        stable_key = ("demo:" if lead["is_demo"] else "live:") + (topic_keys[0] if topic_keys else "|".join(sorted(article["id"] for article in originals)))
        if topic_keys and any(article["kind"] == "filing" for article in originals):
            stable_key += ":filing:" + "|".join(sorted(article["id"] for article in originals if article["kind"] == "filing"))
        evidence = []
        for article in originals:
            evidence.append({"claim": article["title"], "supporting_article_ids": [article["id"]],
                             "status": "needs_review" if article["id"] in conflict_ids else "reported",
                             "label": "待核对数值与原文" if article["id"] in conflict_ids else "来源报告，未经逐项独立核实"})
        cluster = {
            "id": "topic-" + hashlib.sha256(stable_key.encode()).hexdigest()[:16],
            "title": lead["title"] or "待补充标题", "summary": lead["summary"],
            "topic": topic_keys[0] if topic_keys else lead["title"],
            "asset_classes": sorted({item for article in originals for item in article["asset_classes"]}),
            "regions": sorted({item for article in originals for item in article["regions"]}),
            "entities": sorted({item for article in originals for item in article["entities"]}),
            "tags": sorted({item for article in originals for item in article["tags"]}),
            "article_count": len(all_articles), "unique_count": len(originals),
            "duplicate_count": len(all_articles) - len(originals),
            "independent_sources": len(source_groups),
            "source_ids": sorted({article["source_id"] for article in all_articles if article["source_id"]}),
            "latest_at": latest_article["activity_at"],
            "latest_date_basis": latest_article["activity_date_basis"],
            "verification": _verification(originals, len(source_groups), conflicts, lead["is_demo"]),
            "sentiment": _sentiment(originals),
            "articles": [{key: value for key, value in article.items() if key != "_analysis"} for article in all_articles],
            "evidence": evidence, "conflicts": conflicts, "conflict_count": conflict_count, "is_demo": lead["is_demo"],
        }
        if conflict_count > len(conflicts):
            cluster["verification"]["limitations"].append(f"检测到 {conflict_count} 组待核对数值差异；仅列出前 {len(conflicts)} 组，所有涉及条目均已标记待核对。")
        cluster["scenario"] = scenario_for_cluster(cluster)
        clusters.append(cluster)
    return sorted(clusters, key=lambda cluster: (cluster["latest_at"], cluster["id"]), reverse=True)


def market_summary(clusters):
    """Summarize article wording and provenance, never measured market mood."""
    asset_labels = {"equity": "股票", "futures": "期货", "options": "期权", "crypto": "数字资产", "macro": "宏观政策"}
    assets = []
    for asset_class, label in asset_labels.items():
        subset = [cluster for cluster in clusters if asset_class in cluster["asset_classes"]]
        total = sum(cluster["unique_count"] for cluster in subset)
        score = round(sum(cluster["sentiment"]["score"] * cluster["unique_count"] for cluster in subset) / max(1, total))
        assets.append({"id": asset_class, "asset_class": asset_class, "label": label,
                       "topic_count": len(subset), "article_count": total,
                       "sentiment_score": score, "sentiment_label": "暂无样本" if not total else "措辞偏积极" if score > 15 else "措辞偏消极" if score < -15 else "措辞中性 / 混合"})
    total_articles = sum(cluster["article_count"] for cluster in clusters)
    duplicate_count = sum(cluster["duplicate_count"] for cluster in clusters)
    unique_count = sum(cluster["unique_count"] for cluster in clusters)
    return {
        "topic_count": len(clusters), "article_count": total_articles,
        "unique_count": unique_count, "duplicate_count": duplicate_count,
        "duplicate_rate": round(100 * duplicate_count / max(1, total_articles), 1),
        "source_count": len({source_id for cluster in clusters for source_id in cluster["source_ids"]}),
        "primary_source_topics": sum(any(article["source_tier"] == "primary" for article in cluster["articles"]) for cluster in clusters),
        "multiple_source_topics": sum(cluster["independent_sources"] >= 2 for cluster in clusters),
        "verification_counts": dict(Counter(cluster["verification"]["status"] for cluster in clusters)),
        "sentiment_score": round(sum(cluster["sentiment"]["score"] * cluster["unique_count"] for cluster in clusters) / max(1, unique_count)),
        "assets": assets,
        "latest_at": max((cluster["latest_at"] for cluster in clusters), default=""),
        "demo_topic_count": sum(cluster["is_demo"] for cluster in clusters),
        "live_topic_count": sum(not cluster["is_demo"] for cluster in clusters),
        "method": "基于当前条目的去重、来源目录和措辞关键词；不代表全市场覆盖、事实认证或投资预测。",
    }
