#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.0（9 段）→ v4.0（10 段）：新增 S10 请求生成，其余 9 段原样不动。

依据（本批 1008 条，来自归因自带的 component_role）：
  request_generation 265 条（26.3%），现在散在 S1/S2/S4/S6/S7/S8/S9 七段里，
  没有独立归属；它是一条独立责任域（把计划落成请求/代码/spec），修法与
  「工具执行失败」「脚本执行失败」都不同，因此单独成段。

不动的部分：
  S1 取数规划 / S2 检索规划 / S3 结构化取数 / S4 本地计算 / S5 网页检索 /
  S6 客服知识库 / S7 记忆 / S8 能力装载 / S9 终稿生成 的叶子、include/exclude、
  标签、判定卡全部保留（α=0.918 的那套）。

用法：python3 bin/extend_v4_request_stage.py [--dry]
"""
import json, os, sys, glob, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAX = os.path.join(ROOT, 'taxonomy')

# S10 叶子：(id 尾号, 名称, 机制, 标签, definition, include, exclude, plain)
LEAVES = [
    ('M1', '请求为空', ['失败'],
     '生成的调用参数为空或必填项全空，请求被网关或工具直接拦下，没有真正执行。',
     'arguments 为 {}、query 为空串、只给工具名不给参数。',
     '参数有值但类型或字段名不合契约 → `S10.M2.8`；被校验拒绝 → `S10.M3.2`。',
     '空的请求发出去，直接被拦。'),
    ('M1', '未生成请求', ['缺失'],
     '计划里点名了某个动作，请求生成阶段没有产出对应的调用，这一条静默消失。',
     '计划三条查询只生成两条调用且无说明；某工具本轮从未被请求。',
     '整批计划都没下发（属计划层）→ `S1.M1.3`；生成了但内容空 → `S10.M1.1`。',
     '该发的请求没写出来。'),
    ('M2', '请求照抄原句', ['缺失'],
     '把用户整句问话当作查询文本下发，没有拆成字段、条件与期间。',
     'query 等于用户原问，没有指标名、没有日期、没有标的代码。',
     '拆了字段但字段选错 → `S10.M2.4`；条件过宽 → `S10.M2.5`。',
     '把问句原样当查询发出去。'),
    ('M2', '请求标的写错', ['实体'],
     '请求里的标的代码、名称或市场域写错：代码拼错、市场写反、名称与代码不匹配。',
     '请求带 NVDA.N 而不是 NVDA、港股代码写成 A 股格式、名称对但代码指向另一只。',
     '计划层就认错标的 → `S1.M2.2`；标的对但取回别的 → `S3.M2.1`。',
     '请求里的代码不是用户说的那只。'),
    ('M2', '请求期间写错', ['时效'],
     '请求里的日期、报告期或区间端点写错，与计划要的期间不一致。',
     '「近一年」起始日算错、报告期取成最新一期、区间两端写成同一天。',
     '计划层就锚错 → `S1.M2.4`；期间对但取回旧值 → `S3.M4.7`。',
     '请求的日期不是要的那个。'),
    ('M2', '请求字段取近似', ['口径'],
     '请求要 A 指标，实际下发的字段是含 A 的近似或派生指标，口径已经变了。',
     '要「归母净利」请求成「净利润」、要 TTM 请求成最新单季、要前复权请求成不复权。',
     '字段对但返回缺该字段 → `S3.M4.1`；取回后口径理解错 → `S9.M5.4`。',
     '要 A 却按 B 去取。'),
    ('M2', '请求过滤条件写错', ['缺失'],
     '请求的筛选条件方向错、范围过窄或过宽，或几个条件互相抵消。',
     '「高股息」写成低于阈值、三项 high 叠加导致零行、时间条件与标的条件冲突。',
     '完全没写条件（属计划层）→ `S1.M2.7`；条件对但执行零行 → `S3.M3.3`。',
     '条件写反，或者叠到没人满足。'),
    ('M2', '请求排序分页写错', ['缺失'],
     '请求里的排序字段、方向、TopN 或分页参数写错。',
     '要「从低到高」写成降序、要前二十却取全量、分页参数指向第二页。',
     '排序对但取回条数少 → `S3.M3.8`。',
     '排序或条数参数写歪。'),
    ('M2', '工具选错', ['失败'],
     '请求指向的工具不在可用清单里，或选了一个做不到这件事的工具。',
     '把技能名当函数名调用、调用清单外工具、用检索工具做结构化筛选。',
     '工具对但参数错 → `S10.M2.8`；计划层就选错 → `S1.M2.1`。',
     '调了一个不存在或不合适的工具。'),
    ('M2', '请求结构不合契约', ['失败'],
     '请求的形式不合工具契约：参数类型错、字段名是契约外的、多个 JSON 拼接、必填项缺失。',
     '整数参数传字符串、用了契约没有的顶层字段、两个 query 对象拼成一个 arguments 串。',
     '结构对但内容指错对象 → `S10.M2.2`；被校验器拒绝 → `S10.M3.2`。',
     '请求形状不对，工具不认。'),
    ('M2', '请求重复', ['缺失'],
     '同一请求在一轮里被重复下发，或把已成功的请求原样重发。',
     '连续两次相同 query；报错后不改参数直接重试同一条。',
     '计划层重复 → `S1.M2.11`。',
     '同一条请求发了两遍。'),
    ('M3', '生成物不可执行', ['失败'],
     '生成的脚本或调用一跑就炸：语法错误、引用不存在的列/键、括号不配对。',
     'SyntaxError、KeyError、NameError、未闭合字符串，脚本非零退出。',
     '能跑但算错 → `S10.M5.1`；执行前被校验拦下 → `S10.M3.2`。',
     '写出来的代码根本跑不起来。'),
    ('M3', '请求被校验拒绝', ['失败'],
     '请求或渲染 spec 在执行前被契约校验拒绝：字段不被支持、必填缺项、鉴权不通过。',
     'spec 校验报字段越界、工具返回参数不支持、鉴权失败无回执。',
     '请求内容指错对象 → `S10.M2.2`；工具已执行但报错 → `S3.M3.2`。',
     '还没执行就被校验挡回来。'),
    ('M3', '生成物被截断', ['缺失'],
     '生成的参数、代码或 spec 被截断，只交付了一部分。',
     '参数列表写到一半、脚本最后一段缺失、spec 数组不闭合。',
     '计划层被截断 → `S1.M3.1`；执行输出被截断 → `S3.M3.8`。',
     '生成到一半就断了。'),
    ('M5', '生成代码计算逻辑错', ['单位'],
     '生成物本身能跑，但里面的换算、聚合或标签逻辑写错。',
     '万手→手换算系数取错、聚合窗口把当日算进去、标签与数值错位。',
     '生成物跑不起来 → `S10.M3.1`；取回后再算错 → `S9.M5.1`。',
     '代码能跑，算式是错的。'),
    ('M6', '生成物写死结论', ['编造'],
     '把结论、数值或判断硬编码进生成的脚本或 spec，冒充计算结果。',
     '脚本里直接写死目标值再打印「结果」；spec 里写死一段没有数据来源的文案。',
     '有数据但算错 → `S10.M5.1`；终稿阶段编造 → `S9.M6.4`。',
     '把答案写死在代码里，跑一遍假装算出来。'),
    ('M7', '生成物缺项', ['缺失'],
     '生成的请求或 spec 明确少了必需的部分：缺一个指标、缺一段序列、缺图例。',
     'spec 有数据序列但无 axis、脚本只写取数不写渲染、请求只带部分指标。',
     '生成物整体为空 → `S10.M1.1`；计划层就漏维度 → `S1.M2.6`。',
     '生成的东西缺一块，跑起来也是残的。'),
]

REQUEST_TOOLS = {'planner-finquery', 'planner-search', 'shell', 'CodeInterpreter', 'web_search',
                 'web_fetch', 'customer_services', 'aime_memory_read', 'aime_memory_write',
                 'view_skill/read_file', 'client_action', 'display', 'stock_ner_parse'}


def load(n):
    return json.load(open(os.path.join(TAX, n), encoding='utf-8'))


def main():
    dry = '--dry' in sys.argv
    inv, tags, comps = load('inventory.json'), load('leaf_tags.json'), load('components.json')
    stages, c2l, disc = load('stages.json'), load('comp2l1.json'), load('discriminators.json')

    if 'S10' in inv['l1']:
        sys.exit('S10 已存在，本脚本只跑一次')

    # 1. 段定义
    inv['l1']['S10'] = {
        'name': '请求生成',
        'definition': '把计划落成可调用的请求、代码或渲染 spec：定参数、字段、结构、契约。'
                      '失效多为参数缺失/类型错、字段取近似、工具选错、生成物不可执行。',
    }
    stages['S10'] = {
        'name': '请求生成',
        'definition': '把计划落成可调用的请求、代码或渲染 spec：定参数、字段、结构、契约。'
                      '失效多为参数缺失/类型错、字段取近似、工具选错、生成物不可执行。',
        'components': sorted({e['tool'] for e in comps.values()
                             if e.get('role') == 'request_generation' and e['tool'] in REQUEST_TOOLS}),
    }

    # 2. 真正由 S10 承接的工具集合（以 components.json 里带 request_generation 的 raw 为准）
    req_tools = sorted({e['tool'] for e in comps.values()
                        if e.get('role') == 'request_generation' and e['tool'] in REQUEST_TOOLS})

    # 3. 叶子（编号按机制内顺序）
    seq = collections.Counter()
    for mei, name, tg, definition, include, exclude, plain in LEAVES:
        seq[mei] += 1
        nid = f'S10.{mei}.{seq[mei]}'
        inv['nodes'].append({'id': nid, 'l1': 'S10', 'l2': mei, 'name': name,
                             'definition': definition + '（本叶仅收请求生成环节产生的缺陷）',
                             'include': include, 'exclude': exclude,
                             'components': req_tools, 'plain': plain})
        tags[nid] = tg

    # 3. component → 阶段：request_generation 的 raw 走 S10，其余沿用旧映射
    n_req = 0
    for raw, e in comps.items():
        tool = e['tool']
        role = e.get('role', '')
        if role == 'request_generation' and tool in REQUEST_TOOLS:
            e['stage'] = 'S10'
            n_req += 1
        else:
            e['stage'] = c2l.get(tool, 'S3')
            e.setdefault('role', '')
    # 4. comp2l1 用 "S1+S10" 表示跨段工具
    span = collections.defaultdict(set)
    for raw, e in comps.items():
        span[e['tool']].add(e['stage'])
    for tool, sts in span.items():
        c2l[tool] = '+'.join(sorted(sts)) if len(sts) > 1 else next(iter(sts))

    print(f"新增 S10：{len(LEAVES)} 叶，{n_req} 个 raw 组件改由 S10 承接")
    print("跨段工具:", {t: s for t, s in sorted(c2l.items()) if '+' in s})
    print("段数:", len(inv['l1']), "叶子:", len(inv['nodes']))
    if dry:
        print('dry run')
        return
    for name, obj in [('inventory.json', inv), ('leaf_tags.json', tags), ('stages.json', stages),
                      ('comp2l1.json', c2l), ('components.json', comps), ('discriminators.json', disc)]:
        json.dump(obj, open(os.path.join(TAX, name), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('written')


if __name__ == '__main__':
    main()
