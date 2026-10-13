"""Extractive topic briefing. Every quoted point retains its article identity."""
import re


def build_briefing(cluster):
    originals = [a for a in cluster['articles'] if not a.get('duplicate_of')]
    points=[]
    for article in originals[:6]:
        text=article.get('summary') or article['title']
        sentences=re.split(r'(?<=[。！？])\s*|(?<=[.!?])\s+(?=[A-Z])',text)
        quote=next((s.strip() for s in sentences if len(s.strip())>20),text)
        points.append({'text':quote[:500],'article_id':article['id'],'source_name':article.get('source_name',''),
                       'url':article['url'],'type':'source_excerpt'})
    kinds={a.get('kind') for a in originals}
    questions=['这些材料是否讨论同一时间、地区和统计口径？','是否存在来自同一原始消息的转载或引用？']
    policy=None
    if 'policy' in kinds or 'filing' in kinds:
        statements=[]
        for article in originals:
            text=article['title']+' '+article.get('summary','')
            lower=text.casefold()
            if re.search(r'\b(proposal|proposed|consultation|draft)\b|征求意见|草案|拟议',lower):
                stage='包含提案/征求意见措辞，不能视作已生效'
            elif re.search(r'\b(final rule|adopts?|effective)\b|正式实施|生效|通过',lower):
                stage='包含正式规则或生效措辞，需核对原文和具体日期'
            else:
                stage='摘要无法确认法律效力和实施阶段'
            statements.append({'article_id':article['id'],'stage':stage})
        policy={'stages':statements,'effective_date':'未从完整原文确认','scope':'需核对正式文件的适用地域、对象和例外',
                'transmission':['原文与实施条件','行业需求 / 收入 / 成本','公司现金流与估值；需补充财务及行情数据']}
        questions+=['发布日期是否不同于生效日？','政策适用范围和具体公司业务是否匹配？']
    if 'research' in kinds:
        questions+=['研究是否经过同行评审，方法和样本能否复核？','结论能否用于当前市场，是否存在样本外失效？']
    return {'method':'原文摘要摘录与规则清单；并非全文理解或自动事实认证。',
            'points':points,'conflicts':cluster.get('conflicts',[]),'questions':questions,'policy':policy,
            'independence_note':f"{len(originals)} 条去重材料、{cluster['independent_sources']} 个来源主体；每条摘录单独保留出处。"}
