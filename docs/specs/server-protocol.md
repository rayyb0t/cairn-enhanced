# Cairn 协作探索协议

---

## 本质

衍迹（Cairn）将目标导向的探索过程建模为一张有向无环图：从已知事实出发，不断探索产出新事实，直到某些事实共同满足终点。名称取自登山者用石头垒起的路标——每个 Fact 是一块石头，Agent 沿着前人的 cairn 继续探索，同时垒新的 cairn 给后来者。

这套 API 管理这张图的生长过程。多个消费者（人或 Agent）并发读取完整图，各自声明探索意图，各自产出新事实，最终由某个消费者判断终点已达成。对于一些很简单的问题，消费者也可以在项目最初阶段直接围绕起点和终点做一次快速推进，而不必先拆成多轮显式规划；这种“起步即做”的过程仍通过 Intent/Fact 正常落图。

图仍然是知识与因果关系的唯一表达；但协议也允许少量**项目级协调状态**，用于消费者之间做互斥和可见性协作。这类状态不是事实，不属于图，不参与因果推理。当前唯一内置的项目级协调状态是 `Project.reason`，用于标识“某个消费者正在对整个项目做一次 `reason` 判断”。

系统不做任何推理和决策，只负责图的一致性维护。

### 设计渊源

这套协议是**黑板架构**（Blackboard Architecture）的现代化重构。1970 年代 CMU 的 Hearsay-II 系统提出了同样的范式：多个专家围绕一块共享黑板工作，各自读取当前状态，各自贡献新知识，没有中央调度。Fact 对应黑板上的内容，Intent 对应专家的行动记录，Hint 对应旁注。区别在于：经典黑板只有"当前状态"，本协议通过 Intent 保留了完整的因果链，使得图同时是知识库和推理路径的审计日志。

协调机制上，这套设计等价于蚁群的**信息素机制**（stigmergy）——个体之间不直接通信，而是通过改变共享环境间接协调。Agent 读图、写 Fact，就是在环境上留下信息素，其他 Agent 据此决策。

每个消费者的工作循环（读图→判断→声明意图→执行→产出事实）构成一个独立的 **OODA 循环**（Observe-Orient-Decide-Act），多个循环通过共享图间接同步。这与军事指挥中的**任务式指挥**（Mission Command）同构——上级只下达目标（goal），各单位根据态势自主决策，Hint 扮演"指挥官意图"的角色。核心优势：决策速度不受中心调度瓶颈限制。

### 状态变化的表达

Fact 只增不改，状态变化通过追加新 Fact 表达。例如在多层网络渗透中：

- `f003`: "获得 host-A 的 root shell"
- `f007`: "通过 host-A 跳板发现内网 10.0.2.0/24"
- `f025`: "host-A shell 已断开"

"shell 断了"本身就是一个新的客观事实。Agent 读图时看到 f003 和 f025，自行判断当前可达性，从 f025 出发声明新的探索意图（重建 shell、换跳板）。不需要修改历史 Fact，不需要标记失效——事实的时序本身就携带了状态变化的信息。

---

## 核心概念

### Project

一个有明确起点和终点的问题实例，包含完整的图数据。

三种状态：

- `active`：进行中，接受探索写操作
- `stopped`：硬停止，不接受探索写操作，可恢复为 `active`
- `completed`：当前已完成，不接受探索写操作；若外部确认之前的完成判断有误，可显式调用 `reopen` 撤销完成态并继续

### Fact

图中的节点，代表一个已确认的客观事实。只有描述文本，没有状态标记。只增不改，永久保留。

描述文本应是探索结论的提炼，而非原始数据。当原始输出较大时（如扫描结果），描述应包含关键洞见和原始数据的文件引用，例如："nmap 全端口扫描发现 22/80/443/8080 开放，80 和 8080 均为 HTTP 服务，详细结果见 /tmp/scans/nmap_192.168.1.10.xml"。这保证图保持轻量，同时原始数据仍可追溯。

每个 Project 有两个特殊 Fact，在 Project 创建时写入：

- `origin`：起点
- `goal`：终点

普通 Fact 由系统生成 id，如 `f001`。

### Intent

图中的边，代表从一个或多个 Fact 出发的探索过程。有三种状态：

- **进行中**：Intent 已被某个消费者认领，`worker` 不为 null
- **未认领**：Intent 尚无结论，且当前 `worker` 为 null，等待消费者接手
- **已结论**：`to` 不为 null，探索已完成，产出了结论 Fact

未结论的 Intent（`to=null`）表示这个探索方向已声明但尚未有结论。声明时可只写下方向而不认领，也可同时认领。已结论的 Intent 不可修改。

协议本身不区分 Intent 的“类型”，所有 Intent 在服务端都一视同仁；但消费者可以约定某些**保留语义**。例如可约定一条：

- `from = ["origin"]`
- `description = "bootstrap"`

的 open Intent 表示“项目最初阶段的一次直接推进尝试”。这类保留 intent 仍然走同样的 heartbeat / release / conclude 生命周期，不需要额外接口；区别只在于消费者会把它解释为“先自己分析并执行，先把关键证明性事实落图；若该事实已足以满足 goal，再继续声明 complete”，而不是普通的细分探索方向。

这个约定适合那些可能很快完成、或至少很快得到首个关键突破的问题。这样项目在前端和审计记录中仍然是可见、可理解的：读者能明确看到系统先做过一次 `bootstrap` 尝试，产出了什么关键事实；像 flag、shell、权限证明这类对后续系统有价值的结果，不会因为直接完成项目而绕开事实图。

**超边语义：** `from` 允许多个 Fact id。`from` 包含多个 id 时，这条 Intent 在图中等价于一条超边——多个源节点共同指向同一个目标节点，共享同一个 description、creator 和 worker：

```
f002 ──┐
       ├──(intent)──→ f006
f004 ──┘
```

这完整保留了"多个已知事实共同支撑一次探索"的因果关系，而不是被迫选一个主 Fact 丢失其余上下文。

### Hint

图外输入，策略建议或补充说明。不属于事实图，不影响因果关系，供消费者读图时参考。

Hint 代表外部补充的高纬输入，不属于探索执行本身。因此项目处于 `stopped` 或 `completed` 时，仍允许继续补充 Hint。`stopped` 项目恢复为 `active` 后，后续消费者会在下一次读图时看到这些新增提示；`completed` 项目上的新 Hint 会被保留，若之后需要继续处理该项目，仍应通过 `reopen` 回到 `active`。

除了人工补充的策略提示，Hint 也适合用于消费者之间传递**态势评估**。随着图增长，大量已排除的方向会稀释有效信息。消费者在完成一轮探索后，可将对当前局势的判断写为 Hint（如"SSH 和 SQL 注入方向均已排除，攻击面集中在文件上传功能"），帮助后续消费者快速定位重点，降低每轮读图的认知成本。

---

## 数据结构

### Project

```
id
title           # 项目名称
status          # "active" | "stopped" | "completed"
bootstrap_enabled  # 是否允许消费者在初始态运行 bootstrap，默认 true
created_at
reason          # 当前项目级 reason lease，null 表示当前无人执行 reason
repair_enabled  # 修复层（影子模式）开关，默认 false；只有 JSON 的 true 表示打开；从不导出
```

`repair_enabled` 是修复层（影子模式）的项目开关（见下文「修复层（影子模式）」），是附加字段，出现在每个项目对象中（`GET /projects`、`GET /projects/{project_id}` 以及返回项目元信息的各接口）。新建项目默认关闭；升级前已有的项目迁移后同样为关闭（新列 `DEFAULT 0`）。读取方只把 JSON 的 `true` 当作打开：`null`、`1`、`"yes"` 等任何其他值都按关闭处理，且不会让项目读取失败；缺少该字段（旧服务端）也按关闭处理。该开关不出现在导出（YAML 与 timeline）中。

### Project.reason

```
worker             # 当前执行 reason 的消费者
trigger            # 本次 reason 的触发原因，由消费者定义
started_at         # 本次 reason 开始时间
last_heartbeat_at  # 最近一次 reason 心跳时间
```

### Fact

```
id              # "origin" | "goal" | 系统生成（如 f001）
description     # 客观事实描述
```

### Intent

```
id
from            # 出发 Fact 的 id 数组，至少一个
to              # 结论 Fact 的 id，null 表示尚无结论
description     # 意图描述
creator         # 声明意图的消费者，创建时写入，不可变
worker          # 执行者标识，语义随状态变化（见下方说明）
last_heartbeat_at  # 最后一次心跳时间，仅尚无结论时有意义，永久保留，不清空
created_at
concluded_at    # 结论时间，null 表示尚无结论
issues          # 运行中出现的问题（只读，最新在前），无问题时为空数组
```

`creator` 在创建时写入，标识谁洞察局势提出了这个探索方向，不可变。`worker` 可为空；若创建时同时认领，则 `worker` 必须等于 `creator`，并写入首个 `last_heartbeat_at`。

请求体里的文本字段（如 `title`、`content`、`description`、`creator`、`worker`、`trigger`）以及 `from` 中的 Fact id，服务端都会先做首尾空白裁剪；裁剪后若为空，则返回 `422`。

如果消费者采用 `bootstrap` 约定，常见做法是使用固定 `creator`，例如 `dispatcher.bootstrap`，以便在图上与普通探索 intent 清晰区分；这只是消费者约定，不是服务端约束。

**worker 字段语义：**

| Intent 状态           | worker 含义                      |
| --------------------- | -------------------------------- |
| 尚无结论，worker=null | 当前无人处理，等待认领           |
| 尚无结论，worker 有值 | 有消费者正在处理                 |
| 尚无结论，worker 超时 | worker 清空为 null，等待重新认领 |
| 已结论                | 产出结论的消费者，永久保留       |

超时清空 worker 只针对尚无结论的 Intent。已结论的 Intent 不参与超时逻辑，worker 永久保留为产出结论的消费者标识。

**issues 字段：** 只读的追加字段，记录该 Intent 运行中出现、需要在界面上提示的问题（界面上显示为提示图标；已结论的 Intent 显示在其产出的 Fact 节点上）。每条包含 `id`、`kind`、`run_id`（`repair_review` 时为运行 id，其余为 `null`）、`detail`（对象）、`created_at`、`updated_at`。`kind` 目前有两种。`json_repaired` 表示 conclude 的输出在严格解析失败后经保守修复才解析成功，`detail` 为 `{"strategy": "lenient_decode" | "envelope_repair", "error": "<严格解析的错误>"}`，由 conclude 请求的可选字段 `parse_repair` 写入。`repair_review` 记录修复层（影子模式）对一次运行的评审结论，或服务端的“未评审”记录（见下方说明）。`issues` 不参与导出（YAML 与 timeline 都不含），因此不会进入消费者读取的图快照。不认识该字段的客户端可直接忽略；读取方遇到无法识别的 issue 应跳过该条，而不是让整个读取失败。

**`repair_review` issue：** 修复层（影子模式，见下文「修复层（影子模式）」）为一次运行的评审窗口写入一条 `kind = "repair_review"` 的 issue，`run_id` 为该运行的 `run_id`。它由修复代理的评审（`POST /repair/runs/{run_id}/reviews`）或服务端的“未评审”记录写入，与 `json_repaired` 存放在同一张 `intent_issues` 表中，同样按最新在前读取。它只是记录：`mode` 恒为 `"shadow"`，`applied` 恒为 `false`，任何结论都不会被执行。运行中的 Intent 把它显示在自己的节点上；Intent 结论后，它随其他 issue 一起显示在产出的 Fact 节点上。由于 `GET /projects/{project_id}` 携带每个 Intent 的全部 issue、每次 heartbeat 的回复也携带该 Intent 的 issue，`detail` 只是一份精简的展示副本：

| 键 | 类型 | 写入方 | 含义 |
| --- | --- | --- | --- |
| `decision` | `continue` \| `extend` \| `detach` \| `not_reviewed` | 修复代理或服务端 | 评审结论，或服务端的未评审记录 |
| `mode` | `"shadow"` | 服务端 | 只记录，不执行 |
| `applied` | `false` | 服务端；修复代理发来的 `applied` 被忽略 | |
| `window` | 整数 | 服务端 | Phase 2 恒为 1 |
| `run_started_at` | 时间 | 服务端 | 运行的 `started_at` |
| `elapsed_seconds` | 整数 | 服务端 | 记录时已用的时间；对已结束运行的未评审记录，计到它最后已知存活的时刻 |
| `budget_seconds`、`window_seconds`、`deadline_at`、`slices_used` | | 服务端 | 运行的快照 |
| `reason` | 字符串，至多 400 个字符 | 修复代理 | 展示副本；空白折叠，去掉 Unicode Cc 与 Cf 类字符 |
| `evidence` | 至多 4 个、每个至多 200 个字符的字符串 | 修复代理 | 展示副本 |
| `next_step` | 字符串或 null，至多 200 个字符 | 修复代理 | 展示副本 |
| `display_truncated` | 布尔值 | 服务端 | 任一展示副本被截断时为 true |
| `fallback` | null，或 `model_timeout` \| `model_error` \| `invalid_output` \| `transcript_unreadable` \| `digest_error` | 修复代理 | 自动的 continue，不是模型的选择 |
| `model` | 字符串或 null，至多 64 个字符 | 修复代理 | 作答的模型 |
| `slice_seconds` | 整数 | 服务端，仅 extend | 若执行这次 extend 会增加的时长 |
| `why` | `agent_not_seen` \| `agent_seen` | 服务端，仅 not_reviewed | 窗口打开之后修复代理是否轮询过 |
| `agent_last_seen_at` | 时间或 null | 服务端，仅 not_reviewed | 修复代理最近一次轮询的时间 |
| `run_ended` | 布尔值 | 服务端，仅 not_reviewed | 记录时运行是否已结束 |
| `run_ended_at`、`end_reason` | 时间、字符串 | 服务端，运行结束时补上；对已结束运行的未评审记录，在写入时即带上 | 供界面标出属于更早一次尝试的评审 |
| `outcome` | 字符串 | 服务端，结果 `/end` 存入运行的结果时补上 | 任务的返回值（`success`、`failed` …）；一旦有值，界面即视该运行为已结束 |

完整文本（`reason` 至多 2,000 个字符，`evidence` 至多 10 条、每条至多 500 个字符，`next_step` 至多 500 个字符）、digest 与模型原始输出保存在 `repair_reviews` 表中，只能凭 agent 令牌读取（`GET /repair/runs/{run_id}`）。`detail` 至多两层嵌套，远在 issue 的 8 层上限之内。

### Hint

```
id
content
creator         # 提示的作者
created_at
```

### Settings

```
intent_timeout  # 单位秒，超过此时间无心跳则 worker 清空为 null
reason_timeout  # 单位秒，超过此时间无心跳则 Project.reason 清空为 null
```

---

## 修复层（影子模式）

修复层是叠加在现有协议之上的可选附加层：在长时间的 `explore` 接近超时时，由独立的修复代理（repair agent，见 `docs/specs/repair-agent.md`）判断这次运行是在推进还是卡住了。当前阶段（Phase 2）是**影子模式**：只记录，从不执行。每个项目有一个开关 `repair_enabled`，默认关闭。开关打开后，Dispatcher 为该项目每一次 `explore` 运行登记一条运行记录（见 `docs/specs/dispatcher-design.md`「修复层（影子模式）」），修复代理在运行预算的 75%–80% 之间对它评审一次，结论作为一条 `repair_review` issue 显示在 Intent 节点上，界面注明 “Shadow mode: recorded, not applied.”。评审不会改变任何运行：不延长截止时间、不追加时间片、不脱离、不停止，每次运行仍按今天的方式开始、执行和结束。

开关关闭时（每个项目的默认状态），项目的行为与之前完全相同：现有接口只多出一个附加字段 `"repair_enabled": false`，现有语句与导出不变，也没有任何现有接口调用修复代码。唯一的例外是删除：删除项目（以及 reopen 删除完成边对应的 Intent）的语句会通过外键级联删除修复表中对应的行（见下文「删除」）；从未打开过修复层的项目没有修复数据，因此不会多删任何东西。

修复层的运行与评审都不属于图：它们不是 Fact，也不是 Intent，不参与因果推理，也不出现在导出中。

### 存储

修复数据保存在两张新表中，由现有的建表脚本创建。`projects` 表只追加一列 `repair_enabled INTEGER NOT NULL DEFAULT 0`，放在最后一列，使新库与迁移后的旧库列顺序相同；已有项目迁移后为 0，即关闭。服务端不保存任何修复设置：评审规则是代码中的常量，由 `GET /repair/settings` 只读地报告。

`repair_runs`：每次运行一行。

| 列 | 含义 |
| --- | --- |
| `run_id` | 主键，由 Dispatcher 生成（`uuid4().hex`），每个 execute 进程一个 |
| `project_id`、`intent_id` | 运行所属的项目与 Intent |
| `worker` | Worker 名称，即服务端的 claim 键 |
| `cli_type` | `claudecode` \| `codex` \| `pi` \| `mock` |
| `execution` | `container` \| `local` |
| `session_id` | 会话 id，未知时为 NULL |
| `locator_json` | 不透明的定位信息（容器或工作目录、prompt marker、Dispatcher 的开始时间），默认 `'{}'`；原样保存、原样返回，服务端从不解释 |
| `budget_seconds` | 运行开始时的 `tasks.explore.timeout` |
| `started_at` | 服务端时钟：收到登记的时间减去 `started_seconds_ago` |
| `deadline_at` | `started_at + budget_seconds`；Phase 2 中从不移动 |
| `version` | Phase 2 恒为 1 |
| `slices_used` | Phase 2 恒为 0 |
| `state` | `running` \| `ended`（Phase 3/4 会增加 `sealed`、`detached`、`stop_requested`） |
| `reviewable` | 窗口短于 350 s，或 CLI 没有会话记录时为 0 |
| `settled_window` | 已决定“是否评审”的最高窗口号 |
| `registered_at`、`updated_at` | 登记与最近一次更新的时间 |
| `ended_at` | 服务端把运行标记为结束的时间 |
| `end_reason` | `dispatcher` \| `superseded` \| `concluded` \| `released` \| `claim_lost` \| `claim_taken` \| `project_inactive` |
| `last_alive_at` | 已知运行仍存活的最后时刻；未知时为 NULL |
| `outcome` | `run_explore_task` 的返回值，来自第二次 `/end` |
| `outcome_json` | 第一次 `/end` 上报的 `{"timed_out", "cancelled", "returncode"}` |
| `fact_id` | 该 Intent 产出的 Fact（一旦已知） |

外键为 `FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE`。三个索引：`repair_runs_open_key`，`(project_id, intent_id, worker)` 上的部分唯一索引（`WHERE ended_at IS NULL`），即同一项目、Intent 与 Worker 至多一条未结束的运行；`repair_runs_by_intent`，`(project_id, intent_id)`，供级联删除查找；`repair_runs_attention`，`(project_id)`，`WHERE ended_at IS NULL OR settled_window = 0`，让 sweep 只扫描未结束或未定的行。

`repair_reviews`：每次运行的每个窗口至多一条评审。主键 `(run_id, window_no)` 让“一个窗口一条评审”成为数据库层面的事实。

| 列 | 含义 |
| --- | --- |
| `run_id` | `REFERENCES repair_runs(run_id) ON DELETE CASCADE` |
| `window_no` | 窗口号（`window` 是 SQL 关键字） |
| `review_id` | 修复代理的幂等键；服务端的未评审记录为 `'server'` |
| `source` | `agent` \| `server` |
| `decision` | `continue` \| `extend` \| `detach` \| `not_reviewed` |
| `issue_id` | 界面显示的那条 `intent_issues` 行 |
| `detail_json` | 完整保存的评审 |
| `digest` | 模型看到的 digest，至多 65536 个字符 |
| `digest_sha256` | digest 的 SHA-256 |
| `raw_output` | 模型回答的第一段文本，至多 16384 个字符 |
| `created_at` | 记录时间 |

### 运行

一次运行（run）是一次 `explore` 尝试的一个 execute 进程。被释放或过期后重新认领的 Intent 是一次新的尝试，有新的 `run_id`，旧行保留为历史。重复以“Intent + Worker 名称”为键：部分唯一索引保证同一 (project, intent, worker) 至多一条未结束的行，同一 Worker 为同一 Intent 登记新运行时，旧的未结束运行以 `superseded` 结束。claim 协议本身已保证一个 Intent 不会同时被两个 Worker 持有，登记又要求登记方持有 claim，因此实际上每个 Intent 至多一条进行中的运行。

开关不参与存活判定：运行中途关闭开关，该运行照常存活、照常结束，只是不再出现在列表中，评审也会被拒绝（`409 repair_disabled`）。Dispatcher 下一次读到项目之后才开始的运行走新的路径；一次运行始终保持它开始时的路径。

### 存活判定

未结束的行（`ended_at IS NULL`）在 `run_end_reason` 返回结束原因之前都视为存活。规则写在行的 `state` 之上，使 Phase 4 无需改写就能豁免脱离的运行：

```python
LAPSE_EXEMPT_STATES: frozenset[str] = frozenset()    # Phase 4 加入 {"detached", "stop_requested"}

def repair_lapse_seconds(intent_timeout: int) -> int:
    return max(LAPSE_TIMEOUT_MULTIPLIER * intent_timeout, LAPSE_MIN_SECONDS)   # 4 与 60：默认 intent_timeout 为 15 时为 60 s

def run_end_reason(run, intent, project, now, intent_timeout) -> tuple[str, str | None] | None:
    """None while the open run is live, else (end_reason, last_alive_at)."""
    if intent["to_fact_id"] is not None:
        return "concluded", intent["concluded_at"]
    if project["status"] != "active":
        return "project_inactive", intent["last_heartbeat_at"]
    if intent["worker"] is not None and intent["worker"] != run["worker"]:
        return "claim_taken", None
    if run["state"] in LAPSE_EXEMPT_STATES:
        return None
    age = seconds_since(intent["last_heartbeat_at"], now)   # NULL 时为无穷大
    if intent["worker"] is None and age <= intent_timeout:
        return "released", intent["last_heartbeat_at"]
    if age > repair_lapse_seconds(intent_timeout):
        return "claim_lost", intent["last_heartbeat_at"]
    return None
```

- 只有三种情况会清空 claim：release、停止项目，以及心跳超过 `intent_timeout` 后的超时清理。因此 claim 已清空而心跳仍新于 `intent_timeout`，说明运行的任务主动释放了它（它只在 execute 之后释放），或者项目被停止了。
- 现有协议容忍心跳间隙：同一 Worker 下一次心跳就会重新认领，慢但成功的心跳也不会触发租约。因此只有间隙超过 `repair_lapse_seconds` 才把运行判为 `claim_lost`；短暂的间隙不会让运行从影子数据中消失。

### 运行如何结束

| 事件 | 路径 | `end_reason` | `last_alive_at` | `fact_id` |
| --- | --- | --- | --- | --- |
| 进程退出，Dispatcher 的第一次 `/end` | `/end` | `dispatcher` | 上报的退出时间 `now − exited_seconds_ago`，不早于 `started_at` | 若此时已结论，取 Intent 的 `to_fact_id` |
| Intent 已结论，尚无 `/end` | sweep | `concluded` | Intent 的 `concluded_at` | Intent 的 `to_fact_id` |
| 已释放（claim 已清空，心跳新于 `intent_timeout`） | sweep | `released` | Intent 的 `last_heartbeat_at` | NULL |
| 超过 `repair_lapse_seconds` 没有心跳 | sweep | `claim_lost` | Intent 的 `last_heartbeat_at` | NULL |
| 其他 Worker 持有 claim | sweep | `claim_taken` | NULL（未知） | NULL |
| 项目被停止或完成 | sweep | `project_inactive` | Intent 的 `last_heartbeat_at` | NULL |
| 同一 Worker 为该 Intent 登记了新运行 | 登记 | `superseded` | NULL（未知） | NULL |
| Intent 或项目被删除 | 现有的 `DELETE` 在自身事务中通过外键删除该行，其评审随之级联删除 | | | |
| 项目被 reopen | 无：reopen 的 `DELETE FROM intents` 同样级联，但它只删除完成边对应的 Intent，那条 Intent 从来没有运行 | | | |

- 被删除的运行立即消失：之后 Dispatcher 的 `/end` 与修复代理的评审都会得到 `404 run_not_found`，两者都视为终结。
- sweep 或 supersede 已经结束的行，之后到达的 `/end` 保留 `ended_at` 与 `end_reason`，但把 `last_alive_at` 改为上报的退出时间（以 Dispatcher 的退出时间为准）；若该窗口还没有评审行，再执行一次未评审规则，主键防止重复。
- `/end` 与重新派发的先后**不保证**：`/end` 从守护线程发出，可能重试最多 12 s，而 execute 失败会立即释放 Intent，调度器可能先重新派发它。新的登记会 supersede 旧行，旧行迟到的 `/end` 仍按上一条补上 `last_alive_at`。
- 第二次 `/end` 携带任务的结果，只填空字段：`outcome`，以及 Intent 已结论时的 `fact_id`。存入结果时也把它标到该运行的 issue 上（见「结束标记」）。超时后由 conclude 收尾产出 Fact 的运行，正是这第二次调用记录下了 Fact。

### sweep

`sweep(conn, now, agent_seen_before)` 是幂等的。它在运行接口（登记、两种形式的 `GET /repair/runs`、`GET /repair/runs/{run_id}`、评审）开始时，以及 `/end` 完成结束之后，在一个 `BEGIN IMMEDIATE` 事务中执行。现有接口从不调用它，服务端也没有后台线程。它按顺序做两件事：

1. **结束**：对每个有结束原因（见「存活判定」）的未结束行，设置 `ended_at = now`、`state = 'ended'`，以及上表中的 `end_reason`、`last_alive_at` 与 `fact_id`，然后给它的 issue 打上结束标记。
2. **窗口**：对每个未结束或未定的行，按未评审规则决定它的当前窗口。

删除已删除项目和 Intent 的行不是 sweep 的工作：现有的删除语句已通过外键在自身事务中完成（见「删除」），因此 sweep 没有孤儿清理，修复层也没有清理工具。`BEGIN IMMEDIATE` 让每个“先检查、后写入”都是原子的。`GET /repair/runs` 先提交 sweep，再在第二个短事务中读取列表，因此构建视图时不持有写锁，修复代理的每次轮询只占用写锁几毫秒。

### 窗口与评审时钟

- 时钟以服务端为准：`started_at` 及所有派生时间都使用服务端时钟（整秒）。
- Phase 2 每次运行只有一个窗口：`[started_at, deadline_at]`，`seconds = budget_seconds`，`number = 1`。（从 Phase 3 起，窗口 `w > 1` 从上一个截止时间开始，长度为一个时间片。）
- 评审在 `start + floor(0.75 × seconds)` **打开**，在 `start + floor(0.80 × seconds)` **关闭**。按时开始的评审一直接受到 `end − 10 s`（`accept_until`）。服务端拒绝 digest 时间早于“打开 − 5 s”或晚于“关闭 + 5 s”的评审，因此每条被记录的结论都做于 75%–80% 之间。
- **不可评审**：窗口短于 350 s（60 s 的模型调用加 10 s 余量必须放得进最后 20%），或 CLI 类型不在 `REVIEWABLE_CLI_TYPES = {claudecode, codex, pi}` 中（`mock` 没有会话记录）。这类运行在登记时即定为 `reviewable = 0`，从不评审，也从不标记。
- 例：预算 1800 s、10:00:00 开始的运行，10:22:30 打开，10:24:00 关闭，接受到 10:29:50。

运行视图中的 `window.review_status`：

| 状态 | 条件 |
| --- | --- |
| `pending` | 评审尚未打开 |
| `open` | 打开 ≤ now < 关闭 |
| `late` | 关闭 ≤ now < `accept_until`，尚无评审 |
| `reviewed` | 已有修复代理的评审 |
| `not_reviewed` | 已有服务端的未评审记录 |
| `not_reviewable` | `reviewable = 0` |
| `closed` | 没有记录，且运行在窗口关闭之前或在未知时间结束，或已过 `accept_until`（后者持续到下一次 sweep 记录 `not_reviewed`） |

常量（`cairn/src/cairn/server/repair.py`）：`REVIEW_OPEN_FRACTION = 0.75`、`REVIEW_CLOSE_FRACTION = 0.80`、`REVIEW_ACCEPT_MARGIN_SECONDS = 10`、`MIN_REVIEWABLE_WINDOW_SECONDS = 350`、`DIGEST_TIME_MARGIN_SECONDS = 5`、`REVIEWABLE_CLI_TYPES`、`SLICE_SECONDS = 600`、`LAPSE_MIN_SECONDS = 60`、`LAPSE_TIMEOUT_MULTIPLIER = 4`、`LISTED_STATES = frozenset({"running"})`。六个评审常量与 `SLICE_SECONDS` 以 `open_fraction`、`close_fraction`、`accept_margin_seconds`、`min_window_seconds`、`digest_time_margin_seconds`、`reviewable_cli_types` 与 `slice_seconds` 的名字出现在 `GET /repair/runs`（`review_rules` 与 `slice_seconds`）和 `GET /repair/settings`（`review` 与 `slice_seconds`）中，因此修复代理不硬编码其中任何一个。`LAPSE_MIN_SECONDS` 与 `LAPSE_TIMEOUT_MULTIPLIER` 只在服务端内部使用，不对外报告。

### 未评审规则

每个运行的当前窗口只决定一次：`settled_window` 保证一次性，`(run, window)` 主键兜底。规则按顺序检查：

1. 此刻项目开关关闭：定为已决，什么也不记录，因此关闭修复层的项目永远不会出现新的标记。
2. 该窗口已有评审，或运行不可评审：定为已决，什么也不记录。
3. 运行**存活**、`now ≥ accept_until` 且没有评审：记录**未评审**（`run_ended: false`，已用时间计到 now）。
4. 运行**已结束**、`last_alive_at ≥ 关闭时间`（80% 过去时它仍存活）且没有评审：记录**未评审**（`run_ended: true`，已用时间计到 `last_alive_at`）。
5. 运行在窗口关闭之前结束，或结束时间未知：定为已决，什么也不记录。在 78% 时自行结束的运行并没有撞上时限。
6. 其余情况（`accept_until` 之前仍存活的运行）：暂不决定。

记录时用 `INSERT … ON CONFLICT (run_id, window_no) DO NOTHING` 插入一条 `repair_reviews` 行（`source='server'`、`review_id='server'`、`decision='not_reviewed'`）；只有确实插入了这一行，才插入对应的 `intent_issues` 行，并通过 `issue_id` 关联。对已结束的运行（规则 4），这条 issue 在插入时就带上结束标记：`run_ended_at`、`end_reason`，以及运行已有结果时的 `outcome`。日志：`repair window not reviewed run=%s project=%s intent=%s window=%s why=%s`。

`why` 为 `agent_seen`，当修复代理最近一次默认轮询 `GET /repair/runs` 发生在窗口打开之时或之后；否则为 `agent_not_seen`。轮询时间保存在内存中（单个服务端进程）。`GET /repair/runs` 用上一次的轮询时间做 sweep，之后才记录本次轮询，因此修复代理停机后重新上线，不会让停机期间看起来像 `agent_seen`。评估读取不记录轮询。

记录出现的时机：修复代理在线时，在 `accept_until` 之后一个轮询周期（约 5 s）之内；修复代理离线而 Dispatcher 正常时，在该运行的 `/end` 时。它先出现在仍未结论的 Intent 节点上；若今天的 conclude 收尾产出了 Fact，则随 issue 一起移到 Fact 节点上。

### 结束标记

运行结束时（任何路径），服务端在同一事务中更新所有 `run_id` 相同、`kind = 'repair_review'` 的 `intent_issues` 行：在 `detail` 中加入 `run_ended_at`（行的 `last_alive_at`，没有时取 `ended_at`）、`end_reason`，以及运行已有结果时的 `outcome`，并更新 `updated_at`。之后的 `/end` 修正 `last_alive_at` 或存入结果时会重新标记。界面据此标出属于更早一次尝试的评审。只有修复代码做这件事，也只针对 `repair_review` issue，而从未打开修复层的项目没有这类 issue。

### 角色令牌

主线上所有接口都没有认证。修复层只为新的机器接口加上角色令牌：

| 接口 | 保护方式 |
| --- | --- |
| `GET /repair/runs`、`GET /repair/runs/{run_id}`、`POST /repair/runs/{run_id}/reviews` | agent 令牌 |
| `PUT /repair/runs/{run_id}`、`POST /repair/runs/{run_id}/end` | dispatcher 令牌 |
| `PUT /projects/{project_id}/repair`、`GET /repair/settings` | 开放，与主线上的其他接口相同 |
| 所有现有接口 | 开放，不变 |

- **配置**：`CAIRN_REPAIR_AGENT_TOKEN_FILE` 与 `CAIRN_REPAIR_DISPATCHER_TOKEN_FILE` 指向文件，文件内容去掉首尾的 ASCII 空白（空格、`\t`、`\n`、`\r`、`\v`、`\f`，即 `bytes.strip()` 去掉的字符）后即为令牌；`CAIRN_REPAIR_AGENT_TOKEN` 与 `CAIRN_REPAIR_DISPATCHER_TOKEN` 直接给出令牌值（同样去掉首尾的 ASCII 空白）。其他空白字符（0x1C–0x1F、U+0085、U+00A0 等 `str.strip()` 也会去掉的字符）不去掉，随后的可打印 ASCII 检查会拒绝它们。服务端不是任何 Worker 的父进程，两种方式在服务端都是安全的；运行手册使用文件。它们在应用的 lifespan 中读取一次（`cairn serve` 经由 uvicorn 运行 lifespan）。
- **校验**：启动时按顺序检查每个角色的令牌，第一个不满足的条件拒绝该令牌，以 WARNING 记录 `repair token rejected role=%s reason=%s`，并让该角色保持未配置：文件不可读（`reason=file_unreadable`）；不是可打印 ASCII，即 `value.isascii() and value.isprintable()` 为假，只有 0x20–0x7E 的字符可以通过（`reason=not_ascii`；Dispatcher 与修复代理对各自的令牌文件使用同一规则：同样只去掉首尾的 ASCII 空白，再做同样的检查，因此三者对同一个令牌文件的结论相同，例如以 0x1F 或 U+00A0 结尾的文件在三处都是 `not_ascii`）；少于 32 个字符（`reason=shorter_than_32`）。随后，两个令牌相同时两者都被拒绝（`reason=identical`）。服务端总能启动，并以 INFO 记录 `repair tokens configured agent=%s dispatcher=%s`（只含布尔值）。这一行只在运维方配置了日志时可见，因此运行手册改用 `GET /repair/settings` 的 `roles_configured` 检查。
- **检查**：FastAPI 依赖 `require_agent(endpoint)` 与 `require_dispatcher(endpoint)` 读取请求头 `X-Cairn-Repair-Token`，用 `hmac.compare_digest` 比较它的 UTF-8 字节（`header.encode("utf-8")`）与令牌的字节；因此非 ASCII 的请求头只是一次不匹配（401），而不是 500。角色未配置：`503 token_not_configured`（失败即关闭）。缺少令牌、令牌错误或使用了另一个角色的令牌：`401 unauthorized`，以 WARNING 记录 `repair auth failed role=%s endpoint=%s`，每个角色任意 60 s 内至多一行（在内存中计算，每个服务端进程独立）。修复路由在 FastAPI 读取请求体之前先做这项检查，因此被拒绝的请求总是得到带 code 的 401 或 503，其请求体不会被读取。令牌的值从不写入日志。
- **接口名**：`endpoint` 是路由的固定名称，由每个路由传给它的依赖（`Depends(require_agent("runs.list"))`）：`runs.list`（`GET /repair/runs`）、`runs.get`（`GET /repair/runs/{run_id}`）、`runs.review`（`POST /repair/runs/{run_id}/reviews`）、`runs.register`（`PUT /repair/runs/{run_id}`）、`runs.end`（`POST /repair/runs/{run_id}/end`）。它从不是请求路径或路径参数：依赖在任何检查之前运行，而路径与 `run_id` 参数会保留解码后的 `%0A` 换行，否则没有令牌的调用方就能向服务端日志写入伪造的行。
- **dev-0.3.0 移植**：该分支的路由声明 `dependencies=[Depends(require_auth)]`，其 API key 不带权限范围。移植时修复路由在 `require_auth` 之上保留角色检查：Dispatcher 像该分支的 `CairnClient` 一样发送 `X-API-Key`，修复代理发送其配置给出的 key。

### 删除

修复数据与 Cairn 自身的数据一样，随项目一起删除。`repair_runs` 的外键与 `intent_sources`、`intent_issues` 的外键形式相同，而每个服务端连接都设置了 `PRAGMA foreign_keys=ON`，因此：

- 现有的 `DELETE /projects/{project_id}`（`DELETE FROM projects`，级联到项目的所有 Intent）在同一语句、同一事务中删除这些 Intent 的所有运行，并通过 `repair_reviews` 的外键删除这些运行的评审，包括 digest 与模型原始输出。不运行任何修复代码，修复层停止期间同样如此。
- reopen 的 `DELETE FROM intents` 同样会级联，但它只删除完成边对应的 Intent，而登记会拒绝已结论的 Intent，因此那条 Intent 从来没有运行。
- 这是“现有接口不触碰修复表”的唯一例外。从未打开修复层的项目没有运行（登记会拒绝开关关闭的项目），因此对它们来说，级联什么也不删除。
- 回滚到修复层之前的服务端版本后仍然级联：外键属于数据库文件中的表，旧版本的连接同样设置了该 pragma。
- 外键必须正是这个形式：引用的列不是 `intents` 的主键时，SQLite 会以 `foreign key mismatch` 拒绝上面两种删除（即使没有任何修复数据，也会变成 500）；缺少 `ON DELETE CASCADE` 时，删除一个有运行的项目会以 `FOREIGN KEY constraint failed` 失败。`repair_reviews` 的外键同理，经第二层级联到达。
- 会话记录副本不在服务端：`dispatch.yaml` 含 `repair:` 部分时，Dispatcher 会另行删除被删除项目的副本（见 `docs/specs/dispatcher-design.md`）。

### 错误格式与约定

- 修复代码返回的错误统一为 `{"detail": {"code": "<code>", "message": "<text>", ...}}`，包括令牌检查的 401 与 503、带 code 的 422（`bad_run_id`、`bad_started_seconds_ago`、`bad_cursor`），以及查找项目与 Intent 时的 403 与 404（`project_inactive`、`project_not_found`、`intent_not_found`）。客户端因此能把修复层的回答与 FastAPI 自己对未知路由的 `{"detail": "Not Found"}` 区分开。请求未通过 pydantic 模型校验时，保持 FastAPI 的 422 格式 `{"detail": [{"loc": …, "msg": …, "type": …}]}`。服务端没有增加应用级的异常处理器，因此现有接口的 422 响应体不变。面向界面的 `PUT /projects/{project_id}/repair` 保持纯字符串的 `detail`，因为界面只显示字符串或列表形式的 detail。
- 所有 id 规则都是整串匹配（Python 中用 `re.fullmatch`，请求模型用 pydantic 的 `pattern`），以换行结尾的 id 同样会被拒绝。
- 修复请求体中的数字必须是有限值：`NaN` 与 `Infinity` 得到 422。每个参与时间运算的数字都以 604800（最大的预算）为上限。
- 请求模型忽略未知字段，因此较新的客户端也能对接这个服务端。时间格式为服务端时钟的 `YYYY-MM-DDTHH:MM:SSZ`。
- 调用方把任何非 2xx 都视为“记日志，然后照今天的方式继续”。Phase 2 中没有任何运行依赖修复调用。

### 日志

服务端不配置日志：`cairn serve` 只调用 `db.configure` 与 `uvicorn.run`，uvicorn 只配置它自己的 logger。因此 `cairn.server.*` 的记录只有 WARNING 及以上才会显示，由 Python 的兜底处理器以裸消息（没有级别、时间或 logger 名）输出到 stderr；INFO 记录只在运维方自行配置日志时出现。修复层不调用 `logging.basicConfig` 或 `logging.config.dictConfig`，不添加任何 handler，也不修改 `cli.py`，所以运维方必须看到的那几行都是 WARNING。

服务端修复日志的完整列表。`server/repair.py`、`server/routers/repair.py` 以及 `routers/projects.py` 中的开关行使用 logger `cairn.server.repair`；`server/repair_auth.py` 使用 `cairn.server.repair_auth`。

| 级别 | Logger | 日志行 | 出处 |
| --- | --- | --- | --- |
| WARNING | `cairn.server.repair` | `repair switch project=%s enabled=%s client=%s` | `PUT /projects/{project_id}/repair`，以及 `repair_enabled` 为 true 的 `POST /projects` |
| WARNING | `cairn.server.repair_auth` | `repair token rejected role=%s reason=%s` | 启动时；reason 为 `file_unreadable`、`not_ascii`、`shorter_than_32` 或 `identical` |
| WARNING | `cairn.server.repair_auth` | `repair auth failed role=%s endpoint=%s` | 每个角色任意 60 s 内至多一行 |
| INFO | `cairn.server.repair_auth` | `repair tokens configured agent=%s dispatcher=%s` | 启动时 |
| INFO | `cairn.server.repair` | `repair run registered run=%s project=%s intent=%s worker=%s cli=%s execution=%s budget=%ss session=%s` | 登记；session 为 `yes` 或 `no`，从不打印 id |
| INFO | `cairn.server.repair` | `repair run updated run=%s fields=%s` | 已知运行的登记补上了字段 |
| INFO | `cairn.server.repair` | `repair run superseded run=%s by=%s project=%s intent=%s worker=%s` | 登记 |
| INFO | `cairn.server.repair` | `repair run ended run=%s project=%s intent=%s reason=%s outcome=%s` | `/end` |
| INFO | `cairn.server.repair` | `repair window not reviewed run=%s project=%s intent=%s window=%s why=%s` | 未评审规则 |
| INFO | `cairn.server.repair` | `repair review recorded run=%s project=%s intent=%s window=%s decision=%s fallback=%s mode=shadow applied=false` | 评审 |
| INFO | `cairn.server.repair` | `repair review refused run=%s code=%s` | 评审的每个 409 |

共同规则：日志记录运行、项目、Intent 与 Worker 的 id；从不记录令牌、会话 id、digest、模型原始输出与评审文本；自由文本的错误被替换为异常的类名。日志行中不出现请求路径，也不出现认证与校验之前收到的任何值：id 只在匹配其规则或在数据库中找到之后才记录，认证失败只给出固定的接口名，`client=` 只以 `ipaddress.ip_address()` 接受的十六进制数字、冒号与点打印，从不带 IPv6 的 scope id。每一行都有固定前缀 `repair `，便于过滤，且没有一行匹配 Phase 0 日志解析器的模式。

---

## 接口列表

### 全局设置

#### GET /settings

返回全局配置。

响应：

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

#### PUT /settings

更新全局配置。

Body：

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

### Projects

#### GET /projects

返回所有项目列表，含元信息和统计摘要，不含图数据。

响应：

```json
[
  {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "bootstrap_enabled": true,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    },
    "fact_count": 8,
    "intent_count": 5,
    "working_intent_count": 2,
    "unclaimed_intent_count": 1,
    "hint_count": 3
  }
]
```

---

#### POST /projects

创建新项目。`origin` 和 `goal` 写入 `facts` 作为特殊 Fact。`hints` 可选。`bootstrap_enabled` 可选，默认为 `true`；为 `false` 时消费者跳过 bootstrap。即使为 `true`，消费者没有 bootstrap 能力时也可直接进入 reason。

Body：

```json
{
  "title": "xx渗透测试",
  "origin": "目标 http://192.168.1.10",
  "goal": "拿到 flag",
  "bootstrap_enabled": true,
  "hints": [
    { "content": "优先看 web 服务", "creator": "human" },
    { "content": "注意 80 端口", "creator": "human" }
  ]
}
```

`repair_enabled` 可选，默认为 `false`，是普通的布尔值（`"sometimes"` 之类的值返回 422，与 `bootstrap_enabled` 相同）。为 `true` 时，新项目创建时即打开修复层开关（见「修复层（影子模式）」）：服务端在同一事务中写入开关，提交后以 WARNING 记录 `repair switch project=%s enabled=%s client=%s`。新建项目对话框的 “Repair long explores” 复选框就是这样设置开关的。不带该字段或为 `false` 时，请求与执行的 SQL 都与之前完全相同，也不记日志；响应只多出附加字段 `"repair_enabled": false`。

---

#### GET /projects/{project_id}

返回项目完整数据，包含 facts、intents、hints。

响应：

```json
{
  "project": {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "bootstrap_enabled": true,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    }
  },
  "facts": [
    { "id": "origin", "description": "目标 http://192.168.1.10" },
    { "id": "goal",   "description": "拿到 flag" },
    { "id": "f001",   "description": "发现开放端口 22/80/443" }
  ],
  "intents": [
    {
      "id": "i001",
      "from": ["origin"],
      "to": "f001",
      "description": "nmap 全端口扫描",
      "creator": "agent-A",
      "worker": "agent-A",
      "last_heartbeat_at": "2026-03-21T10:02:00Z",
      "created_at": "2026-03-21T10:01:00Z",
      "concluded_at": "2026-03-21T10:02:00Z"
    },
    {
      "id": "i002",
      "from": ["f001"],
      "to": null,
      "description": "http 服务探测与目录扫描",
      "creator": "agent-A",
      "worker": "agent-B",
      "last_heartbeat_at": "2026-03-21T10:04:00Z",
      "created_at": "2026-03-21T10:03:00Z",
      "concluded_at": null
    }
  ],
  "hints": [
    { "id": "h001", "content": "优先看 web 服务", "creator": "human", "created_at": "2026-03-21T10:00:00Z" },
    { "id": "h002", "content": "注意 80 端口", "creator": "human", "created_at": "2026-03-21T10:00:00Z" }
  ]
}
```

---

#### DELETE /projects/{project_id}

删除项目及其所有数据。

如果某个 Dispatcher 仍持有该项目对应的运行容器，容器不由 Server 直接管理；Dispatcher 在后续轮询中发现项目已不存在后，会把该项目视为 `deleted`，取消本地仍在运行的任务，并删除对应的 orphan 容器。

项目的修复数据（运行、评审，以及评审中保存的 digest 与模型原始输出）随项目一起删除，在同一语句、同一事务中完成：`DELETE FROM projects` 级联删除项目的 Intent，再经 `repair_runs` 与 `repair_reviews` 的外键（`ON DELETE CASCADE`）删除这些 Intent 的运行及其评审，不运行任何修复代码（见「修复层（影子模式）」›「删除」）。从未打开修复层的项目没有修复数据，删除结果与之前相同。会话记录的副本不在 Server 上：若 Dispatcher 的 `dispatch.yaml` 含 `repair:` 部分，它会在约一分钟内（Dispatcher 当时未运行，则在其下次启动时）删除该项目的副本目录。

---

#### PUT /projects/{project_id}/title

修改项目标题。该操作不改变项目图，也不影响 `reason` lease、open intent claim 或项目状态；无论项目当前是 `active`、`stopped` 还是 `completed`，都允许修改标题。

Body：

```json
{
  "title": "xx渗透测试（复盘）"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试（复盘）",
  "status": "completed",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### PUT /projects/{project_id}/status

更新项目状态。仅允许 `active` 和 `stopped` 之间切换。`completed` 项目不可通过该接口再次变更状态；若需要撤销完成态，必须调用专用的 `reopen` 接口。该接口属于项目管理操作，不属于探索写操作。

当项目切到 `stopped` 时，Server 会立即把所有尚无结论的 Intent 的 `worker` 清空为 `null`，并把 `project.reason` 清空，使这些 claim 立刻失效。这样项目恢复后可以马上重新认领，不必等待超时。`stopped` 的语义是硬停止：Server 负责拒绝后续探索写操作并清空 open intent claim / reason lease；消费者拿到这个信号后应立刻取消本地仍在运行的任务，并停止对应项目容器。

已知问题：当前协议里，Intent 级只保存“当前 claim 持有者”这一份信息，也就是 `intent.worker`。因此项目一旦被切到 `stopped`，这些 open intent 的 `worker` 会被立即清空；停止后从项目详情里将无法直接看出“该 intent 在停止前最后是由哪个 worker 在推进”。后续可以考虑增加类似 `worker_history` 的 Intent 级历史字段来保留这部分可见性，但当前版本尚未实现。

Body：

```json
{
  "status": "stopped"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试",
  "status": "stopped",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### POST /projects/{project_id}/reason/claim

认领项目级 `reason` lease。它不属于图，不产生 Intent/Fact，只表示“当前有人正在对整个项目做一次 `reason` 判断”。单个项目同一时刻最多只能有一个 `reason` lease。

若当前 `project.reason` 为空，则 claim 成功并写入 `worker`、`trigger`、`started_at`、`last_heartbeat_at`。若当前已被其他消费者占用且未超时，返回 409。若当前已被同一 `worker` 占用，则幂等返回当前状态。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A",
  "trigger": "new_facts"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试",
  "status": "active",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": {
    "worker": "dispatcher-worker-A",
    "trigger": "new_facts",
    "started_at": "2026-03-21T10:06:00Z",
    "last_heartbeat_at": "2026-03-21T10:06:00Z"
  }
}
```

---

#### POST /projects/{project_id}/reason/heartbeat

当前 `worker` 对项目级 `reason` lease 续约，更新 `last_heartbeat_at`。仅当前持有该 lease 的 `worker` 可以续约；若当前无人持有，返回 409；若被其他消费者占用且未超时，返回 409。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

#### POST /projects/{project_id}/reason/release

当前 `worker` 主动释放项目级 `reason` lease，使其立刻回到无人执行 `reason` 的状态。仅当前持有者本人可以释放；若当前已为空，则幂等返回当前状态；若被其他消费者占用且未超时，返回 409。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

### Hints

#### POST /projects/{project_id}/hints

向项目补充一条策略提示。Hint 属于图外输入写操作，`active`、`stopped` 和 `completed` 项目均允许。

Body：

```json
{
  "content": "尝试文件上传绕过",
  "creator": "agent-A"
}
```

---

### Intents

#### POST /projects/{project_id}/intents

从某个 Fact 出发声明探索意图，产生一条尚无结论的 Intent。可选择只声明，或声明并同时认领。Intent 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "from": ["f001"],
  "description": "http 服务探测与目录扫描",
  "creator": "agent-A",
  "worker": null
}
```

`from` 为数组，至少包含一个 Fact id。`worker` 可为 `null` 或等于 `creator`；`null` 表示只声明不认领，等于 `creator` 表示声明并立即认领。多个 Fact 共同驱动同一次探索时，全部列入 `from`，完整保留因果关系。例如：

`from` 不能包含 `goal`。

服务端不校验 `description` 的业务语义，因此像 `description = "bootstrap"` 这样的保留 intent 也只是普通 Intent。是否把它解释为“项目最初阶段的直接推进尝试”，由消费者自己约定。

```json
{
  "from": ["f002", "f004"],
  "description": "用 f004 获取的凭据登录 f002 发现的后台",
  "creator": "agent-A",
  "worker": "agent-A"
}
```

响应：新创建的 Intent 对象。

---

#### POST /projects/{project_id}/intents/{intent_id}/heartbeat

消费者上报存活，更新 `worker` 和 `last_heartbeat_at`。仅对尚无结论的 Intent 有效。该接口用于续约，或在某条 Intent 已超时变为未认领后重新接手。若当前 `worker` 为 `null`，任意消费者可通过 heartbeat 认领；若当前 `worker` 已被其他消费者占用且未超时，返回 409。Heartbeat 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

消费者通常会按固定心跳周期继续 heartbeat 或轮询项目；项目进入 `stopped` 或 `completed` 后，继续 heartbeat 会返回 403，而轮询读取会看到项目 `status` 变化。由于 Server 在 `stopped` 时会立即清空 open intent 的 `worker`，消费者应把它当作硬停止信号，尽快终止本地推进，而不是继续依赖旧 claim。

Body：

```json
{
  "worker": "agent-B"
}
```

---

#### POST /projects/{project_id}/intents/{intent_id}/release

当前 `worker` 主动释放一条尚无结论的 Intent，使其回到未认领状态，供其他消费者立即接手。仅当前 `worker` 本人可以释放；若被其他消费者占用且未超时，返回 409。若该 Intent 当前已未认领，则幂等返回当前状态。`last_heartbeat_at` 保留，不清空。Release 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "agent-B"
}
```

响应：更新后的 Intent 对象。

---

#### POST /projects/{project_id}/intents/{intent_id}/conclude

探索结束，产出新 Fact 并将 Intent 结论落定。原子操作。仅对尚无结论的 Intent 有效。claim 规则与 heartbeat 相同：未认领可直接 conclude；由当前 `worker` conclude；若被其他消费者占用且未超时，返回 409。Conclude 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

`worker` 写入后永久保留，标识本次结论由谁产出。

Body：

```json
{
  "worker": "agent-B",
  "description": "80端口运行 nginx 1.18，存在目录遍历"
}
```

可选字段 `parse_repair`：消费者在 conclude 输出经 JSON 修复才解析成功时附带，例如 `{"strategy": "envelope_repair", "error": "accepted must be true or false"}`。服务端在 Fact 与结论提交之后，用单独的短事务把它记录为该 Intent 的一条 `json_repaired` issue（`error` 截断到 300 字符）。该字段只作记录，不影响结论：值无法识别时直接忽略，不返回 422；记录失败只写日志，已提交的 Fact 与结论不受影响（响应中的 `intent.issues` 此时不含这条记录）。不带该字段时行为与之前完全相同。

响应：

```json
{
  "fact": {
    "id": "f002",
    "description": "80端口运行 nginx 1.18，存在目录遍历"
  },
  "intent": {
    "id": "i002",
    "from": ["f001"],
    "to": "f002",
    "description": "http 服务探测与目录扫描",
    "creator": "agent-A",
    "worker": "agent-B",
    "last_heartbeat_at": "2026-03-21T10:05:00Z",
    "created_at": "2026-03-21T10:03:00Z",
    "concluded_at": "2026-03-21T10:05:00Z",
    "issues": []
  }
}
```

---

#### POST /projects/{project_id}/complete

消费者判断某些 Facts 共同满足 goal 后调用，声明项目完成。原子操作。Complete 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

创建一条 `from` 指向所列 Facts、`to` 指向 `goal` 的已结论 Intent。完成声明无探索过程，服务端将 `creator` 和 `worker` 均设为请求中的 `worker` 值。Project 状态变为 `completed`，并立即清空当前 `project.reason`。

如果后续外部验证发现这次完成判断有误，可再调用 `POST /projects/{project_id}/reopen` 撤销这条完成边，并把纠错信息写成新的 Fact 后继续探索。

`from` 不能包含 `goal`。

Body：

```json
{
  "from": ["f007", "f012", "f015"],
  "description": "三个 shell 均已获取，满足 goal 要求",
  "worker": "agent-A"
}
```

---

#### POST /projects/{project_id}/reopen

外部确认项目其实尚未完成时，显式撤销当前完成态。该接口属于项目管理操作，不属于探索写操作；仅 `completed` 项目允许调用。

服务端会：

- 找到当前唯一一条 `to=goal` 的完成边
- 读取它原本的 `from`
- 删除这条完成边，不保留“曾完成过一次”的图内历史
- 新建一个普通 Fact，内容为调用者提供的客观纠错说明
- 新建一条已结论 Intent，`description` 固定为 `external_feedback`，其 `from` 继承原完成边的 `from`，`to` 指向这个新 Fact
- 将 `creator` 和 `worker` 都写为请求中的 `creator`
- 将项目状态改回 `active`
- 清空当前 `project.reason`

这适合诸如“提交的 flag 是错的，需要继续寻找正确 flag”这类图外反馈。反馈本身被记成图中的新 Fact，而 `reopen` 只是触发这次改写的控制动作。

Body：

```json
{
  "description": "flag FLAG{fake} 是错误的，需要继续寻找正确 flag",
  "creator": "judge"
}
```

响应：

```json
{
  "project": {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "created_at": "2026-03-21T10:00:00Z",
    "reason": null
  },
  "fact": {
    "id": "f016",
    "description": "flag FLAG{fake} 是错误的，需要继续寻找正确 flag"
  },
  "intent": {
    "id": "i021",
    "from": ["f008"],
    "to": "f016",
    "description": "external_feedback",
    "creator": "judge",
    "worker": "judge",
    "last_heartbeat_at": "2026-03-21T10:10:00Z",
    "created_at": "2026-03-21T10:10:00Z",
    "concluded_at": "2026-03-21T10:10:00Z"
  }
}
```

---

### 导出

#### GET /projects/{project_id}/export?format=yaml

返回项目图的 YAML 结构化快照，供消费者读取。不含 `last_heartbeat_at`。尚无结论的 Intent（`to=null`）出现在 YAML 中，`worker` 字段直接反映当前是否有消费者在处理。`project.reason` 不在导出中出现，因为它是项目级协调状态，不属于事实图；Intent 的 `issues` 同样不在导出中出现。读取时服务端仍会执行超时清理，因此导出结果反映的是清理后的当前状态。`created_at` 和 `concluded_at` 按服务端当前时区格式化为 `YYYY-MM-DD HH:mm:ss`。

修复层（影子模式）的任何内容都不出现在导出中：项目开关 `repair_enabled`、修复运行与评审都不导出。YAML 按名称列出项目字段（只有 `title`、`origin`、`goal` 与 `bootstrap_enabled`），timeline 不打印项目标志，`repair_review` issue 与其他 issue 一样不导出。因此打开修复层前后，同一张图导出的 YAML 与 timeline 字节相同，Worker 读到的图快照不受影响。

```yaml
project:
  title: "xx渗透测试"
  origin: "目标 http://192.168.1.10"
  goal: "拿到 flag"
  bootstrap_enabled: true

hints:
  - content: "优先看 web 服务"
    creator: "human"
    created_at: "2026-03-21 18:00:00"
  - content: "注意 80 端口"
    creator: "human"
    created_at: "2026-03-21 18:00:00"

facts:
  - id: origin
    description: "目标 http://192.168.1.10"
  - id: goal
    description: "拿到 flag"
  - id: f001
    description: "发现开放端口 22/80/443"
  - id: f002
    description: "80端口运行 nginx 1.18，存在目录遍历"
  - id: f003
    description: "遍历发现 /backup/db.sql，包含用户表"
  - id: f004
    description: "获得凭据 admin/passwd123"
  - id: f005
    description: "ssh 登录失败，凭据不通用"
  - id: f006
    description: "web后台登录成功，发现文件上传功能"
  - id: f007
    description: "上传 webshell 成功，获得 www-data shell"
  - id: f008
    description: "在 /var/www/html/flag.txt 找到 flag{abc123}"

intents:
  - from: [origin]
    to: f001
    description: "nmap 全端口扫描"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:01:00"
    concluded_at: "2026-03-21 18:02:00"

  - from: [f001]
    to: f002
    description: "http 服务探测与目录扫描"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:02:00"
    concluded_at: "2026-03-21 18:04:00"

  - from: [f002]
    to: f003
    description: "利用目录遍历下载 /backup/db.sql"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:04:00"
    concluded_at: "2026-03-21 18:05:00"

  - from: [f003]
    to: f004
    description: "解析 db.sql 提取用户凭据"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:05:00"
    concluded_at: "2026-03-21 18:05:30"

  - from: [f004]
    to: f005
    description: "尝试 ssh 登录"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f002, f004]
    to: f006
    description: "用 f004 获取的凭据登录 f002 发现的后台"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f006]
    to: f007
    description: "上传 php webshell 绕过扩展名检测"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:06:00"
    concluded_at: "2026-03-21 18:07:00"

  - from: [f007]
    to: f008
    description: "在 shell 中搜索 flag 文件"
    creator: "agent-B"
    worker: "agent-A"
    created_at: "2026-03-21 18:07:00"
    concluded_at: "2026-03-21 18:08:00"

  - from: [f008]
    to: goal
    description: "flag{abc123} 满足 goal 要求"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:08:00"
    concluded_at: "2026-03-21 18:08:01"
```

尚无结论的 Intent 示例（`worker` 不为 null 表示有消费者正在处理）：

```yaml
  - from: [f003]
    to: null
    description: "尝试利用目录遍历读取敏感文件"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:09:00"
    concluded_at: null
```

---

#### GET /projects/{project_id}/export?format=timeline

返回项目的时间线纯文本，按事件发生时间排序。YAML 快照展示图的结构拓扑，timeline 展示事件的先后顺序和因果链，两者互补。事件类型包括 `PROJECT CREATED`、`HINT`、`INTENT DECLARED`、`INTENT CONCLUDED`、`PROJECT COMPLETED`。若项目曾经完成后又被 `reopen`，由于旧的 `goal` 边已被撤销，timeline 中不会保留之前那次 `PROJECT COMPLETED` 事件。

---

### 修复层（影子模式）

修复层的机器接口与开关，概念见上文「修复层（影子模式）」。运行接口要求请求头 `X-Cairn-Repair-Token` 携带对应角色的令牌：登记与结束用 dispatcher 令牌，列表、读取与评审用 agent 令牌；`PUT /projects/{project_id}/repair` 与 `GET /repair/settings` 是开放的。令牌检查对每个运行接口都相同：角色未配置返回 `503 token_not_configured`，缺少令牌、令牌错误或用了另一个角色的令牌返回 `401 unauthorized`（见「角色令牌」）。每个运行接口都在一个 `BEGIN IMMEDIATE` 事务中先执行 sweep（`/end` 在结束之后执行），因此即使请求被拒绝，sweep 的结果也已提交。错误格式见「错误格式与约定」。

#### PUT /repair/runs/{run_id}

登记一次运行（dispatcher 令牌）。`run_id` 必须整串匹配 `^[A-Za-z0-9][A-Za-z0-9_.-]{7,63}$`（`re.fullmatch`），否则返回 `422 bad_run_id`；Dispatcher 使用 `uuid4().hex`。

Body（`RunRegisterRequest`）：

```json
{
  "project_id": "proj_027",
  "intent_id": "i014",
  "worker": "codex-1",
  "cli_type": "codex",
  "execution": "container",
  "budget_seconds": 1800,
  "session_id": null,
  "started_seconds_ago": 0.004,
  "locator": {
    "container": "cairn-dispatch-proj_027",
    "prompt_marker": "/tmp/cairn-prompts/explore_execute-0a1b2c3d4e5f/graph.yaml",
    "dispatcher_started_at": "2026-11-14T10:00:00.123Z"
  }
}
```

| 字段 | 规则（不满足时返回 422） |
| --- | --- |
| `project_id`、`intent_id`、`worker` | 1–128 个字符 |
| `cli_type` | `^[a-z0-9_-]{1,32}$` |
| `execution` | `container` 或 `local` |
| `budget_seconds` | 整数，1–604800 |
| `session_id` | null，或匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` 且不含 `..`（Dispatcher、服务端与副本共用这一条规则） |
| `started_seconds_ago` | 数字 ≥ 0，默认 0。只在本次 PUT 创建该行时使用：此时不得大于 `budget_seconds`，否则返回 `422 bad_started_seconds_ago`，且在用它计算任何值之前检查；`started_at = 收到时间 − round(value)`。对已知的 `run_id` 忽略该字段、也不检查范围，因为超时运行的会话 id 补登会在预算耗尽之后才到达。Dispatcher 每次尝试登记时都用单调时钟重新计算它，因此重试的登记仍保持准确的时间 |
| `locator` | 至多 8 个键的对象，键至多 64 个字符，值为至多 1024 个字符的字符串或 null。原样保存、原样返回，从不解释；修复代理忽略它 |

处理，在一个 `BEGIN IMMEDIATE` 事务中：

1. sweep。
2. **已知的 `run_id`**：`project_id`、`intent_id`、`worker`、`cli_type`、`execution` 或 `budget_seconds` 与已存的值不同，返回 `409 run_conflict` 并附 `fields`（因此第二次登记永远不能提高预算）；`session_id` 与已存的不同，返回 `409 session_conflict`。忽略 `started_seconds_ago`。否则在 `session_id` 为空时补上它、在 `locator_json` 为 `{}` 时补上 locator（日志 `repair run updated run=%s fields=%s`），返回 **200** `{"created": false, "run": RunView}`。对已结束的行同样有效，因此只在结束时才得知的会话 id（如 local 模式）也能记录下来，超时之后亦然。
3. **新的 `run_id`**，按顺序：
   - `started_seconds_ago` 大于 `budget_seconds`：`422 bad_started_seconds_ago`；
   - 项目不存在：`404 project_not_found`；项目不是 `active`：`403 project_inactive`；项目开关关闭：`409 repair_disabled`；
   - Intent 不存在：`404 intent_not_found`；Intent 已结论（`to_fact_id` 已设置）：`409 intent_concluded`；
   - 是 bootstrap intent（按调度器的四条件判断：description 为 `bootstrap`、creator 为 `dispatcher.bootstrap`、sources 恰为 `["origin"]`、尚未结论）：`409 not_explore`；
   - Intent 的 `worker` 既不等于 `body.worker` 也不为 NULL，或其心跳早于 `repair_lapse_seconds`：`409 not_claimed`。登记只读取 claim，从不执行超时清理，也从不写 Intent。调度器在提交任务之前已经 claim，因此正常的登记总能通过；
   - 以 `superseded` 结束同一 (project, intent, worker) 的未结束行，日志 `repair run superseded run=%s by=%s project=%s intent=%s worker=%s`；
   - 插入：`version 1`、`slices_used 0`、`state 'running'`、`deadline_at = started_at + budget`，按窗口规则设置 `reviewable`，`settled_window = 0`（不可评审时为 1）；
   - 日志 `repair run registered run=%s project=%s intent=%s worker=%s cli=%s execution=%s budget=%ss session=%s`，session 为 `yes` 或 `no`，从不打印 id；
   - 返回 **201** `{"created": true, "run": RunView}`。

错误：`401 unauthorized`、`503 token_not_configured`；修复代码的 `422 bad_run_id` 或 `422 bad_started_seconds_ago`；请求体未通过 `RunRegisterRequest` 时为 FastAPI 格式的 422；以及上面列出的 403、404 与 409。

---

#### POST /repair/runs/{run_id}/end

结束一次运行（dispatcher 令牌）。Dispatcher 发送两次：execute 进程退出时，以及任务返回之后。

退出时：

```json
{"timed_out": true, "cancelled": false, "returncode": 124, "exited_seconds_ago": 0.2}
```

任务返回之后：

```json
{"outcome": "success"}
```

| 字段 | 规则 |
| --- | --- |
| `outcome` | 可选，`^[a-z_]{1,32}$`。今天的取值为 `success`、`failed`、`cancelled`、`rejected`、`unhealthy`（`run_explore_task` 的返回值）；其他值按原样保存 |
| `timed_out`、`cancelled` | 可选的布尔值；`timed_out` 即今天的 `did_timeout(result)` |
| `returncode` | 可选的整数 |
| `exited_seconds_ago` | 可选的数字，0–604800，每次尝试都重新计算 |

处理：运行不存在时返回 `404 run_not_found`，包括已随 Intent 或项目删除的运行，以及 Intent 或项目已不存在的运行。未结束的行上的第一次 `/end`：`ended_at = now`、`end_reason = 'dispatcher'`、`last_alive_at = max(now − exited_seconds_ago, started_at)`、上报的字段，以及 Intent 已结论时的 `fact_id`，然后打上结束标记。已被 sweep 或 supersede 结束的行：用退出时间修正 `last_alive_at`，并再执行一次未评审规则（见「运行如何结束」）。之后的调用只填空字段；存入 `outcome` 的那次调用同时把它标到运行的 issue 上。然后执行 sweep，返回 200 `{"run": RunView}`。`/end` 不检查开关。日志 `repair run ended run=%s project=%s intent=%s reason=%s outcome=%s`。

---

#### GET /repair/runs

列出运行（agent 令牌）。

| 参数 | 含义 |
| --- | --- |
| `project_id` | 可选的过滤条件 |
| `include_ended` | 默认 false。为 true 时：所有运行（给出 `project_id` 时只含该项目），包括已结束的，按登记时间从新到旧，供评估工具使用 |
| `limit` | 1–500，默认 200 |
| `cursor` | 只能与 `include_ended` 一起使用：上一页的 `next_cursor` |

默认列表包含开关打开的 `active` 项目中的存活运行，按 `started_at` 从早到晚，并且只包含 `state` 属于 `LISTED_STATES = frozenset({"running"})` 的行。Phase 2 中每个未结束的行都处于 `running`，因此这就是全部存活运行；这个常量使 Phase 4 无需改写列表逻辑，就能让保持存活的脱离运行不进入评审。

响应：

```json
{
  "server_time": "2026-11-14T10:22:41Z",
  "shadow": true,
  "slice_seconds": 600,
  "review_rules": {"open_fraction": 0.75, "close_fraction": 0.8, "accept_margin_seconds": 10,
                   "min_window_seconds": 350, "digest_time_margin_seconds": 5,
                   "reviewable_cli_types": ["claudecode", "codex", "pi"]},
  "runs": [RunView],
  "next_cursor": null
}
```

`RunView`，登记、`/end` 与 `GET /repair/runs/{run_id}` 也返回它：

```json
{
  "run_id": "5f2c9a1e7b3d4c0a9e8d7c6b5a493827",
  "project_id": "proj_027", "intent_id": "i014",
  "intent": {"description": "Extract the squashfs root and list its configuration files", "from": ["f003"]},
  "worker": "codex-1", "cli_type": "codex", "execution": "container",
  "session_id": "0199a1b2-6a3f-7f0e-9a51-2a7c8f1d9e44",
  "locator": {"container": "cairn-dispatch-proj_027", "prompt_marker": "/tmp/cairn-prompts/explore_execute-0a1b2c3d4e5f/graph.yaml", "dispatcher_started_at": "2026-11-14T10:00:00.123Z"},
  "budget_seconds": 1800, "started_at": "2026-11-14T10:00:00Z", "deadline_at": "2026-11-14T10:30:00Z",
  "version": 1, "slices_used": 0, "state": "running",
  "elapsed_seconds": 1361, "remaining_seconds": 439,
  "window": {"number": 1, "start_at": "2026-11-14T10:00:00Z", "end_at": "2026-11-14T10:30:00Z", "seconds": 1800,
             "review_opens_at": "2026-11-14T10:22:30Z", "review_closes_at": "2026-11-14T10:24:00Z",
             "review_accept_until": "2026-11-14T10:29:50Z", "reviewable": true, "review_status": "open"},
  "reviews": [],
  "registered_at": "2026-11-14T10:00:00Z", "updated_at": "2026-11-14T10:00:01Z",
  "ended_at": null, "end_reason": null, "last_alive_at": null, "outcome": null, "fact_id": null
}
```

- `intent` 是该 Intent 的 `description` 与 `from`。
- `state` 为 `running`、`over_budget`（仍存活但已过 `deadline_at`：今天的超时与 conclude 收尾正在进行）或 `ended`。`elapsed_seconds` 与 `remaining_seconds` 使用服务端时钟，已结束的运行为 null；其他时钟都不需要与它一致。
- `window.review_status` 的取值见「窗口与评审时钟」。
- `reviews` 是摘要：`window`、`review_id`、`source`、`decision`、`issue_id`、`created_at`。
- 副作用：sweep；然后，**只有 `include_ended` 为 false 时**，记录修复代理的轮询时间。因此评估读取从不改变 `why`；但它们仍可能写入：sweep 可能结束运行、记录未评审 issue，与任何 sweep 一样。
- 错误：401、503；参数类型错误或越界时为 FastAPI 格式的 422；`cursor` 没有与 `include_ended` 一起使用，或服务端无法解码时为 `422 bad_cursor`。

---

#### GET /repair/runs/{run_id}

读取一次运行（agent 令牌），无论是否结束。返回它的 `RunView`，其中 `reviews` 是完整保存的评审：`repair_reviews.detail_json` 中的内容，加上 `review_id`、`source`、`issue_id`、`created_at`、`digest`、`digest_sha256` 与 `raw_output`。先执行 sweep；不记录轮询时间。运行不存在时返回 `404 run_not_found`，包括 Intent 或项目已不存在的运行。

---

#### POST /repair/runs/{run_id}/reviews

记录修复代理对一个窗口的评审（agent 令牌）。

Body：

```json
{
  "review_id": "rv-8b1e2f30a9c4d5e6f7a8b9c0",
  "window": 1,
  "decision": "extend",
  "reason": "The squashfs extraction is advancing steadily and the next step depends on its result.",
  "evidence": ["E14 t+21:10 second partition found", "E16 t+22:10 extraction reports 70%"],
  "next_step": "List /etc once the extraction finishes",
  "fallback": null,
  "expected_version": 1,
  "digest_elapsed_seconds": 1352,
  "transcript_cut_seconds": 1340,
  "model": "<model id>",
  "digest": "<the digest the model saw>",
  "raw_output": "{\"decision\": \"extend\", ...}"
}
```

| 字段 | 规则（不满足时返回 422） |
| --- | --- |
| `review_id` | `^[A-Za-z0-9_.:-]{8,64}$`；每次评审一个，每次重试都复用 |
| `window` | 1–1000 |
| `decision` | `continue` \| `extend` \| `detach` |
| `reason` | 1–2000 个字符，清理后不能为空 |
| `evidence` | 至多 10 项，每项 1–500 个字符 |
| `next_step` | null 或至多 500 个字符 |
| `fallback` | null，或 `model_timeout`、`model_error`、`invalid_output`、`transcript_unreadable`、`digest_error` 之一；有 fallback 时 `decision` 必须为 `continue` |
| `expected_version` | 可选的整数（Phase 3 对 extend 与 detach 要求它） |
| `digest_elapsed_seconds` | **必填**，0 到 604800 的数字：发起本次评审的那次轮询中该运行的 `elapsed_seconds` |
| `transcript_cut_seconds` | 可选，数字 ≥ 0：副本已知与源文件一致的、运行的已用时间（副本 `meta.json` 的 `checked_elapsed_seconds`），用于审计 |
| `model` | 可选，至多 100 个字符 |
| `digest` | 可选，至多 65536 个字符 |
| `raw_output` | 可选，至多 16384 个字符 |

请求的上限等于保存的上限，因此不会被悄悄截断；修复代理在提交之前把文本截到这些上限。

检查，在 sweep 之后、同一个 `BEGIN IMMEDIATE` 事务中，按顺序：

| # | 条件 | 回答 |
| --- | --- | --- |
| 1 | 运行不存在，包括已随 Intent 或项目删除的运行，以及 Intent 或项目已不存在的运行 | `404 run_not_found` |
| 2 | 该 (run, window) 已有一行，且 `review_id` 相同 | **200** 重放 `{"created": false, ...}`，返回已保存的评审与 issue，即使运行已经结束 |
| 3 | 该窗口已有另一个修复代理 `review_id` 的行 | `409 already_reviewed`，附已有评审的摘要 |
| 4 | 该行是服务端的未评审记录 | `409 window_closed`，附该记录的摘要 |
| 5 | 项目开关关闭 | `409 repair_disabled` |
| 6 | 运行已结束 | `409 run_ended`，附 `end_reason` |
| 7 | `window` 不是当前窗口 | `409 wrong_window` |
| 8 | 运行不可评审 | `409 not_reviewable` |
| 9 | now < `review_opens_at` | `409 too_early`，附 `review_opens_at` |
| 10 | now ≥ `review_accept_until` | `409 window_closed` |
| 11 | `started_at + digest_elapsed_seconds` < `review_opens_at − 5 s` | `409 digest_too_early` |
| 12 | `started_at + digest_elapsed_seconds` > `review_closes_at + 5 s` | `409 digest_too_late`，附 `review_closes_at` |
| 13 | 给出了 `expected_version` 且不等于 `version` | `409 stale_version`，附 `version` |

成功时：

- 清理文本：把连续空白折叠为一个空格；去掉 Unicode Cc 与 Cf 类字符（双向覆盖、零宽字符等）。
- 在 `repair_reviews` 中保存完整的评审（`source='agent'`，detail 中带 `mode: "shadow"` 与 `applied: false`，digest 及其 SHA-256，原始输出），在同一事务中插入展示用的 issue，并设置 `settled_window`。这是原子的：任何一步失败都让请求失败（500），修复代理用同一个 `review_id` 重试。（与此不同，`json_repaired` issue 是在 conclude 提交之后尽力记录的。）
- extend 与 detach 与 continue 一样只是记录：不向 Dispatcher 发送任何东西，运行的 `deadline_at`、`version`、`slices_used` 与 `state` 从不改变。
- 日志 `repair review recorded run=%s project=%s intent=%s window=%s decision=%s fallback=%s mode=shadow applied=false`；每个 409 以 INFO 记录 `repair review refused run=%s code=%s`。
- 返回 **201**：

  ```json
  {"created": true, "mode": "shadow", "applied": false,
   "review": {"run_id": "5f2c…", "window": 1, "review_id": "rv-8b1e…", "source": "agent", "decision": "extend", "issue_id": 42, "created_at": "2026-11-14T10:22:41Z"},
   "issue": {"id": 42, "kind": "repair_review", "run_id": "5f2c…", "detail": {"decision": "extend", "mode": "shadow", "applied": false, "...": "..."}, "created_at": "2026-11-14T10:22:41Z", "updated_at": "2026-11-14T10:22:41Z"}}
  ```

同一窗口由多个线程同时提交不同的 `review_id` 时，只有一个得到 201，其余得到 409，也只产生一条 issue。

---

#### PUT /projects/{project_id}/repair

打开或关闭项目的修复层开关（开放）。

Body（`UpdateProjectRepairRequest`）：

```json
{
  "enabled": true
}
```

- `enabled` 必须是布尔值，`"sometimes"` 之类的值返回 422，与 `bootstrap_enabled` 相同。
- 返回 200 与项目元信息（含 `repair_enabled`）；项目不存在返回 404 `"Project not found"`（纯字符串 detail，供界面显示）。任何项目状态下都允许调用。
- 调用方：项目标题栏中的 Repair 按钮（3 秒内点击两次）、`curl`，以及任何能访问服务端端口的程序（包括 Worker）。新建项目对话框的复选框则通过 `POST /projects` 的 `repair_enabled` 设置开关。
- 打开开关即同意把该项目每次评审的 digest 发送给 `dispatch.yaml` 的 `repair:` 部分中列出的第一个评审模型；`repair.projects` 列表只能缩小这一范围。主线上该接口是开放的，因此能访问 8000 端口的任何程序（包括 Worker）都能为任何项目给出或撤回这一同意。
- 只在值发生变化时写入并记日志：`repair switch project=%s enabled=%s client=%s`，级别 WARNING，因此不需要任何日志配置就会出现在服务端的 stderr 上。`client` 是连接的地址 `request.client.host`（`request.client` 为 None 时为 `-`），只有当它整串匹配 `[0-9A-Fa-f:.]{2,45}`、且 `ipaddress.ip_address(host)` 接受它时才打印，否则为 `-`。两个检查缺一不可：`ip_address()` 也接受带 scope id（`%` 之后的文本）的 IPv6 地址，而 scope id 可以是任意可打印文本。
- 这一行记录的是“发生了变化”以及时间，而不是“谁做的”：服务端主机上的每个调用方显示同一个地址，而 127.0.0.1 上的调用方还能用 `X-Forwarded-For` 设置这个地址（`cairn serve` 使用 uvicorn 默认的代理头设置，信任 127.0.0.1）。
- 对运行没有其他影响：已登记的运行继续并正常结束；开关关闭后它们不再出现在列表中，评审被拒绝。Dispatcher 下一次读取项目之后开始的运行才走新的路径；一次运行始终保持它开始时的路径。

---

#### GET /repair/settings

只读地返回修复层的设置（开放）。

```json
{"shadow": true, "slice_seconds": 600,
 "max_extensions_per_run": 0, "max_extended_runs": 0, "max_detached_per_project": 0,
 "max_detached_total": 0, "max_detached_seconds": 0,
 "review": {"open_fraction": 0.75, "close_fraction": 0.8, "accept_margin_seconds": 10,
            "min_window_seconds": 350, "digest_time_margin_seconds": 5,
            "reviewable_cli_types": ["claudecode", "codex", "pi"]},
 "roles_configured": {"agent": true, "dispatcher": true},
 "agent_last_seen_at": "2026-11-14T10:22:41Z"}
```

影子模式开启；600 s 的时间片只用于描述 extend 结论；所有动作上限都为 0。没有 `PUT`（FastAPI 返回 405），也没有更新模型：这些都是代码中的常量，`GET /settings` 与 `PUT /settings` 不受影响。从 Phase 3 起，执行模式与上限是服务端的启动设置，任何 HTTP 请求都不能改变它们。`roles_configured` 表示哪个角色配置了有效的令牌，`agent_last_seen_at` 是修复代理最近一次默认轮询的时间（尚未轮询过时为 null），运行手册用一次 `curl` 就能检查部署。

---

## 消费者典型使用流程

```
1. 读取结构化项目数据
   用于调度、状态判断、intent 选择与协议写回

2. GET /projects/{id}/export?format=yaml
   读取图快照（仅供 Dispatcher 渲染给 Agent）

3. 若项目仍处于最初阶段：
   典型判定是 facts 只有 `origin` 和 `goal`，且没有普通 intent；
   某些消费者会允许“没有任何 intent”，或“只存在保留 bootstrap intent”都视为初始态
   若 `project.bootstrap_enabled=false`，或消费者不具备 bootstrap 能力，则直接进入 reason；
   a. 若尚不存在保留 bootstrap intent：
      POST /projects/{id}/intents
        { from: ["origin"], description: "bootstrap", creator: "dispatcher.bootstrap", worker: null }
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      认领成功后，直接做一轮 `bootstrap`
   c. 推进过程中持续 heartbeat
   d. 若拿到了关键证明性事实：
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "发现登录页存在默认口令 admin:admin" }
      若该新 fact 已足以满足 goal，也可继续：
      POST /projects/{id}/complete
        { from: ["新写入的 fact id"], description: "为什么该事实已足以满足 goal", worker: "dispatcher-worker-A" }
   e. 若本轮 bootstrap 失败且需要放弃：
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. 回到步骤 1

4. 若图中存在未认领的普通 Intent（worker=null）：
   a. 选择一条可认领 Intent
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      认领成功后，派发一次 `explore`
   c. 探索过程中持续 heartbeat
   d. 探索成功时：
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "发现 /search 参数存在报错注入" }
   e. 若探索失败且需要放弃：
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. 回到步骤 1

5. 若图中没有未认领 Intent：
   a. POST /projects/{id}/reason/claim
        { worker: "dispatcher-worker-A", trigger: "new_facts" }
      认领成功后，派发一次 `reason`
   b. 推进过程中持续 POST /projects/{id}/reason/heartbeat
        { worker: "dispatcher-worker-A" }
   c. 若 `reason` 返回完成结论：
      POST /projects/{id}/complete
        { from: ["f008"], description: "flag{abc} 满足 goal 要求", worker: "dispatcher-worker-A" }
      流程结束
   d. 若 `reason` 返回新的 intent：
      POST /projects/{id}/intents
        { from: ["f003"], description: "尝试 SQL 注入", creator: "dispatcher-worker-A", worker: null }
      然后 POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   e. 若 `reason` 未返回完成结论，也未返回新 intent：
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
      本轮不写图
   f. 若 `reason` 执行失败、超时或需要放弃：
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   g. 回到步骤 1

6. 若项目已 `completed`，但图外验证确认之前的完成判断有误：
   a. POST /projects/{id}/reopen
        { description: "flag FLAG{fake} 是错误的，需要继续寻找正确 flag", creator: "judge" }
   b. 项目回到 `active`，图中新增一条 `external_feedback` 结论边和一个纠错 Fact
   c. 回到步骤 1
```
