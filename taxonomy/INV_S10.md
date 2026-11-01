# 环节 S10：请求生成

把计划落成可调用的请求、代码或渲染 spec：定参数、字段、结构、契约。失效多为参数缺失/类型错、字段取近似、工具选错、生成物不可执行。

## 0. 判定骨架（按顺序问，命中即停）

1. **这一环该做的动作，一点都没做？** → `S10.M1.1` 请求为空 / `S10.M1.2` 未生成请求
2. **动作做了，但对象/指标/时段/范围不是用户要的？** → `S10.M2.1` 请求照抄原句 / `S10.M2.2` 请求标的写错 / `S10.M2.3` 请求期间写错 / `S10.M2.4` 请求字段取近似 / `S10.M2.5` 请求过滤条件写错 / `S10.M2.6` 请求排序分页写错 / `S10.M2.7` 工具选错 / `S10.M2.8` 请求结构不合契约 / `S10.M2.9` 请求重复
3. **调用报错/超时/零行/空返回/被截断？** → `S10.M3.1` 生成物不可执行 / `S10.M3.2` 请求被校验拒绝 / `S10.M3.3` 生成物被截断
5. **数据对，但换算/聚合/口径/比较错，或自相矛盾？** → `S10.M5.1` 生成代码计算逻辑错
6. **结论没证据，或与已有证据相反？** → `S10.M6.1` 生成物写死结论
7. **形式与完整性不达标（缺项/格式/语言/答非所问）？** → `S10.M7.1` 生成物缺项

> 同一环节内，先定机制，再选类别。跨机制的类别不互斥，靠本节顺序裁决。

## 1. 类别速查表

| 类别 | 名称 | 机制 | 条目 | 会话 | 标签 | 最相近 |
|---|---|---|---|---|---|---|
| `S10.M1.1` | 请求为空 | M1 | – | – | 失败 | `–` |
| `S10.M1.2` | 未生成请求 | M1 | – | – | 缺失 | `–` |
| `S10.M2.1` | 请求照抄原句 | M2 | – | – | 缺失 | `–` |
| `S10.M2.2` | 请求标的写错 | M2 | – | – | 实体 | `–` |
| `S10.M2.3` | 请求期间写错 | M2 | – | – | 时效 | `–` |
| `S10.M2.4` | 请求字段取近似 | M2 | – | – | 口径 | `–` |
| `S10.M2.5` | 请求过滤条件写错 | M2 | – | – | 缺失 | `–` |
| `S10.M2.6` | 请求排序分页写错 | M2 | – | – | 缺失 | `–` |
| `S10.M2.7` | 工具选错 | M2 | – | – | 失败 | `–` |
| `S10.M2.8` | 请求结构不合契约 | M2 | – | – | 失败 | `–` |
| `S10.M2.9` | 请求重复 | M2 | – | – | 缺失 | `–` |
| `S10.M3.1` | 生成物不可执行 | M3 | – | – | 失败 | `–` |
| `S10.M3.2` | 请求被校验拒绝 | M3 | – | – | 失败 | `–` |
| `S10.M3.3` | 生成物被截断 | M3 | – | – | 缺失 | `–` |
| `S10.M5.1` | 生成代码计算逻辑错 | M5 | – | – | 单位 | `–` |
| `S10.M6.1` | 生成物写死结论 | M6 | – | – | 编造 | `–` |
| `S10.M7.1` | 生成物缺项 | M7 | – | – | 缺失 | `–` |

## 2. 类别判定卡（用来区分互斥）

### `S10.M1.1` 请求为空

- **定义**：生成的调用参数为空或必填项全空，请求被网关或工具直接拦下，没有真正执行。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：arguments 为 {}、query 为空串、只给工具名不给参数。
- **排除**：参数有值但类型或字段名不合契约 → `S10.M2.8`；被校验拒绝 → `S10.M3.2`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：失败 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M1.2` 未生成请求

- **定义**：计划里点名了某个动作，请求生成阶段没有产出对应的调用，这一条静默消失。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：计划三条查询只生成两条调用且无说明；某工具本轮从未被请求。
- **排除**：整批计划都没下发（属计划层）→ `S1.M1.3`；生成了但内容空 → `S10.M1.1`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：缺失 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.1` 请求照抄原句

- **定义**：把用户整句问话当作查询文本下发，没有拆成字段、条件与期间。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：query 等于用户原问，没有指标名、没有日期、没有标的代码。
- **排除**：拆了字段但字段选错 → `S10.M2.4`；条件过宽 → `S10.M2.5`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：缺失 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.2` 请求标的写错

- **定义**：请求里的标的代码、名称或市场域写错：代码拼错、市场写反、名称与代码不匹配。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：请求带 NVDA.N 而不是 NVDA、港股代码写成 A 股格式、名称对但代码指向另一只。
- **排除**：计划层就认错标的 → `S1.M2.2`；标的对但取回别的 → `S3.M2.1`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：实体 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.3` 请求期间写错

- **定义**：请求里的日期、报告期或区间端点写错，与计划要的期间不一致。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：「近一年」起始日算错、报告期取成最新一期、区间两端写成同一天。
- **排除**：计划层就锚错 → `S1.M2.4`；期间对但取回旧值 → `S3.M4.7`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：时效 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.4` 请求字段取近似

- **定义**：请求要 A 指标，实际下发的字段是含 A 的近似或派生指标，口径已经变了。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：要「归母净利」请求成「净利润」、要 TTM 请求成最新单季、要前复权请求成不复权。
- **排除**：字段对但返回缺该字段 → `S3.M4.1`；取回后口径理解错 → `S9.M5.4`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：口径 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.5` 请求过滤条件写错

- **定义**：请求的筛选条件方向错、范围过窄或过宽，或几个条件互相抵消。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：「高股息」写成低于阈值、三项 high 叠加导致零行、时间条件与标的条件冲突。
- **排除**：完全没写条件（属计划层）→ `S1.M2.7`；条件对但执行零行 → `S3.M3.3`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：缺失 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.6` 请求排序分页写错

- **定义**：请求里的排序字段、方向、TopN 或分页参数写错。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：要「从低到高」写成降序、要前二十却取全量、分页参数指向第二页。
- **排除**：排序对但取回条数少 → `S3.M3.8`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：缺失 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.7` 工具选错

- **定义**：请求指向的工具不在可用清单里，或选了一个做不到这件事的工具。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：把技能名当函数名调用、调用清单外工具、用检索工具做结构化筛选。
- **排除**：工具对但参数错 → `S10.M2.8`；计划层就选错 → `S1.M2.1`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：失败 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.8` 请求结构不合契约

- **定义**：请求的形式不合工具契约：参数类型错、字段名是契约外的、多个 JSON 拼接、必填项缺失。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：整数参数传字符串、用了契约没有的顶层字段、两个 query 对象拼成一个 arguments 串。
- **排除**：结构对但内容指错对象 → `S10.M2.2`；被校验器拒绝 → `S10.M3.2`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：失败 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M2.9` 请求重复

- **定义**：同一请求在一轮里被重复下发，或把已成功的请求原样重发。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：连续两次相同 query；报错后不改参数直接重试同一条。
- **排除**：计划层重复 → `S1.M2.11`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：缺失 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M3.1` 生成物不可执行

- **定义**：生成的脚本或调用一跑就炸：语法错误、引用不存在的列/键、括号不配对。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：SyntaxError、KeyError、NameError、未闭合字符串，脚本非零退出。
- **排除**：能跑但算错 → `S10.M5.1`；执行前被校验拦下 → `S10.M3.2`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：失败 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M3.2` 请求被校验拒绝

- **定义**：请求或渲染 spec 在执行前被契约校验拒绝：字段不被支持、必填缺项、鉴权不通过。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：spec 校验报字段越界、工具返回参数不支持、鉴权失败无回执。
- **排除**：请求内容指错对象 → `S10.M2.2`；工具已执行但报错 → `S3.M3.2`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：失败 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M3.3` 生成物被截断

- **定义**：生成的参数、代码或 spec 被截断，只交付了一部分。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：参数列表写到一半、脚本最后一段缺失、spec 数组不闭合。
- **排除**：计划层被截断 → `S1.M3.1`；执行输出被截断 → `S3.M3.8`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：缺失 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M5.1` 生成代码计算逻辑错

- **定义**：生成物本身能跑，但里面的换算、聚合或标签逻辑写错。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：万手→手换算系数取错、聚合窗口把当日算进去、标签与数值错位。
- **排除**：生成物跑不起来 → `S10.M3.1`；取回后再算错 → `S9.M5.1`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：单位 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M6.1` 生成物写死结论

- **定义**：把结论、数值或判断硬编码进生成的脚本或 spec，冒充计算结果。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：脚本里直接写死目标值再打印「结果」；spec 里写死一段没有数据来源的文案。
- **排除**：有数据但算错 → `S10.M5.1`；终稿阶段编造 → `S9.M6.4`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：编造 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

### `S10.M7.1` 生成物缺项

- **定义**：生成的请求或 spec 明确少了必需的部分：缺一个指标、缺一段序列、缺图例。（本叶仅收请求生成环节产生的缺陷）
- **可观测信号**：–
- **纳入**：spec 有数据序列但无 axis、脚本只写取数不写渲染、请求只带部分指标。
- **排除**：生成物整体为空 → `S10.M1.1`；计划层就漏维度 → `S1.M2.6`。
- **最相近的类别**：`–` — 区分点：–
- **标签**：缺失 ｜ **组件**：CodeInterpreter、aime_memory_read、aime_memory_write、client_action、customer_services、get_user_context、planner-finquery、planner-search、query_user_positions、shell、stock_ner_parse、understand_image、view_skill/read_file、watchlist_manager

## 3. 最容易混淆的类别对

| A | B | 一句话区分 |
|---|---|---|

