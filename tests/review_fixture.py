"""Deterministic review output for isolated tests; no model/network inference."""
import json


def synthetic_review_result(evidence, options):
    refs = [x['id'] for x in evidence.get('segments', []) + evidence.get('slides', [])]
    if not refs:
        raise ValueError('Synthetic review needs actual fixture sources')
    examples = {
        'concept': ('供给与均衡', '什么决定市场均衡？', '【合成测试服务】供需曲线交点是均衡；请核对实际课堂材料。'),
        'derivation': ('均衡条件', '如何列出均衡条件？', '【合成测试服务】先令 $Q_d=Q_s$。没有完整条件时不能继续计算。'),
        'example': ('课堂例题核对', '这份记录给出了哪些例题条件？', '【合成测试服务】逐项对照本卡来源中的条件，不补造老师没有给出的数值。'),
        'pitfall': ('供给变化与供给量变化', '沿曲线移动与整条曲线移动有什么区别？', '【合成测试服务】先核对老师描述的是价格变化还是其他条件变化。'),
    }
    cards = []
    for kind in options.get('card_types', examples):
        title, front, back = examples[kind]
        cards.append({'type': kind, 'title': title, 'front': front, 'back': back,
            'supplement': '合成测试内容，仅用于检验应用流程。',
            'english': 'Market equilibrium occurs where quantity demanded equals quantity supplied.' if options.get('english') else '',
            'english_meaning': '市场均衡发生在需求量等于供给量时。' if options.get('english') else '',
            'source_ids': refs, 'missing': ['完整推导须回看所列课堂材料，本测试不补造。'] if kind == 'derivation' else []})
    notes = [{'section': section, 'title': title, 'body': body, 'supplement': '', 'source_ids': refs, 'missing': []}
             for section, title, body in (
                 ('structure', '供需与均衡', '【合成测试服务】供给 → 需求 → 均衡。公式 $Q_d=Q_s$；中英 Supply 中文 😀。'),
                 ('focus', '重点核对', '【合成测试服务】优先查看你标记的原文和课件。'),
                 ('questions', '未解决疑问', '【合成测试服务】没懂的标记由你确认理解后再关闭。'),
                 ('slides', '关联材料', '【合成测试服务】本条只引用本批实际提供的材料。'))]
    return json.dumps({'schema_version': 1, 'cards': cards, 'notes': notes}, ensure_ascii=False)
