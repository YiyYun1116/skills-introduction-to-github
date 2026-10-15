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
    if 'policy' in kinds:
        statements=[]
        for article in originals:
            if article.get('kind') != 'policy':
                continue
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
    filing=None
    if 'filing' in kinds:
        items=[]
        for article in originals:
            if article.get('kind') != 'filing':
                continue
            items.append({
                'article_id':article['id'],
                'company_name':article.get('company_name') or '',
                'company_cik':article.get('company_cik') or '',
                'form':article.get('filing_form') or '',
                'accession_number':article.get('accession_number') or '',
                'filing_date':article.get('filing_date') or '',
                'accepted_at':article.get('accepted_at') or (article.get('announced_at') if article.get('date_basis') == 'filing_accepted' else '') or '',
                'reporting_period':article.get('reporting_period') or article.get('report_period') or article.get('period_of_report') or '',
                'status':'申报索引线索，报告正文及财务数字尚未核对',
            })
        filing={
            'items':items,
            'financial_metrics':{'status':'not_extracted','explanation':'仅有申报索引/摘要，尚未从申报正文提取或核对营收、利润、现金流等财务指标。'},
            'checks':['公司名称、CIK、申报表单及 accession number 是否一一对应？',
                      '报告期末、申报日期与 SEC 受理时间是否区分，是否存在修订或重述？',
                      '原始报表中的金额、币种、单位、合并范围及可比期间是否一致？',
                      '经营现金流、盈利质量、审计意见和风险因素是否支持管理层表述？'],
            'limitations':['仅接入申报索引与摘要，未解析完整财务报表或 XBRL 数据。',
                           '受理时间表示文件进入 SEC 系统，不是政策生效时间，也不能推断所属季度。',
                           'SEC 收录不等于认可财务结论；缺少原文核对时不判断公司业绩或股价方向。'],
        }
        questions+=filing['checks']
    if 'research' in kinds:
        questions+=['研究是否经过同行评审，方法和样本能否复核？','结论能否用于当前市场，是否存在样本外失效？']
    return {'method':'原文摘要摘录与规则清单；并非全文理解或自动事实认证。',
            'points':points,'conflicts':cluster.get('conflicts',[]),'questions':questions,'policy':policy,'filing':filing,
            'independence_note':f"{len(originals)} 条去重材料、{cluster['independent_sources']} 个来源主体；每条摘录单独保留出处。"}
