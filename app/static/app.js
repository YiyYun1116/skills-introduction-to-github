'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const list = value => Array.isArray(value) ? value : value == null ? [] : [value];
  const string = value => value == null ? '' : typeof value === 'object' ? Object.values(value).map(string).join(' · ') : String(value);
  const esc = value => string(value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const formatNumber = value => number(value).toLocaleString('zh-CN');
  const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const assets = {all:'全部资产',equity:'股票',futures:'期货',options:'期权',crypto:'数字资产',macro:'宏观政策'};
  const regions = {GLOBAL:'全球',US:'美国',CN:'中国内地',HK:'中国香港',EU:'欧洲'};
  const views = {overview:'情报总览',themes:'主题聚合',research:'研究与论文',watchlist:'关注资产',sources:'数据来源'};
  const state = {data:null,mode:null,view:'overview',asset:'all',region:'all',primary:false,query:'',sort:'latest',watchlist:[],loading:false,syncing:false,request:0,drawer:null,previousFocus:null,pollTimer:null,error:null,watchError:null};
  let searchTimer;
  state.relatedItem = null;
  state.modePinned = false;

  function matchesAsset(cluster,item) {
    if (!list(cluster.asset_classes).includes(item.asset_class)) return false;
    if (item.region !== 'GLOBAL' && !list(cluster.regions).some(region => region === item.region || region === 'GLOBAL')) return false;
    const text = string([cluster.title,cluster.summary,cluster.entities]).toLowerCase();
    const pattern = value => value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    const matches = (term,isSymbol = false) => {
      term = string(term).trim().toLowerCase();
      if (!term) return false;
      if (/[^\x00-\x7F]/.test(term)) return text.includes(term);
      if (isSymbol && term.length < 3) return new RegExp(`(?:\\$|\\(|\\b(?:nyse|nasdaq):\\s*)${pattern(term)}(?=$|[^a-z0-9-])`,'i').test(text);
      return new RegExp(`(?:^|[^a-z0-9])${pattern(term)}(?=$|[^a-z0-9])`,'i').test(text);
    };
    return matches(item.symbol,true) || matches(item.name);
  }

  function safeUrl(value) {
    try { const url = new URL(string(value)); return ['http:','https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''; }
    catch (_) { return ''; }
  }
  function date(value, full = false) {
    if (!value) return '时间未提供';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '时间未提供';
    return new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',...(full ? {year:'numeric',hour:'2-digit',minute:'2-digit'} : {})}).format(parsed);
  }
  function sourceById(id) { return list(state.data?.sources).find(source => source.id === id) || {id,name:'未登记来源',tier:'unknown'}; }
  function clusterDate(cluster,full=false) { return `${cluster.latest_date_basis === 'feed_announcement' ? '订阅公告 ' : ''}${date(cluster.latest_at,full)}`; }
  function articleDate(article) {
    if (article.announced_at) return `订阅公告 ${date(article.announced_at,true)} · 原始发表日期${article.published_at ? date(article.published_at,true) : '未提供'}`;
    return `发布 ${date(article.published_at,true)}${article.updated_at ? ` · 修订 ${date(article.updated_at,true)}` : ''}`;
  }
  function sourceName(id) { return string(sourceById(id).name) || '未登记来源'; }
  function mono(source) {
    const name = string(source.name).replace(/^演示\s*·\s*/, '');
    const latin = name.match(/\b[A-Z]{2,}/);
    return latin ? latin[0].slice(0,3) : name.slice(0,2);
  }
  function sentimentClass(score) { return number(score) > 15 ? 'positive' : number(score) < -15 ? 'negative' : 'neutral'; }
  function shortSentiment(score) { return number(score) > 15 ? '措辞偏积极' : number(score) < -15 ? '措辞偏消极' : '措辞中性'; }
  function sourceState(source) {
    const result = list(state.data?.ingestion?.results).find(item => item.source_id === source.id);
    const value = string(result?.status || source.status).toLowerCase();
    if (source.is_demo) return {label:'演示来源',kind:'unknown',detail:'虚构材料，仅用于体验'};
    if (source.enabled === false) return {label:'手动查阅',kind:'unknown',detail:'此来源未启用自动订阅，可访问官网查阅原始材料。'};
    if (['ok','success','available','healthy'].includes(value)) return {label:'已连接',kind:'',detail:result?.detail || result?.message || ''};
    if (value === 'empty') return {label:'订阅暂无更新',kind:'unknown',detail:result?.detail || '订阅读取成功，本次没有可用条目。'};
    if (['error','failed','blocked','unavailable'].includes(value)) return {label:'连接受限',kind:'error',detail:string(result?.error || result?.message || source.last_check?.error || '本次采集失败，请检查环境网络设置后重试。')};
    return {label:state.data?.ingestion?.running ? '同步中' : '尚未检查',kind:'unknown',detail:''};
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {...options,headers:{'Accept':'application/json',...(options.body ? {'Content-Type':'application/json'} : {}),...options.headers},signal:controller.signal});
      let result;
      try { result = await response.json(); } catch (_) { result = {}; }
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `请求未完成（${response.status}），请稍后重试。`);
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('服务响应超时，请检查服务状态后重试。');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function toast(message, isError = false) {
    const node = document.createElement('div'); node.className = `toast${isError ? ' error' : ''}`; node.textContent = message;
    $('#toast-container').append(node); setTimeout(() => node.remove(), 5200);
  }
  function empty(title, description, action = '', actionLabel = '', glyph = 'layers') {
    return `<div class="empty-state"><div class="empty-icon">${icon(glyph)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action ? `<button class="button primary" data-action="${esc(action)}">${esc(actionLabel)} ${icon('arrow')}</button>` : ''}</div>`;
  }
  function demoBanner() {
    return state.mode === 'demo' ? `<div class="demo-banner">${icon('info')}<span><strong>演示模式</strong> · 当前内容为虚构样本，时间固定于 2026 年 1 月，来源与分析均非真实市场信息。</span><button class="text-link" data-action="switch-live">查看真实数据 ${icon('arrow')}</button></div>` : '';
  }
  function header(title, subtitle, eyebrow, extra = '') {
    const syncing = state.syncing || state.data?.ingestion?.running;
    return `<div class="page-heading"><div><p class="eyebrow">${esc(eyebrow)}</p><h1>${esc(title)}</h1><p class="page-subtitle">${esc(subtitle)}</p></div><div class="heading-actions">${extra}<a class="button" href="/api/export?mode=${state.mode || 'live'}" download>${icon('download')} 导出情报</a><button class="button primary" data-action="sync" ${syncing ? 'disabled' : ''}>${icon('refresh')} ${syncing ? '正在同步' : '同步公开来源'}</button></div></div>`;
  }
  function toolbar() {
    return `<div class="workspace-toolbar"><div class="asset-tabs" role="group" aria-label="资产类别筛选">${Object.entries(assets).map(([id,label]) => `<button class="asset-tab ${state.asset === id ? 'active' : ''}" data-action="asset" data-id="${id}" aria-pressed="${state.asset === id}">${id === 'all' ? icon('globe') : ''}${label}</button>`).join('')}</div><div class="toolbar-options"><select class="compact-select" id="region-filter" aria-label="市场地区"><option value="all">全部市场</option>${Object.entries(regions).map(([id,label]) => `<option value="${id}" ${state.region === id ? 'selected' : ''}>${label}</option>`).join('')}</select><label class="filter-check"><input type="checkbox" id="primary-filter" ${state.primary ? 'checked' : ''}>含一手来源</label></div></div>`;
  }
  function filteredClusters() {
    let clusters = list(state.data?.clusters).filter(cluster => {
      if (state.relatedItem && !matchesAsset(cluster,state.relatedItem)) return false;
      if (state.asset !== 'all' && !list(cluster.asset_classes).includes(state.asset)) return false;
      if (state.region !== 'all' && !list(cluster.regions).includes(state.region)) return false;
      if (state.primary && !list(cluster.source_ids).some(id => sourceById(id).tier === 'primary')) return false;
      return !state.query || string([cluster.title,cluster.summary,cluster.topic,cluster.entities,cluster.tags,list(cluster.source_ids).map(sourceName)]).toLowerCase().includes(state.query.toLowerCase());
    });
    return clusters.sort((a,b) => state.sort === 'sources' ? number(b.independent_sources) - number(a.independent_sources) : string(b.latest_at).localeCompare(string(a.latest_at)));
  }
  function clusterCard(cluster, index = 0) {
    const category = list(cluster.asset_classes).find(value => value === state.asset) || list(cluster.asset_classes)[0] || 'macro';
    const sources = list(cluster.source_ids).map(sourceById);
    const score = cluster.sentiment?.score;
    const independent = number(cluster.independent_sources);
    const badge = independent > 1 ? `${independent} 个来源线索` : '单来源待复核';
    const label = cluster.is_demo ? '演示' : assets[category] || '公开情报';
    return `<article class="topic-card"><div class="topic-topline"><span class="topic-category ${esc(category)}">${esc(label)}</span>${list(cluster.tags).slice(0,1).map(tag => `<span class="topic-category">${esc(tag)}</span>`).join('')}<span class="topic-time">${icon('clock')} ${clusterDate(cluster)}</span></div><h3><button class="topic-title-button" data-action="open-cluster" data-id="${esc(cluster.id)}">${esc(cluster.title)}</button></h3><p class="topic-summary">${esc(cluster.summary || '摘要暂缺，请打开原文查阅。')}</p><div class="topic-footer"><div class="source-stack">${sources.slice(0,3).map(source => `<span class="source-monogram" title="${esc(source.name)}">${esc(mono(source))}</span>`).join('')}<span class="source-caption">${formatNumber(cluster.unique_count)} 条去重材料${number(cluster.duplicate_count) ? ` · 合并 ${formatNumber(cluster.duplicate_count)} 条重复` : ''}</span></div><div class="card-meta"><span class="evidence-badge ${independent < 2 ? 'pending' : ''}" title="${esc(cluster.verification?.label)}">${icon('shield')}${badge}</span><span class="sentiment-pill ${sentimentClass(score)}">${shortSentiment(score)}</span><button class="topic-open" data-action="open-cluster" data-id="${esc(cluster.id)}" aria-label="查看${esc(cluster.title)}的证据与分析">${icon('arrow')}</button></div></div></article>`;
  }
  function metrics() {
    const stats = state.data?.stats || {};
    const articles = number(stats.articles), duplicates = number(stats.duplicates), unique = Math.max(0,articles - duplicates);
    const rate = articles ? Math.round(duplicates / articles * 100) : 0;
    return `<section class="metrics-grid" aria-label="当前数据集统计"><div class="metric-card"><div class="metric-top">去重后信息<span class="metric-icon">${icon('layers')}</span></div><div class="metric-value">${formatNumber(unique)}<small>条</small></div><div class="metric-foot"><span class="green">${formatNumber(articles)}</span> 条原始材料 · 当前数据集</div></div><div class="metric-card"><div class="metric-top">追踪中的主题<span class="metric-icon">${icon('globe')}</span></div><div class="metric-value">${formatNumber(stats.clusters)}<small>个</small></div><div class="metric-foot">跨资产关联 · 同题证据聚合</div></div><div class="metric-card featured"><div class="metric-top">已合并重复信息<span class="metric-icon">${icon('filter')}</span></div><div class="metric-value">${formatNumber(duplicates)}<small>条</small></div><div class="metric-foot"><span class="green">${rate}%</span> 重复占比 · 转载不增加独立性</div></div><div class="metric-card"><div class="metric-top">实际引用一手来源<span class="metric-icon">${icon('shield')}</span></div><div class="metric-value">${formatNumber(stats.primary_sources)}<small>家</small></div><div class="metric-foot">共 ${formatNumber(stats.independent_sources)} 个独立来源主体</div></div></section>`;
  }
  function sentimentPanel(clusters) {
    const categories = ['equity','futures','options','crypto'];
    return `<section class="panel"><div class="panel-head"><div><h2 class="panel-title">${icon('trend')} 领域情绪温度</h2><p class="panel-subtitle">基于当前筛选的去重材料措辞</p></div></div><div class="sentiment-legend"><span><i class="legend-dot"></i>偏消极</span><span><i class="legend-dot neutral"></i>中性 / 混合</span><span><i class="legend-dot positive"></i>偏积极</span></div><div class="sentiment-grid">${categories.map(category => {
      const subset = clusters.filter(cluster => list(cluster.asset_classes).includes(category));
      const weight = subset.reduce((sum,cluster) => sum + number(cluster.unique_count),0);
      const score = weight ? Math.round(subset.reduce((sum,cluster) => sum + number(cluster.sentiment?.score) * number(cluster.unique_count),0) / weight) : 0;
      return `<button class="sentiment-tile ${subset.length ? sentimentClass(score) : 'no-data'}" data-action="asset" data-id="${category}" aria-label="筛选${assets[category]}主题"><strong>${assets[category]}</strong>${icon('arrow')}<span>${subset.length ? shortSentiment(score) : '暂无信息样本'}</span><small>${subset.length ? `${formatNumber(subset.length)} 个主题 · 措辞分 ${score > 0 ? '+' : ''}${score}` : '等待公开来源接入'}</small></button>`;
    }).join('')}</div><p class="panel-disclaimer">词典评分 −100 至 100；不代表市场涨跌、投资者情绪或真实资金流。</p></section>`;
  }
  function sourcePanel() {
    const sources = list(state.data?.sources).filter(source => !source.is_demo && source.enabled !== false);
    const connected = sources.filter(source => sourceState(source).label === '已连接').length;
    return `<section class="panel"><div class="panel-head"><div><h2 class="panel-title">${icon('globe')} 来源连接状态</h2><p class="panel-subtitle">${connected} / ${sources.length} 个订阅已连接</p></div><a class="text-link" href="#sources">管理 ${icon('arrow')}</a></div><div class="source-health">${sources.slice(0,4).map(source => {const status = sourceState(source); return `<div class="health-row"><span class="source-icon">${esc(mono(source))}</span><span class="health-source-name">${esc(source.name)}</span><span class="health-status ${status.kind}">${status.label}</span></div>`;}).join('') || '<div class="filter-empty">暂无配置的数据源</div>'}</div><p class="panel-disclaimer">${sources.some(source => sourceState(source).kind === 'error') ? '部分来源访问受限。可在数据来源页查看原因，调整环境网络设置后重试。' : '来源等级衡量可追溯性，不能替代逐项事实核验。'}</p></section>`;
  }
  function noClusters() {
    if (state.relatedItem || state.query || state.asset !== 'all' || state.region !== 'all' || state.primary) return empty('没有符合条件的主题','尝试其他关键词，或清除筛选条件查看全部材料。','reset-filters','清除筛选','search');
    return empty('等待第一条真实信息','目前还没有成功采集的公开材料。同步来源后，主题聚合、证据溯源与情绪分析将在这里生成。','sync','同步公开来源','globe');
  }
  function overview() {
    const clusters = filteredClusters();
    return `${header('看清信息，连接洞见。','追踪全球市场的重要变化，从原始信息走向有依据的判断。','GLOBAL INTELLIGENCE / 全球金融情报')}${demoBanner()}${metrics()}${toolbar()}${searchInfo()}<div class="dashboard-layout"><section class="panel"><div class="panel-head"><div><h2 class="panel-title">值得关注的主题 <span class="count-label">${clusters.length} TOPICS</span></h2><p class="panel-subtitle">同一事件，多源聚合。保留证据，过滤重复。</p></div>${sortSelect()}</div><div class="topic-list">${clusters.slice(0,5).map(clusterCard).join('') || noClusters()}</div>${clusters.length ? `<div class="panel-bottom"><a href="#themes" class="text-link">查看全部 ${clusters.length} 个主题 ${icon('arrow')}</a></div>` : ''}</section><aside class="side-panels">${sentimentPanel(clusters)}${sourcePanel()}<section class="insight-panel"><div class="insight-label">${icon('book')} THE RESEARCH MINDSET</div><h3>越是纷繁，<br>越要回到第一手材料。</h3><p>将政策原文、学术研究与新闻放在一起，辨认共识，也看见分歧。</p><a class="text-link" href="#research">探索研究与论文 ${icon('arrow')}</a></section></aside></div><div class="coverage-line"><span>${icon('shield')} 可追溯 ≠ 已证实；多源线索仍需交叉核验</span><span>${state.mode === 'demo' ? '演示快照 · 2026 年 1 月' : `最近采集：${date(state.data?.ingestion?.last_run,true)}（北京时间）`}</span></div>`;
  }
  function searchInfo() { return state.query || state.relatedItem ? `<div class="search-active">${icon('search')} ${state.relatedItem ? `候选资产关联：${esc(state.relatedItem.symbol)} · ${esc(state.relatedItem.name)}（名称/代码与分类匹配，需人工确认）` : `搜索“${esc(state.query)}”`}<button class="text-link" data-action="clear-search">清除 ${icon('close')}</button></div>` : ''; }
  function sortSelect() { return `<select class="compact-select sort-select" aria-label="主题排序"><option value="latest" ${state.sort === 'latest' ? 'selected' : ''}>最近活动</option><option value="sources" ${state.sort === 'sources' ? 'selected' : ''}>来源数量</option></select>`; }
  function themes() {
    const clusters = filteredClusters();
    return `${header('把信息，放回同一个语境。','跨来源聚合相关事件，识别转载关系，逐条追踪支持材料。','TOPIC EXPLORER / 主题聚合')}${demoBanner()}${toolbar()}${searchInfo()}<div class="section-tools"><span class="result-count">${clusters.length} 个主题 · 点击主题查看证据链与条件情景</span>${sortSelect()}</div><section class="themes-grid">${clusters.map(clusterCard).join('') || noClusters()}</section>`;
  }
  function research() {
    const clusters = filteredClusters().filter(cluster => list(cluster.articles).some(article => article.kind === 'research' || sourceById(article.source_id).kind === 'research'));
    return `${header('从研究出发，走得更深。','连接公开论文与机构研究，拆解方法、假设和结论的适用边界。','RESEARCH LIBRARY / 研究与论文')}${demoBanner()}<div class="info-strip">${icon('book')}<span>当前收录配置来源的公开订阅。作者身份与研究质量需结合原文复核；预印本不等于同行评审，平台不提供“顶尖人才”排名。</span></div>${toolbar()}${searchInfo()}<div class="section-tools"><span class="result-count">${clusters.length} 个研究主题</span>${sortSelect()}</div>${clusters.map(cluster => {
      const article = list(cluster.articles).find(item => item.kind === 'research' || sourceById(item.source_id).kind === 'research') || list(cluster.articles)[0];
      return `<article class="research-card"><div class="paper-glyph">${icon('book')}</div><div class="research-content"><div class="research-label">${cluster.is_demo ? 'SYNTHETIC RESEARCH · 虚构研究示例' : 'OPEN RESEARCH · 公开研究'}</div><h3><button class="topic-title-button" data-action="open-cluster" data-id="${esc(cluster.id)}">${esc(cluster.title)}</button></h3><p>${esc(cluster.summary)}</p><div class="research-meta"><span>${esc(sourceName(article?.source_id))}</span><span>${clusterDate(cluster,true)}</span>${list(article?.authors).length ? `<span>作者：${esc(list(article.authors).join('、'))}</span>` : ''}<button class="text-link" data-action="open-cluster" data-id="${esc(cluster.id)}">阅读拆解 ${icon('arrow')}</button></div></div></article>`;
    }).join('') || empty('暂无符合条件的研究材料','研究订阅成功采集后将在此展示。可以调整筛选，或前往来源页查看学术来源的连接状态。','go-sources','查看研究来源','book')}`;
  }
  function sources() {
    const catalog = list(state.data?.sources).filter(source => !source.is_demo).filter(source => !state.query || string([source.name,source.description,source.kind,source.region]).toLowerCase().includes(state.query.toLowerCase()));
    return `${header('有出处，才有下一步。','从央行、监管机构和学术平台开始，建立可检查的来源目录。','SOURCE DIRECTORY / 数据来源')}${demoBanner()}<div class="info-strip">${icon('shield')}<span>来源目录与实际采集状态分别展示。连接受限时不会以演示材料填充真实数据；未配置的网站、付费新闻和实时行情尚未接入。点击原始网站可人工核对信息。</span></div>${searchInfo()}<div class="section-tools"><span class="result-count">${catalog.length} 个已配置来源 · HTTPS / 官方公开入口</span><span class="result-count">最近采集 ${date(state.data?.ingestion?.last_run,true)}</span></div><section class="sources-grid">${catalog.map(source => {
      const status = sourceState(source);
      const url = safeUrl(source.url);
      const kind = {official:'官方机构',research:'学术研究',news:'新闻机构',regulator:'监管机构',central_bank:'中央银行',exchange:'交易所'}[source.kind] || string(source.kind);
      const tier = {primary:'一手来源',academic:'学术来源',secondary:'二手报道'}[source.tier] || '待评估';
      return `<article class="source-card"><div class="source-card-top"><span class="source-icon">${esc(mono(source))}</span><div><h3>${esc(source.name)}</h3><span class="source-kind">${esc(kind)} · ${esc(tier)}</span></div></div><p>${esc(source.description || '通过该平台公开入口查阅原始材料。')}</p><div class="source-info"><span>${esc(regions[source.region] || source.region || '全球')} · ${esc(source.language || '多语言')}</span><span class="health-status ${status.kind}">${status.label}</span></div>${status.detail && status.kind !== 'error' ? `<div class="source-disabled">${esc(status.detail)}</div>` : ''}${status.kind === 'error' ? `<div class="source-error">${esc(status.detail)}<br>检查云环境网络域名配置后，可重试本来源。</div>` : source.enabled === false ? `<div class="source-disabled">${esc(status.detail)}</div>` : ''}<div class="source-info"><span>独立主体：${esc(source.independence_group || '未登记')}</span></div><div class="source-card-actions">${url ? `<a class="text-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">原始网站 ${icon('external')}</a>` : '<span class="text-link">缺少原文地址</span>'}${source.enabled !== false ? `<button class="button small" data-action="sync-source" data-id="${esc(source.id)}" ${state.syncing || state.data?.ingestion?.running ? 'disabled' : ''}>${icon('refresh')} 重新同步</button>` : '<span class="source-kind">仅原文入口</span>'}</div></article>`;
    }).join('') || empty('没有匹配的数据来源','尝试其他搜索词，或清除搜索以查看已配置的官方来源。','clear-search','清除搜索','globe')}</section>`;
  }
  function watchlist() {
    const items = list(state.watchlist).filter(item => (state.asset === 'all' || item.asset_class === state.asset) && (state.region === 'all' || item.region === state.region) && (!state.query || string([item.symbol,item.name,item.note]).toLowerCase().includes(state.query.toLowerCase())));
    return `${header('保持关注，持续验证。','记录全球多资产的研究对象与观察假设，建立自己的关注清单。','WATCHLIST / 关注资产')}${demoBanner()}<div class="info-strip">${icon('star')}<span>关注清单保存到本地数据库。股票、期货、期权和数字资产均可记录；当前未接入报价或期权链，研究关联仅通过名称及代码匹配。</span></div><form id="watch-form" class="watch-form"><div class="field"><label for="watch-symbol">资产代码 *</label><input id="watch-symbol" name="symbol" maxlength="40" placeholder="例如 AAPL / BTC-USD" required autocomplete="off"></div><div class="field"><label for="watch-name">资产名称 *</label><input id="watch-name" name="name" maxlength="80" placeholder="公司、合约或资产名称" required autocomplete="off"></div><div class="field"><label for="watch-asset">资产类别</label><select id="watch-asset" name="asset_class">${Object.entries(assets).filter(([id]) => id !== 'all').map(([id,label]) => `<option value="${id}">${label}</option>`).join('')}</select></div><div class="field"><label for="watch-region">所属市场</label><select id="watch-region" name="region">${Object.entries(regions).map(([id,label]) => `<option value="${id}">${label}</option>`).join('')}</select></div><div class="field field-wide"><label for="watch-note">观察笔记</label><input id="watch-note" name="note" maxlength="500" placeholder="记录关注原因、催化因素或待验证的假设"></div><div class="form-submit"><button type="submit" class="button primary">${icon('plus')} 添加关注</button></div></form>${state.watchError ? `<div class="notice-error">${esc(state.watchError)}<button class="text-link" data-action="reload-watchlist">重新读取</button></div>` : ''}${toolbar()}${searchInfo()}${items.length ? `<div class="table-wrap"><table class="asset-table"><thead><tr><th>资产 / 代码</th><th>类别 / 市场</th><th>行情状态</th><th>观察笔记</th><th>关联信息</th><th><span class="sr-only">操作</span></th></tr></thead><tbody>${items.map(item => {
      const related = list(state.data?.clusters).filter(cluster => matchesAsset(cluster,item)).length;
      return `<tr><td><span class="asset-symbol">${esc(item.symbol)}</span><span class="asset-name">${esc(item.name)}</span></td><td>${esc(assets[item.asset_class] || item.asset_class)}<span class="asset-name">${esc(regions[item.region] || item.region)}</span></td><td><span class="no-quote">行情未接入</span></td><td class="asset-note">${esc(item.note || '尚未记录')}</td><td><button class="text-link" data-action="related" data-id="${esc(item.id)}">${related} 个候选主题 ${icon('arrow')}</button></td><td><button class="icon-button" data-action="delete-watch" data-id="${esc(item.id)}" aria-label="移除 ${esc(item.symbol)}">${icon('trash')}</button></td></tr>`;
    }).join('')}</tbody></table></div>` : empty(state.watchlist.length ? '没有符合筛选的关注资产' : '你的研究清单，从一个问题开始',state.watchlist.length ? '调整资产类别、市场或搜索词，查看已添加的关注对象。' : '使用上方表单添加资产，并写下你希望持续验证的研究假设。','', '', 'star')}`;
  }
  function render() {
    if (!state.data) return;
    const focused = document.activeElement;
    const focusedId = focused?.id;
    const selection = focused instanceof HTMLInputElement ? [focused.selectionStart,focused.selectionEnd] : null;
    const formValues = {};
    if (state.view === 'watchlist') for (const field of document.querySelectorAll('#watch-form input,#watch-form select')) formValues[field.name] = field.value;
    const renderers = {overview,themes,research,watchlist,sources};
    $('#page-content').innerHTML = renderers[state.view]();
    for (const [name,value] of Object.entries(formValues)) { const field = document.querySelector(`#watch-form [name="${name}"]`); if (field) field.value = value; }
    if (focusedId && focusedId !== 'global-search') { const target = document.getElementById(focusedId); if (target) { target.focus({preventScroll:true}); if (selection && typeof target.setSelectionRange === 'function' && target.type !== 'checkbox') { try { target.setSelectionRange(...selection); } catch (_) {} } } }
    $('#breadcrumb-current').textContent = views[state.view];
    $('#nav-topic-count').textContent = formatNumber(state.data.stats?.clusters);
    document.querySelectorAll('.nav-item').forEach(link => { const selected = link.dataset.view === state.view; link.classList.toggle('active',selected); if (selected) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current'); });
    $('#mode-label').textContent = state.mode === 'demo' ? 'DEMO 演示' : 'LIVE 公开数据';
    $('#mode-switch').classList.toggle('live',state.mode === 'live');
    $('#mode-switch').setAttribute('aria-label',state.mode === 'demo' ? '当前为演示模式，切换到真实公开数据' : '当前为真实公开数据，切换到演示模式');
    const notice = [];
    if (state.data.ingestion?.error) notice.push(`<div class="notice-error">${esc(state.data.ingestion.error)}</div>`);
    if (state.error) notice.push(`<div class="notice-error"><span>${esc(state.error)}</span><button class="text-link" data-action="reload">重新读取</button></div>`);
    if (state.data.ingestion?.running || state.syncing) notice.push(`<div class="sync-banner">${icon('refresh')} 正在采集公开订阅，保留来源链接并合并重复材料。完成后将自动更新。</div>`);
    if (state.mode === 'live' && !state.data.ingestion?.running && list(state.data.ingestion?.results).some(item => ['error','failed','blocked'].includes(item.status))) notice.push(`<div class="notice-error"><span>部分公开来源本次连接失败，真实数据可能不完整。请在数据来源页查看采集错误。</span><a class="text-link" href="#sources">查看详情 ${icon('arrow')}</a></div>`);
    $('#global-notice').innerHTML = notice.join('');
    document.title = `${views[state.view]} · 溯源 Signal Atlas`;
  }
  async function loadDashboard(mode, quiet = false) {
    const requestId = ++state.request;
    state.loading = true;
    try {
      const selected = mode === undefined ? state.mode : mode;
      const data = await api(`/api/dashboard${selected ? `?mode=${encodeURIComponent(selected)}` : ''}`);
      if (requestId !== state.request) return;
      if (!data || !Array.isArray(data.clusters)) throw new Error('服务返回的数据格式不完整，请稍后重试。');
      if (state.mode && state.mode !== data.mode) closeDrawer();
      state.data = data; state.mode = data.mode === 'demo' ? 'demo' : 'live'; state.error = null;
      render();
      if (data.ingestion?.running) schedulePoll();
      else if (state.syncing) { state.syncing = false; render(); }
    } catch (error) {
      if (requestId !== state.request) return;
      state.error = `读取数据失败：${error.message}`;
      if (state.data) render();
      else $('#page-content').innerHTML = empty('暂时无法连接工作台',state.error,'reload','重新连接','globe');
      if (!quiet) toast(state.error,true);
    } finally { if (requestId === state.request) state.loading = false; }
  }
  function schedulePoll() {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      const wasRunning = state.syncing || state.data?.ingestion?.running;
      await loadDashboard(state.modePinned ? state.mode : null,true);
      if (state.error && wasRunning) { schedulePoll(); return; }
      if (wasRunning && !state.data?.ingestion?.running) {
        state.syncing = false; render();
        const successful = list(state.data?.ingestion?.last_run_results).filter(item => ['ok','success','empty'].includes(item.status)).length;
        toast(successful ? `同步完成，${successful} 个来源已连接。${state.mode === 'demo' ? '可切换到真实数据查看。' : ''}` : '本次未连接成功；数据来源页保留了失败原因。',!successful);
      }
    },2200);
  }
  async function sync(sourceId) {
    if (state.syncing || state.data?.ingestion?.running) { toast('来源正在同步，请稍候。'); return; }
    state.syncing = true; render();
    try {
      await api('/api/ingest',{method:'POST',body:JSON.stringify(sourceId ? {source_ids:[sourceId]} : {})});
      if (state.data) state.data.ingestion = {...state.data.ingestion,running:true};
      render(); schedulePoll();
      toast('已开始同步公开来源，完成后会自动更新。');
    } catch (error) { state.syncing = false; render(); toast(`无法开始同步：${error.message}`,true); }
  }
  async function loadWatchlist() {
    try { const response = await api('/api/watchlist'); state.watchlist = list(response.items); state.watchError = null; }
    catch (error) { state.watchError = `关注列表读取失败：${error.message}`; }
    if (state.view === 'watchlist' && state.data) render();
  }
  function go(view) { location.hash = view; }
  function route() {
    const view = location.hash.slice(1).split('?')[0];
    state.view = views[view] ? view : 'overview';
    if (state.view !== 'themes') state.relatedItem = null;
    closeDrawer();
    $('#sidebar').classList.remove('open'); $('#mobile-menu').setAttribute('aria-expanded','false');
    render(); window.scrollTo({top:0,behavior:'auto'});
  }
  function openDrawer(id) {
    const cluster = list(state.data?.clusters).find(item => item.id === id);
    if (!cluster) return;
    state.drawer = id; state.previousFocus = document.activeElement;
    const verification = cluster.verification || {}, sentiment = cluster.sentiment || {}, scenario = cluster.scenario || {};
    const allArticles = list(cluster.articles), total = number(sentiment.positive) + number(sentiment.neutral) + number(sentiment.negative);
    const sourceIds = list(cluster.source_ids);
    const watch = list(scenario.watch);
    $('#drawer-root').innerHTML = `<div class="drawer-backdrop" data-action="close-drawer"></div><section class="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><header class="drawer-header"><span>TOPIC INTELLIGENCE / 主题证据与分析</span><button class="icon-button" data-action="close-drawer" aria-label="关闭详情">${icon('close')}</button></header><div class="drawer-body">${demoBanner()}<div class="topic-topline">${list(cluster.asset_classes).map(asset => `<span class="topic-category ${esc(asset)}">${esc(assets[asset] || asset)}</span>`).join('')}<span class="topic-time">${clusterDate(cluster,true)}</span></div><h2 id="drawer-title">${esc(cluster.title)}</h2><p class="drawer-intro">${esc(cluster.summary)}</p><div class="tags">${list(cluster.entities).map(entity => `<span class="tag">${esc(entity)}</span>`).join('')}${list(cluster.regions).map(region => `<span class="tag">${esc(regions[region] || region)}</span>`).join('')}</div><div class="detail-metrics"><div class="detail-metric"><strong>${formatNumber(cluster.unique_count)}</strong><span>去重后的内容</span></div><div class="detail-metric"><strong>${formatNumber(cluster.independent_sources)}</strong><span>独立来源主体</span></div><div class="detail-metric"><strong>${formatNumber(cluster.duplicate_count)}</strong><span>已合并的转载 / 重复</span></div></div><section class="drawer-section"><h3>${icon('shield')} 证据可追溯性</h3><div class="verification-box"><strong>${esc(verification.label || '待核验')}</strong><p>${esc(verification.explanation || '需打开原始材料进一步核对。')}</p><p>来源可追溯度：${formatNumber(verification.score)} / 100。该评分衡量来源和链接线索，不是真实性概率。</p><p class="evidence-note">${list(verification.limitations).map(esc).join('<br>')}</p></div><p>独立来源依据目录中的发布主体分组；相同内容的转载不增加独立证据。多个来源提到同一事件，并不代表逐项事实已经证实。</p></section><section class="drawer-section"><h3>${icon('layers')} 主张与支持材料</h3>${list(cluster.evidence).map(evidence => `<div class="evidence-item">${esc(evidence.claim)}<span>${esc(evidence.label || (evidence.status === 'needs_review' ? '存在差异，待核对原文' : '来源报告，待逐项核验'))}</span><div class="evidence-links">${list(evidence.supporting_article_ids).map(articleId => {
      const article = allArticles.find(item => item.id === articleId); const url = safeUrl(article?.url);
      return url && !cluster.is_demo ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(sourceName(article.source_id))} ↗</a>` : `<span>${esc(article ? sourceName(article.source_id) : '对应原文未提供')}${cluster.is_demo ? '（虚构）' : ''}</span>`;
    }).join('')}</div></div>`).join('') || '<p>暂无可拆分的主张。请根据下方原文自行复核。</p>'}</section>${list(cluster.conflicts).length ? `<section class="drawer-section"><h3>${icon('info')} 待核对差异</h3>${list(cluster.conflicts).map(conflict => `<div class="source-error">${esc(conflict.reason || conflict)}<p>${list(conflict.article_ids).map(id => {const article = allArticles.find(a => a.id === id); return article ? esc(article.title) + " · " + esc(sourceName(article.source_id)) : esc(id);}).join("<br>")}</p></div>`).join('')}</section>` : ''}<section class="drawer-section"><h3>${icon('trend')} 文本措辞情绪</h3><span class="sentiment-pill ${sentimentClass(sentiment.score)}">${esc(sentiment.label || '暂无样本')} · ${number(sentiment.score) > 0 ? '+' : ''}${formatNumber(sentiment.score)}</span><div class="sentiment-breakdown" role="img" aria-label="积极 ${number(sentiment.positive)} 条，中性 ${number(sentiment.neutral)} 条，消极 ${number(sentiment.negative)} 条">${['positive','neutral','negative'].map(tone => `<span class="bar-${tone}" style="width:${total ? number(sentiment[tone]) / total * 100 : 0}%"></span>`).join('')}</div><div class="sentiment-values"><span>积极 ${formatNumber(sentiment.positive)}</span><span>中性 ${formatNumber(sentiment.neutral)}</span><span>消极 ${formatNumber(sentiment.negative)}</span></div><p>${esc(sentiment.method || '基于标题与摘要中的情绪词计数，仅用于观察信息措辞。')}</p></section><section class="drawer-section"><h3>${icon('globe')} 条件情景与传导路径</h3><p>以下为需验证的条件推演，没有收益预测或发生概率。${esc(scenario.horizon)}</p><div class="scenario-grid">${[['bull','改善情景 · 若条件兑现'],['base','观察情景 · 等待基本面确认'],['bear','承压情景 · 若风险兑现']].map(([key,label]) => `<div class="scenario-card ${key}"><strong>${label}</strong><p>${esc(scenario[key] || '材料不足，暂无法构建该情景。')}</p></div>`).join('')}</div>${watch.length ? `<p><strong>接下来观察：</strong>${watch.map(esc).join('；')}</p>` : ''}<p>${list(scenario.limitations).map(esc).join('；')}</p></section><section class="drawer-section"><h3>${icon('book')} 原始材料与重复关系 <span class="count-label">${allArticles.length}</span></h3>${allArticles.map(article => {
      const original = allArticles.find(item => item.id === article.duplicate_of); const url = safeUrl(article.url);
      return `<article class="article-item"><div class="article-source"><span>${esc(sourceName(article.source_id))}</span><span>${articleDate(article)}</span></div><h4>${url && !article.is_demo ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(article.title)} ${icon('external')}</a>` : esc(article.title)}</h4>${article.is_demo ? '<p>虚构演示材料 · 不提供真实原文链接</p>' : ''}${article.summary ? `<p>${esc(article.summary)}</p>` : ''}${article.duplicate_of ? `<span class="duplicate-tag">已合并重复 → ${esc(original?.title || article.duplicate_of)}</span>` : ''}</article>`;
    }).join('')}</section><section class="drawer-section"><h3>${icon('shield')} 发布主体说明</h3>${sourceIds.map(sourceId => { const source = sourceById(sourceId); return `<p><strong>${esc(source.name)}</strong> · 独立主体 ${esc(source.independence_group || '未登记')}${source.is_demo ? ' · 虚构来源' : ''}</p>`;}).join('')}</section></div></section>`;
    document.querySelectorAll('.detail-drawer .drawer-section')[1]?.insertAdjacentHTML('afterend',briefingSection(cluster));
    document.body.classList.add('drawer-open');
    $('.detail-drawer [data-action="close-drawer"]').focus();
  }
  function closeDrawer() {
    if (!state.drawer) return;
    state.drawer = null; $('#drawer-root').innerHTML = ''; document.body.classList.remove('drawer-open');
    if (state.previousFocus && document.contains(state.previousFocus)) state.previousFocus.focus({preventScroll:true});
    state.previousFocus = null;
  }
  function briefingSection(cluster) {
    const briefing = cluster.briefing;
    if (!briefing) return '';
    const policy = briefing.policy;
    return `<section class="drawer-section"><h3>${icon('layers')} 组合解读与政策拆解</h3><p>${esc(briefing.independence_note)}</p>${list(briefing.points).map(point => {
      const url = safeUrl(point.url);
      return `<div class="evidence-item">${esc(point.text)}<div class="evidence-links">${url && !cluster.is_demo ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(point.source_name)} · 摘录出处 ↗</a>` : `<span>${esc(point.source_name)} · 演示摘录</span>`}</div></div>`;
    }).join('')}${policy ? `<div class="verification-box"><strong>政策阶段与适用边界</strong>${list(policy.stages).map(item => {
      const article = list(cluster.articles).find(article => article.id === item.article_id);
      return `<p>${esc(article?.title || '材料')}：${esc(item.stage)}</p>`;
    }).join('')}<p>生效日期：${esc(policy.effective_date)}</p><p>适用范围：${esc(policy.scope)}</p><p>需要验证的传导顺序：${list(policy.transmission).map(esc).join(' → ')}</p></div>` : ''}<p><strong>复核清单：</strong>${list(briefing.questions).map(esc).join('；')}</p><p class="evidence-note">${esc(briefing.method)}</p></section>`;
  }
  async function switchMode(mode) { state.modePinned = true; closeDrawer(); await loadDashboard(mode); }
  function clearSearch() { state.relatedItem = null; state.query = ''; $('#global-search').value = ''; render(); }
  function resetFilters() { state.relatedItem = null; state.asset = 'all'; state.region = 'all'; state.primary = false; state.query = ''; $('#global-search').value = ''; render(); }

  document.addEventListener('click',async event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'asset') { state.asset = button.dataset.id; render(); }
    else if (action === 'open-cluster') openDrawer(button.dataset.id);
    else if (action === 'close-drawer') closeDrawer();
    else if (action === 'sync') await sync();
    else if (action === 'sync-source') await sync(button.dataset.id);
    else if (action === 'switch-live') await switchMode('live');
    else if (action === 'reload') await loadDashboard();
    else if (action === 'reset-filters') resetFilters();
    else if (action === 'clear-search') clearSearch();
    else if (action === 'go-sources') go('sources');
    else if (action === 'reload-watchlist') await loadWatchlist();
    else if (action === 'related') { state.relatedItem = state.watchlist.find(item => item.id === button.dataset.id); state.query = ''; $('#global-search').value = ''; state.asset = 'all'; state.region = 'all'; state.primary = false; go('themes'); }
    else if (action === 'delete-watch') {
      button.disabled = true;
      try { await api(`/api/watchlist/${encodeURIComponent(button.dataset.id)}`,{method:'DELETE'}); await loadWatchlist(); toast('已从关注清单移除。'); }
      catch (error) { button.disabled = false; toast(`移除失败：${error.message}`,true); }
    }
  });
  document.addEventListener('change',event => {
    if (event.target.id === 'region-filter') { state.region = event.target.value; render(); }
    else if (event.target.id === 'primary-filter') { state.primary = event.target.checked; render(); }
    else if (event.target.matches('.sort-select')) { state.sort = event.target.value; render(); }
  });
  document.addEventListener('submit',async event => {
    if (event.target.id !== 'watch-form') return;
    event.preventDefault();
    const form = event.target, submit = form.querySelector('button[type="submit"]');
    const values = Object.fromEntries(new FormData(form));
    for (const key of Object.keys(values)) values[key] = string(values[key]).trim();
    if (!values.symbol || !values.name) { toast('请填写资产代码和名称。',true); return; }
    submit.disabled = true;
    try { await api('/api/watchlist',{method:'POST',body:JSON.stringify(values)}); form.reset(); await loadWatchlist(); toast('资产已加入关注清单。'); }
    catch (error) { submit.disabled = false; toast(`添加失败：${error.message}`,true); }
  });
  $('#global-search').addEventListener('input',event => {
    state.relatedItem = null;
    state.query = event.target.value.trim(); clearTimeout(searchTimer); searchTimer = setTimeout(render,180);
  });
  $('#mode-switch').addEventListener('click',() => switchMode(state.mode === 'demo' ? 'live' : 'demo'));
  $('#mobile-menu').addEventListener('click',() => { const opened = $('#sidebar').classList.toggle('open'); $('#mobile-menu').setAttribute('aria-expanded',String(opened)); });
  document.addEventListener('click',event => { if ($('#sidebar').classList.contains('open') && !event.target.closest('#sidebar,#mobile-menu')) { $('#sidebar').classList.remove('open'); $('#mobile-menu').setAttribute('aria-expanded','false'); } });
  document.addEventListener('keydown',event => {
    if (event.key === 'Escape') { closeDrawer(); $('#sidebar').classList.remove('open'); $('#mobile-menu').setAttribute('aria-expanded','false'); }
    if (event.key === '/' && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName) && !state.drawer) { event.preventDefault(); $('#global-search').focus(); }
    if (event.key === 'Tab' && state.drawer) {
      const focusable = Array.from(document.querySelectorAll('.detail-drawer a[href],.detail-drawer button:not([disabled]),.detail-drawer input,.detail-drawer select,[tabindex="0"]')).filter(element => element.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  window.addEventListener('hashchange',route);
  const refreshVisible = () => { if (!document.hidden && !state.loading && !state.syncing) loadDashboard(state.modePinned ? state.mode : null,true); };
  setInterval(refreshVisible,45000);
  document.addEventListener('visibilitychange',refreshVisible);
  route();
  Promise.allSettled([loadDashboard(null),loadWatchlist()]);
})();
