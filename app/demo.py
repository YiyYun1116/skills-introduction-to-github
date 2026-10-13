"""Synthetic fixtures only. Never attributed to real people or publishers."""
from datetime import datetime, timedelta, timezone

DEMO_SOURCES = [
    {'id':'demo-policy','name':'演示 · 政策原始发布源 A','tier':'primary','kind':'official','independence_group':'demo-a'},
    {'id':'demo-data','name':'演示 · 独立统计源 B','tier':'primary','kind':'official','independence_group':'demo-b'},
    {'id':'demo-news','name':'演示 · 新闻转载源 C','tier':'secondary','kind':'news','independence_group':'demo-c'},
    {'id':'demo-research','name':'演示 · 学术研究源 D','tier':'academic','kind':'research','independence_group':'demo-d'},
]
for source in DEMO_SOURCES:
    source.update(url='https://example.org', feed_url=None, region='GLOBAL', language='zh',
                  asset_classes=['macro'], description='虚构测试来源，仅用于体验功能，不构成真实证据。', enabled=False, is_demo=True)


def demo_articles():
    # Fixed dates prevent fixtures masquerading as fresh news.
    base = datetime(2026, 1, 15, 9, tzinfo=timezone.utc)
    topics = [
        ('rates','利率政策与资产定价','演示情景：通胀回落，利率路径仍需等待数据确认',
         '虚构材料：假设通胀降温，但就业仍具韧性。关注后续通胀、工资和政策声明；降息预期并不等于实际降息。',
         ['macro','equity','futures'],['US','GLOBAL'],['利率','债券'],['通胀','货币政策'],'policy'),
        ('compute','算力投资与半导体','演示情景：算力需求增长，资本开支与盈利兑现仍有分歧',
         '虚构材料：假设云端算力需求增长，供应链受益程度取决于订单、交付和毛利率。不能从行业需求直接推出某只股票上涨。',
         ['equity'],['US','CN','HK'],['半导体','云计算'],['人工智能','资本开支'],'news'),
        ('energy','能源供给与商品期货','演示情景：供给扰动增加，原油库存成为观察重点',
         '虚构材料：假设部分供给受限，但需求预测下调。期货方向还受库存、期限结构和汇率影响。',
         ['futures','macro'],['GLOBAL'],['原油','能源'],['供给','库存'],'news'),
        ('crypto','数字资产监管与流动性','演示情景：数字资产规则更清晰，资金流入仍待验证',
         '虚构材料：假设监管披露要求提高，合规成本和投资者信心可能同时变化。需要观察实际资金流、交易深度及监管原文。',
         ['crypto','equity'],['US','EU'],['数字资产','比特币'],['监管','流动性'],'policy'),
        ('volatility','波动率与期权风险','演示研究：隐含波动率如何反映事件风险',
         '虚构论文摘要：讨论事件窗口、波动率曲面与尾部风险的关系。未接入实时期权链，因此不能据此给出权利金或交易信号。',
         ['options','equity'],['GLOBAL'],['期权','波动率'],['尾部风险','期权定价'],'research'),
        ('china','产业政策与中国市场','演示情景：产业支持政策出台，执行与财务影响待观察',
         '虚构材料：假设产业政策支持先进制造。影响应逐层验证政策适用范围、资金落地、公司订单及现金流，不能将政策标题视作业绩承诺。',
         ['equity','macro'],['CN','HK'],['先进制造','产业政策'],['产业升级','财政政策'],'policy'),
        ('climate','转型风险与资产配置','演示研究：能源转型风险如何传导至企业估值',
         '虚构论文摘要：以条件假设分析碳成本、能源结构与企业现金流。需要复核方法、样本期和利益冲突；论文结论不代表学术共识。',
         ['equity','futures'],['EU','GLOBAL'],['新能源','碳市场'],['能源转型','估值'],'research'),
    ]
    items=[]
    for index,(key,topic,title,summary,assets,regions,entities,tags,kind) in enumerate(topics):
        source_id = 'demo-research' if kind=='research' else 'demo-policy'
        item=dict(id=f'demo-{key}-1', title=title, summary=summary, url=f'https://example.org/demo/{key}/1',
                  source_id=source_id, published_at=(base-timedelta(hours=index*3)).isoformat(),
                  ingested_at=base.isoformat(), language='zh', asset_classes=assets, regions=regions,
                  entities=entities,tags=tags,topic_key=key,topic=topic,kind=kind,is_demo=True)
        items.append(item)
        if kind!='research':
            items.append(dict(item,id=f'demo-{key}-2',source_id='demo-data',url=f'https://example.org/demo/{key}/2',
                              title=f'演示补充：{topic}的独立观察与限制',summary=f'虚构独立观察：{summary}'))
            if index<4:
                items.append(dict(item,id=f'demo-{key}-copy',source_id='demo-news',url=f'https://example.org/demo/{key}/copy'))
    return items
