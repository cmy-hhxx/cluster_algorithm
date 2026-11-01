#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.0（9 段 = 工具分组）→ v4.0（5 段 = 归因自己的 component_role 轴）。

为什么要改：2026-11 这批归因输出自带 component_family / component_role 两列，
上游的责任口径已经从「哪个工具」换成「哪个阶段」：
  planning / request_generation / tool / script / delivery
旧 9 段把 265 条 request_generation 摊在 7 个段里，看不到「请求生成一共坏在哪」。

新轴（5 段）：
  S1 规划        planning
  S2 请求生成    request_generation
  S3 工具执行    tool（+ finquery 取数技能脚本：责任域优先，仍算取数）
  S4 脚本执行    script
  S5 答案合成    delivery

叶子来源：
  - S1/S2 为新写（旧分类法没有「请求生成」这一段，规划的叶子也按工具拆过）
  - S3/S4/S5 从旧叶子转写：保留 definition/include/exclude/plain 原文，
    id 重排、成分按新段裁剪；只在段归属变化时改括注。

用法：python3 bin/migrate_v4_role_axis.py [--dry]
"""
import json, os, re, sys, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAX = os.path.join(ROOT, 'taxonomy')


def load(name):
    return json.load(open(os.path.join(TAX, name), encoding='utf-8'))


def dump(obj, name):
    json.dump(obj, open(os.path.join(TAX, name), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 新 5 段
STAGES = collections.OrderedDict([
    ('S1', {'name': '规划',
            'definition': '决定做什么：把问题拆成要查的子问题，选工具、定标的与期间、排执行顺序。'
                          '失效多为意图判错、标的/期间设计错、漏步骤、空计划。',
            'components': ['planner-finquery', 'planner-search']}),
    ('S2', {'name': '请求生成',
            'definition': '把计划落成可调用的请求、代码或渲染 spec：定参数、字段、结构、契约。'
                          '失效多为参数缺失/类型错、字段取近似、工具选错、生成物不可执行。',
            'components': ['shell', 'CodeInterpreter', 'web_search', 'web_fetch', 'customer_services',
                           'aime_memory_read', 'aime_memory_write', 'view_skill/read_file',
                           'client_action', 'display', 'stock_ner_parse']}),
    ('S3', {'name': '工具执行',
            'definition': '外部能力真正执行并返回：取数服务与技能脚本、网页检索与抓取、客服知识库、记忆、'
                          '实体识别、持仓查询、图像理解。失效多为报错/零行/截断/字段丢失/口径错。',
            'components': ['finquery', 'web_search', 'web_fetch', 'customer_services', 'CustomerServiceFAQ',
                           'aime_memory_read', 'get_user_context', 'stock_ner_parse',
                           'query_user_positions', 'watchlist_manager', 'understand_image',
                           'CodeInterpreter', 'shell']}),
    ('S4', {'name': '脚本执行',
            'definition': '沙箱里的脚本真正跑起来并产出文件/图表：shell 内联脚本、可视化 workflow。'
                          '失效多为语法错、报错退出、产物空白或截断。',
            'components': ['shell', 'CodeInterpreter', 'display', 'shell_poll']}),
    ('S5', {'name': '答案合成',
            'definition': '把已有材料组织成最终答复与引用：取值、换算、口径、论断、呈现。'
                          '失效多为编造、口径错、引用错挂、答非所问。',
            'components': ['答案合成', '中间推理处理']}),
])

L2 = None  # 沿用旧 l2

# ---------------------------------------------------------------- S1/S2 新叶子
# kind: new 表示全新；restage 表示沿用旧文本但换段
NEW = {
    'S1': [
        ('S1.M1.1', '零查询计划', ['缺失'],
         '面对必须取数或检索才能回答的问题，规划输出空计划或判定「无需外部材料」，本轮没有任何查询项下发。',
         '计划为空数组、明确写「无需检索/直接回答」、只给一句承诺而无动作；下游只能用旧上下文或常识作答。',
         '有查询项但没有任何调用 → `S1.M1.3`；有查询项但请求没被生成出来 → `S2.M1.2`。',
         '该去查，规划却没派活。'),
        ('S1.M1.2', '追问后未重新规划', ['缺失'],
         '用户补充条件或纠正口径后，规划没有基于新信息重出计划，仍沿用上一轮的旧计划。',
         '多轮会话里用户补齐标的/期间/口径后，本轮计划与上一轮逐字相同且没有新增或修改的查询项。',
         '重出计划但把新条件写错 → `S1.M2.1`；重出计划但没下发 → `S1.M1.3`。',
         '用户又补了一句，计划却原地不动。'),
        ('S1.M1.3', '计划未下发', ['失败'],
         '规划已经产出可执行的查询项，链路没有把它变成任何实际调用，计划停在纸面。',
         '计划里有 N 条查询而实际调用 0 条，或只调了计划之外的其他工具。',
         '调用了但报错 → `S2.M3.1` 或 `S3` 下的执行类叶子；计划本身是空的 → `S1.M1.1`。',
         '计划写好了，没人去执行。'),
        ('S1.M2.1', '取数意图判错', ['缺失'],
         '把不需要结构化取数的问题（咨询、解释、预测、主观判断、闲聊）当成取数任务派下去，或该取数的只做了检索。',
         '问句里没有可取字段或期间，仍生成取数计划；或问句明确要某指标数值却只安排网页检索。',
         '计划要取数且字段对，只是请求写歪 → `S2.M2.1`–`S2.M2.5`；检索与取数都没做 → `S1.M1.1`。',
         '该报价，派成了选股。'),
        ('S1.M2.2', '检索意图判错', ['缺失'],
         '该走网页检索的场景没有安排检索（如政策原文、新闻事件、入口步骤），或该取数的却排了检索。',
         '问句问的是公告/新闻/操作路径，计划里没有任何检索项；或问的是行情数值却只排检索。',
         '排了检索但检索词写歪 → `S2.M2.1`–`S2.M2.4`；完全没排任何动作 → `S1.M1.1`。',
         '该搜的没搜，或者该取数的跑去搜。'),
        ('S1.M2.3', '标的认错', ['实体'],
         '计划里把要查的标的、板块或市场域认错：名称歧义未消解、代码与市场错配、把同类股当成本股。',
         '用户说「英伟达」计划落到 NVDA 以外的代码；港股标的排到美股库；板块名落到同名的另一只股票。',
         '标的在计划里对，但请求里写错 → `S2.M2.2`；工具返回了别的标的的数据 → `S3` 的映射类叶子。',
         '要查 A，计划里去查了 B。'),
        ('S1.M2.4', '期间锚点错', ['时效'],
         '计划把时间窗、报告期或「最新」的锚点设计错：相对时间没落到绝对日期、报告期取错、区间端点错。',
         '「上季度」计划成最新季、「近一年」计划成自然年、财报口径计划成单季数据。',
         '期间在计划里对，请求里写错 → `S2.M2.3`；取回后把旧值当现值 → `S5` 的时效类叶子。',
         '时间口径从计划这一步就偏了。'),
        ('S1.M2.5', '计划漏步骤', ['缺失'],
         '计划缺少完成任务必需的步骤：漏预取基期、漏核对口径、漏加载技能、漏补一问的后续动作。',
         '求同比却没计划取上年同期；要求按技能流程核对却没计划装载技能；多步任务只排了第一步。',
         '步骤排了但执行失败 → `S3`/`S4`；根本没排任何步骤 → `S1.M1.1`。',
         '少排了一步，后面全空。'),
        ('S1.M2.6', '计划重复', ['缺失'],
         '同一意图被拆成多条重复或互相覆盖的查询，或把已经取到的数据又排一遍。',
         '一轮里对同一字段连排 3 次相同查询；自然语言原句与结构化字段各排一条。',
         '两条查询语义不同只是写得粗 → `S1.M2.7`；请求阶段重复下发 → `S2.M2.9`。',
         '同一件事排了两遍。'),
        ('S1.M2.7', '计划范围过泛', ['缺失'],
         '计划没有任何筛选条件或条件过松，等于把全市场全时段的数据都拉回来。',
         '问「高股息龙头」计划里只有「取全部 A 股股息率」，无行业/市值/时间约束。',
         '条件写了但过窄导致零行 → `S2.M2.5`；条件写错方向 → `S1.M2.1`。',
         '没设条件，等于全量拉。'),
        ('S1.M2.8', '计划漏维度', ['缺失'],
         '计划漏掉用户明确要求的维度或子问题，只覆盖其中一部分。',
         '用户要「营收、净利、毛利率三项对比」，计划只排了营收；多标的比较只排了一个标的。',
         '维度排了但请求里丢了 → `S2.M2.8`；取回后没写进答案 → `S5` 的漏交付类叶子。',
         '要三个维度，只排了一个。'),
        ('S1.M3.1', '计划被截断', ['失败'],
         '计划在输出阶段被截断或结构损坏，无法据此执行。',
         '计划 JSON 不闭合、最后一条查询写到一半、字段名被截。',
         '计划完整但内容空 → `S1.M1.1`；计划完整但步骤少 → `S1.M2.5`。',
         '计划写到一半断了。'),
        ('S1.M3.2', '计划未产出', ['失败'],
         '规划调用本身返回空或解析失败，没有可用的计划。',
         '规划接口返回空对象、解析报错、只有一句自然语言而无结构。',
         '计划为空但是主动判定「无需取数」 → `S1.M1.1`。',
         '规划这一步没交出东西。'),
        ('S1.M6.1', '未查即结论', ['编造'],
         '计划层面就已经放弃取数，直接凭上下文或常识给出结论性判断。',
         '计划里写「数据不可得，直接回答」并给出具体数值或结论；没有任何查询却输出带数字的答复。',
         '查了但没查到时编造 → `S5` 的编造类叶子。',
         '没查就敢下结论。'),
    ],
    'S2': [
        ('S2.M1.1', '请求为空', ['失败'],
         '生成的调用参数为空或必填项全空，请求被网关或工具直接拦下，没有真正执行。',
         'arguments 为 {}、query 为空串、只给工具名不给参数。',
         '参数有值但类型或字段名不合契约 → `S2.M2.8`；被校验拒绝 → `S2.M3.2`。',
         '空的请求发出去，直接被拦。'),
        ('S2.M1.2', '未生成请求', ['缺失'],
         '计划里点名了某个动作，请求生成阶段没有产出对应的调用，这一条静默消失。',
         '计划三条查询只生成两条调用，且没有说明；某工具在本轮从未被请求。',
         '整批计划都没下发 → `S1.M1.3`；生成了但内容空 → `S2.M1.1`。',
         '该发的请求没写出来。'),
        ('S2.M2.1', '请求照抄原句', ['缺失'],
         '把用户整句问话当作查询文本下发，没有拆成字段、条件与期间。',
         'query 等于用户原问，没有指标名、没有日期、没有标的代码。',
         '拆了字段但字段选错 → `S2.M2.4`；条件过宽 → `S2.M2.5`。',
         '把问句原样当查询发出去。'),
        ('S2.M2.2', '标的写错', ['实体'],
         '请求里的标的代码、名称或市场域写错：代码拼错、市场写反、名称与代码不匹配。',
         '请求带 NVDA.N 而不是 NVDA、把港股代码写成 A 股格式、名称写对但代码指向另一只。',
         '计划里就认错标的 → `S1.M2.3`；标的对但取回别的 → `S3` 映射类叶子。',
         '请求里的代码不是用户说的那只。'),
        ('S2.M2.3', '期间写错', ['时效'],
         '请求里的日期、报告期或区间端点写错，与计划要的期间不一致。',
         '「近一年」写成起始日早于今天一年、报告期写成最新一期、区间两端写成同一天。',
         '计划里期间就错 → `S1.M2.4`；期间对但取回旧值 → `S3` 时效类叶子。',
         '请求的日期不是要的那个。'),
        ('S2.M2.4', '字段取近似', ['口径'],
         '请求要 A 指标，实际下发的字段是含 A 的近似或派生指标，口径已经变了。',
         '要「归母净利」请求成「净利润」、要「TTM」请求成最新单季、要「前复权」请求成不复权。',
         '字段对但返回缺该字段 → `S3` 的缺字段类叶子；取回后口径理解错 → `S5` 的口径类叶子。',
         '要 A 却按 B 去取。'),
        ('S2.M2.5', '过滤条件写错', ['缺失'],
         '请求的筛选条件方向错、范围过窄或过宽，或条件互相抵消。',
         '「高股息」写成低于阈值、三项 high 叠加导致零行、时间条件与标的条件互相冲突。',
         '完全没写条件 → `S1.M2.7`；条件对但执行零行 → `S3` 零行类叶子。',
         '条件写反，或者叠到没人满足。'),
        ('S2.M2.6', '排序分页写错', ['缺失'],
         '请求里的排序字段、方向、TopN 或分页参数写错。',
         '要「从低到高」写成降序、要前二十却取全量、分页参数指向第二页。',
         '排序对但取回条数少 → `S3` 截断类叶子。',
         '排序或条数参数写歪。'),
        ('S2.M2.7', '工具选错', ['失败'],
         '请求指向的工具不在可用清单里，或选了一个做不到这件事的工具。',
         '把技能名当函数名调用、调用清单外工具、用检索工具做结构化筛选。',
         '工具对但参数错 → `S2.M2.8`；计划层就选错 → `S1.M2.1`。',
         '调了一个不存在或不合适的工具。'),
        ('S2.M2.8', '请求结构不合契约', ['失败'],
         '请求的形式不合工具契约：参数类型错、字段名是契约外的、多个 JSON 拼接、必填项缺失。',
         '整数参数传字符串、用了契约没有的顶层字段、两个 query 对象拼成一个 arguments 串。',
         '结构对但内容指错对象 → `S2.M2.2`；被校验器拒绝 → `S2.M3.2`。',
         '请求形状不对，工具不认。'),
        ('S2.M2.9', '请求重复', ['缺失'],
         '同一请求在一轮里被重复下发，或把已成功的请求原样重发。',
         '连续两次相同 query；报错后不做修改直接重试同一参数。',
         '计划层重复 → `S1.M2.6`。',
         '同一条请求发了两遍。'),
        ('S2.M3.1', '生成物不可执行', ['失败'],
         '生成的脚本或调用一跑就炸：语法错误、引用不存在的列/键、括号不配对。',
         'SyntaxError、KeyError、NameError、未闭合字符串，脚本非零退出。',
         '能跑但算错 → `S2.M5.1`；被网关校验拦下 → `S2.M3.2`。',
         '写出来的代码根本跑不起来。'),
        ('S2.M3.2', '请求被校验拒绝', ['失败'],
         '请求或渲染 spec 在执行前被契约校验拒绝：字段不被支持、必填缺项、鉴权不通过。',
         'spec 校验报字段越界、工具返回参数不支持、鉴权失败无回执。',
         '请求内容本身指错对象 → `S2.M2.2`；工具已经执行但报错 → `S3`。',
         '还没执行就被校验挡回来。'),
        ('S2.M3.3', '生成物被截断', ['缺失'],
         '生成的参数、代码或 spec 被截断，只交付了一部分。',
         '参数列表写到一半、脚本最后一段缺失、spec 数组不闭合。',
         '计划层被截断 → `S1.M3.1`；执行输出被截断 → `S3`/`S4` 的截断类叶子。',
         '生成到一半就断了。'),
        ('S2.M5.1', '生成代码计算逻辑错', ['单位'],
         '生成物本身能跑，但里面的换算、聚合或标签逻辑写错。',
         '万手→手换算系数取错、聚合窗口把当日算进去、标签与数值错位。',
         '生成物跑不起来 → `S2.M3.1`；取回后再算错 → `S5` 的加工类叶子。',
         '代码能跑，算式是错的。'),
        ('S2.M6.1', '生成物写死结论', ['编造'],
         '把结论、数值或判断硬编码进生成的脚本或 spec，冒充计算结果。',
         '脚本里直接写死目标值再打印「结果」；spec 里写死一段没有数据来源的文案。',
         '有数据但算错 → `S2.M5.1`；终稿阶段编造 → `S5` 编造类叶子。',
         '把答案写死在代码里，跑一遍假装算出来。'),
        ('S2.M7.1', '生成物缺项', ['缺失'],
         '生成的请求或 spec 明确少了必需的部分：缺一个指标、缺一段序列、缺图例。',
         'spec 有数据序列但无 axis、脚本只写了取数不写渲染、请求只带部分指标。',
         '生成物整体为空 → `S2.M1.1`；计划层就漏维度 → `S1.M2.8`。',
         '生成的东西缺一块，跑起来也是残的。'),
    ],
}

# ---------------------------------------------------------------- 旧叶子搬运
# 新段 ← 旧 id 清单（按机制、按工具族排序）；文本沿用，id 重排，成分按段裁剪。
RESTAGE = {
    'S3': [
        'S3.M1.1',
        'S3.M2.1', 'S3.M2.2', 'S3.M2.3', 'S3.M2.4', 'S3.M2.5', 'S3.M2.6', 'S3.M2.7', 'S3.M2.8',
        'S4.M2.1',
        'S3.M3.1', 'S3.M3.2', 'S3.M3.3', 'S3.M3.4', 'S3.M3.6', 'S3.M3.7', 'S3.M3.8',
        'S5.M3.1', 'S6.M3.1', 'S6.M3.2', 'S7.M3.2', 'S7.M3.3', 'S4.M3.9',
        'S3.M4.1', 'S3.M4.2', 'S3.M4.3', 'S3.M4.4', 'S3.M4.5', 'S3.M4.6', 'S3.M4.7',
        'S5.M4.1', 'S5.M4.2', 'S5.M4.3', 'S5.M4.4', 'S5.M4.5', 'S5.M4.6',
        'S6.M4.1', 'S6.M4.2', 'S6.M4.3', 'S6.M4.4',
        'S7.M4.5', 'S8.M4.1', 'S2.M4.1',
        'S3.M5.1', 'S3.M5.2', 'S3.M5.3', 'S3.M5.4', 'S8.M5.1',
        'S3.M6.1', 'S6.M6.1', 'S6.M6.2',
        'S6.M7.1', 'S6.M7.2',
    ],
    'S4': [
        'S4.M3.2', 'S4.M3.4', 'S4.M3.5', 'S4.M3.7', 'S4.M3.8', 'S4.M3.10',
        'S4.M4.1', 'S4.M4.2', 'S4.M4.4', 'S4.M4.5', 'S4.M4.6', 'S4.M4.8',
        'S4.M5.3', 'S4.M5.4',
        'S4.M6.1',
        'S4.M7.1',
    ],
    'S5': [
        'S9.M1.1', 'S9.M1.2',
        'S9.M2.1', 'S9.M2.2',
        'S9.M4.1',
        'S9.M5.1', 'S9.M5.2', 'S9.M5.3', 'S9.M5.4', 'S9.M5.5', 'S9.M5.6',
        'S9.M6.1', 'S9.M6.2', 'S9.M6.3', 'S9.M6.4', 'S9.M6.5', 'S9.M6.6', 'S9.M6.7',
        'S9.M7.1', 'S9.M7.2', 'S9.M7.3', 'S9.M7.4',
    ],
}

TAG_BY_NAME = []  # 新叶子 tags 写在 NEW 里


def main():
    dry = '--dry' in sys.argv
    old_inv = load('inventory.json')
    old_nodes = {n['id']: n for n in old_inv['nodes']}
    old_tags = load('leaf_tags.json')
    old_disc = load('discriminators.json')
    comps = load('components.json')

    # 1. 新 5 段
    global L2
    L2 = old_inv['l2']

    # 2. 组件 → 阶段：先按 role 写入 stage，再据此建 tool→{stage} 反查表
    ROLE2STAGE = {'planning': 'S1', 'request_generation': 'S2', 'tool': 'S3',
                  'script': 'S4', 'delivery': 'S5'}
    FINQ_SCRIPT = ('/opt/skills-aime/aime-finquery/', '/opt/skills-wencai')
    for raw, e in comps.items():
        e['tool'] = '答案合成' if e['tool'] == '终稿生成轮' else e['tool']
        role = e.get('role') or default_role(e['tool'])
        e['role'] = role
        stage = ROLE2STAGE[role]
        if role == 'script' and raw.startswith(FINQ_SCRIPT):
            stage = 'S3'  # 用户口径：取数技能脚本算工具执行（责任域优先）
        e['stage'] = stage
    tool_stages = collections.defaultdict(set)
    for raw, e in comps.items():
        tool_stages[e['tool']].add(e['stage'])

    # 3. 组装节点
    nodes, old2new, new_tags = [], {}, {}

    for l1, rows in NEW.items():
        for nid, name, tags, definition, include, exclude, plain in rows:
            nodes.append({'id': nid, 'l1': l1, 'l2': nid.split('.')[1], 'name': name,
                          'definition': definition + '（本叶仅收本段产生的缺陷）',
                          'include': include, 'exclude': exclude,
                          'components': sorted({c for c in STAGES[l1]['components']
                                                if l1 in tool_stages.get(c, set())}),
                          'plain': plain})
            new_tags[nid] = tags

    for new_l1, olds in RESTAGE.items():
        seq = collections.Counter()
        for old_id in olds:
            o = old_nodes[old_id]
            mei = o['l2']
            seq[mei] += 1
            nid = f"{new_l1}.{mei}.{seq[mei]}"
            old2new[old_id] = nid
            # 成分裁剪：先按 v4 改名，再只留把该段纳入自己阶段集合的工具
            RENAMED = {'终稿生成轮': '答案合成'}
            keep = [RENAMED.get(c, c) for c in o['components']]
            keep = [c for c in keep if new_l1 in tool_stages.get(c, set())]
            nodes.append({'id': nid, 'l1': new_l1, 'l2': mei, 'name': o['name'],
                          'definition': o['definition'], 'include': o['include'],
                          'exclude': o['exclude'], 'components': keep, 'plain': o.get('plain', '')})
            new_tags[nid] = old_tags.get(old_id, [])

    # 4. exclude/definition 里的旧 id 引用重映射
    def fix_refs(text):
        if not text:
            return text
        def rep(m):
            oid = m.group(0)
            if oid in old2new:
                return old2new[oid]
            return oid  # 未搬运的旧 id 留给 lint 报错，人工处理
        return re.sub(r'\bS[1-9]\.M[1-7]\.\d+\b', rep, text)

    for n in nodes:
        for k in ('definition', 'include', 'exclude', 'plain'):
            n[k] = fix_refs(n[k])

    # 7. 搬运后失配的三处排他指向（原指向的旧叶已按新轴并入别的段/被删）
    POST_FIX = {
        'S3.M4.19': {'exclude': '技能根本没加载成功（工具或参数选错）→ `S2.M2.7`；'
                                '加载成功但图像理解结果合计算错 → `S3.M5.5`。'},
        'S4.M4.4': {'exclude': '时点可核但拿到的是过期快照 → `S3.M4.7`（属工具执行）。'},
        'S5.M7.2': {'exclude': '产物已生成但渲染引用解析失败 → `S2.M3.2`（属请求生成）。'},
    }
    for n in nodes:
        n.update(POST_FIX.get(n['id'], {}))

    inv = {'l1': {k: {'name': v['name'], 'definition': v['definition']} for k, v in STAGES.items()},
           'l2': L2, 'nodes': nodes}

    # 6. 其他表
    leaf_tags = {n['id']: new_tags[n['id']] for n in nodes}
    disc = {}
    for old_id, d in old_disc.items():
        if old_id in old2new:
            dd = dict(d)
            if dd.get('near') in old2new:
                dd['near'] = old2new[dd['near']]
            elif 'near' in dd:
                continue
            disc[old2new[old_id]] = dd
    comp2l1 = {}
    for tool, sts in tool_stages.items():
        comp2l1[tool] = sorted(sts)[0] if len(sts) == 1 else '+'.join(sorted(sts))
    stages = {k: {'name': v['name'], 'definition': v['definition'],
                  'components': sorted({c for c in v['components'] if c in tool_stages})}
              for k, v in STAGES.items()}

    print(f"新分类法：{len(nodes)} 叶 / {len(STAGES)} 段")
    for l1 in STAGES:
        ls = [n for n in nodes if n['l1'] == l1]
        print(f"  {l1} {STAGES[l1]['name']:6s} {len(ls):3d} 叶  "
              + ' '.join(f"{m}:{sum(1 for n in ls if n['l2'] == m)}" for m in ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7']
                         if any(n['l2'] == m for n in ls)))

    if dry:
        print('dry run, nothing written')
        return
    dump(inv, 'inventory.json')
    dump(leaf_tags, 'leaf_tags.json')
    dump(disc, 'discriminators.json')
    dump(stages, 'stages.json')
    dump(comp2l1, 'comp2l1.json')
    dump(comps, 'components.json')
    print('written: inventory/leaf_tags/discriminators/stages/comp2l1/components')


def default_role(tool):
    return {'planner-finquery': 'planning', 'planner-search': 'planning',
            'finquery': 'tool', 'web_search': 'tool', 'web_fetch': 'tool',
            'customer_services': 'tool', 'CustomerServiceFAQ': 'tool',
            'aime_memory_read': 'tool', 'get_user_context': 'tool',
            'stock_ner_parse': 'tool', 'query_user_positions': 'tool',
            'watchlist_manager': 'tool', 'understand_image': 'tool',
            'shell': 'script', 'CodeInterpreter': 'script', 'shell_poll': 'script',
            'view_skill/read_file': 'tool', 'client_action': 'request_generation',
            'display': 'request_generation', 'aime_memory_write': 'request_generation',
            '答案合成': 'delivery', '中间推理处理': 'delivery',
            }.get(tool, 'tool')


if __name__ == '__main__':
    main()
