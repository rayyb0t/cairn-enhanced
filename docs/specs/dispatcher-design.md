# Dispatcher

---

## 本质

Dispatcher 是 Cairn 的客户端执行器。它负责：

1. 协调项目容器生命周期
2. 给 Agent 下发明确任务
3. 代 Agent 调用 Cairn API 写回图

Agent 不直接认领 Intent，不直接 heartbeat，不直接调用 Cairn API。Agent 只接收 Dispatcher 下发的任务，返回结构化结果；Dispatcher 再决定如何请求 Server。

---

## 设计要点

1. Agent 的输出任务收敛成三类：`bootstrap`、`reason` 和 `explore`；`bootstrap` 只在项目初始态运行，让 Agent 直接尝试解决整个问题；主阶段只有在已解决时才返回，且必须同时给出关键 Fact 和 `complete`；若主阶段超时，再由 `bootstrap_conclude` 收尾产出 Fact；`reason` 负责读图判断是否完成或是否需要提出一个新 intent；`explore` 只负责执行一个已认领 intent 并产出一个 Fact 结论。
2. Dispatcher 是唯一的协议写入者和控制面；Agent 不 claim、不 heartbeat、不直接调用 Cairn API。
3. 超时策略按任务类型定义；`bootstrap` 和 `explore` 都支持“第一阶段执行 + timeout / parse-fail 后用同一 session 进入 conclude 收尾”的双阶段模式。
4. Prompt 以 markdown 文件形式随代码分发；支持按 prompt 组切换；Worker 行为由 `claudecode`、`codex`、`mock` 等 driver 实现；`dispatch.yaml` 只描述运行期参数。
5. 调度上，项目初始态按 `project.bootstrap_enabled` 和 Worker 能力决定优先 `bootstrap` 或直接 `reason`；非初始态出现新 Fact / Hint 等新态势时优先 `reason`，否则优先消费可认领的 `explore` intent；`reason` 的并发约束通过服务端的项目级 `project.reason` lease 表达为“单项目最多一个”，`bootstrap` 的并发约束是“单项目最多一个保留 bootstrap intent 且最多一个 bootstrap 任务”，跨项目允许并行；`runtime.interval` 被刻意复用为主循环节拍和带 claim 任务的 heartbeat 周期。
6. Worker 按独立的 LLM 并发配额单元建模；同一个 key 不拆成多个 Worker，因此并发控制使用 `workers[].max_running` 即可。
7. 运行日志按“状态变化优先”设计：稳定轮询、正常 heartbeat、重复 skip 原则上不刷屏；容器创建、任务派发、容器内新进程启动、健康检查、超时、收尾、释放 intent、worker 进入短暂不可选窗口等事件必须可见。
8. 项目容器收尾不应阻塞主调度循环；多个已完成项目的容器 cleanup 可以并行进行。
9. 项目切到非 `active` 后，Dispatcher 必须把它视为硬停止：不再派发新任务；对本地仍在运行的 `bootstrap`、`explore`、`reason` 任务立即发出取消；对已取消任务不再进入 conclude fallback；并在后续轮询中停止该项目容器，杀掉容器内仍在运行的 Agent 进程。
10. 当前实现按“单 Dispatcher 实例”设计和测试；不支持多个 Dispatcher 同时连接同一服务端共同调度。
11. 若项目曾 `completed` 后又被服务端显式 `reopen` 为 `active`，Dispatcher 不做特殊分支：它会把这视为普通 active 项目继续调度；同时若该项目容器仍处于已排队 cleanup 状态，Dispatcher 会先等待 cleanup 完成，避免与旧的 completed/stopped cleanup 竞态。
12. 已知限制：当前协议只记录当前 claim 持有者，不保留 Intent 的 worker 历史；因此项目被 `stopped` 后，随着 open intent 的 `worker` 被服务端清空，Dispatcher/UI/API 都无法直接展示“停止前最后是谁在推进这个 intent”。后续若要补这部分可观测性，较合理的方向是在 Intent 上增加类似 `worker_history` 的历史字段，而不是改变当前 claim 语义。

补充：

- 项目被删除后，Dispatcher 会把它视为 `deleted`。这和 `stopped` 一样会先取消本地运行中的任务，但容器收尾语义不同：`deleted` 对应的 orphan 容器会被直接删除，而不是仅停止后保留。

---

## 架构概览

这个项目在架构上可以分成 4 个部分：

1. Cairn Server
2. Dispatcher
3. 项目容器
4. Worker / Agent CLI

### 1. Cairn Server

Server 是协议真相源。

它负责：

- 保存 Project / Fact / Intent / Hint
- 提供协议接口
- 维护 Intent 的认领、心跳、结论状态
- 维护项目级 `reason` lease 的认领、心跳与释放状态

### 2. Dispatcher

Dispatcher 是这个工程要实现的核心。

它负责：

- 拉取项目图状态
- 决定当前该派发哪一种任务
- 选择哪个 Worker 来执行
- 管理项目容器和 Worker 进程
- 维护 session、超时、健康检查、收尾
- 把结果写回 Cairn Server

### 3. 项目容器

每个项目对应一个运行容器。

这个容器是该项目的执行环境，通常负责：

- 提供工具链
- 提供网络环境
- 承载该项目下的 Worker 进程

### 4. Worker / Agent CLI

Worker 不是协议参与者本身，而是 Dispatcher 管理下的执行单元。

例如：

- Claude Code CLI
- Codex CLI

它们负责：

- 接收 Dispatcher 渲染好的 prompt
- 在当前容器内执行任务
- 输出结构化 JSON

### 组件关系

```text
                         +----------------------+
                         |     Cairn Server     |
                         |----------------------|
                         | Projects / Facts     |
                         | Intents / Hints      |
                         | Protocol API         |
                         +----------^-----------+
                                    |
                           read / write API
                                    |
+-----------------------------------------------------------+
|                         Dispatcher                        |
|-----------------------------------------------------------|
| Scheduling / Task Dispatch / Session / Timeout / Health   |
| Container Lifecycle / Protocol Writeback                  |
+----------------------+----------------------+-------------+
                       |                      |
             manage container        manage container
                       |                      |
          +------------v-----------+  +------v-------------+
          |   Project Container A  |  | Project Container B|
          |------------------------|  |--------------------|
          | Worker / Agent CLI     |  | Worker / Agent CLI |
          | - Claude Code          |  | - Codex            |
          | - Codex                |  | - ...              |
          +------------------------+  +--------------------+
```

### 执行主链路

Dispatcher 会同时读取两类数据：

- 结构化接口：用于调度、状态判断、intent 选择、协议写回
- `GET /projects/{project_id}/export?format=yaml`：仅用于构造 prompt 所需的图快照

1. Dispatcher 从 Server 读取项目图
2. Dispatcher 依据调度规则选择任务类型和 Worker
3. Dispatcher 渲染 prompt 与命令占位符
4. 如果是 `explore`，Dispatcher 先通过 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` 认领目标 intent；如果是 `reason`，则先通过 `POST /projects/{project_id}/reason/claim` 认领项目级 reason lease
5. Dispatcher 在项目容器内启动 Worker 进程
6. Worker 输出结构化 JSON
7. Dispatcher 解析结果，并调用 `POST /projects/{project_id}/complete`、`POST /projects/{project_id}/intents`、`POST /projects/{project_id}/intents/{intent_id}/conclude`、`POST /projects/{project_id}/intents/{intent_id}/release` 或 `POST /projects/{project_id}/reason/release`

项目若被切到 `stopped`，这条主链路会在下一轮短路：Dispatcher 不再把该项目纳入 active 调度集合，会先取消本地仍在运行的任务，再转入容器 cleanup 流程并停止项目容器。对 `bootstrap` / `explore` 来说，被 `stopped` 取消后不会再进入 conclude fallback，因此不会再额外落 Fact。项目恢复为 `active` 后，Dispatcher 会重新读取图状态，再决定是继续 `explore`、进入 `reason`，还是在初始态依据 `project.bootstrap_enabled` 和 Worker 能力重新选择 `bootstrap` 或 `reason`。项目若在 `completed` 后被服务端 `reopen`，对 Dispatcher 来说也等价于“重新变成 active 且图上多了一个新 fact”；下一轮会按普通 active 项目继续调度。

Worker 选择规则：

1. 先按任务类型筛选
2. 再过滤掉已达到 `max_running` 的 Worker
3. 再过滤掉处于短暂不可选窗口内的不健康 Worker
4. 在剩余 Worker 中，优先选择 `priority` 更小的
5. 如果 `priority` 相同，则优先选择当前运行中任务数更少的
6. 如果仍然相同，则随机选择
7. 如果是 `explore`，Dispatcher 先通过 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` claim 成功，再真正启动任务
8. 如果是 `reason`，claim 成功后由 `POST /projects/{project_id}/reason/heartbeat` 维持 lease；当 `runtime.worker_healthcheck=startup_and_task` 时，真正启动前再对选中的 Worker 执行一次健康检查；如果失败，则本次任务作废；该 Worker 会进入一个短暂不可选窗口，等待后续轮次再尝试

---

## 配置模型

Dispatcher 使用一个运行期配置文件：

- `dispatch.yaml`

也就是说：

- 使用者只需要提供 `dispatch.yaml`
- 任务 prompt 以 markdown 文件形式随代码分发，并通过 `runtime.prompt_group` 选择目录
- Worker 的健康检查、命令模板、session 处理、二阶段收尾能力由对应 driver 实现
- `runtime.execution` 选择执行后端：默认 `container`（每项目一个容器），或 `local`（worker 直接在 dispatcher 宿主机上以子进程运行，复用本机已配置好的 CLI，无需 Docker 与 API key）

代码目录可以采用类似组织：

```text
dispatcher/
  models.py
  prompting.py
  output_parser.py
  contracts.py
  prompts/
    default/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
    mock/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
  workers/
    base.py
    registry.py
    adapters/
      claudecode.py
      codex.py
      mock.py
```

本文档附录给出：

- `dispatch.example.yaml` 的示例内容
- 上述 markdown prompt 的示例内容

---

## 任务模型

### 三类任务一览

| 任务 | 触发条件 | 输入 | 输出 | 超时策略 |
| --- | --- | --- | --- | --- |
| `bootstrap` | 项目 `active`；`project.bootstrap_enabled=true`，且配置中存在支持 `bootstrap` 的 Worker 或项目已经存在保留 bootstrap intent；facts 只有 `origin` 和 `goal`；当前没有普通 intent；允许不存在 bootstrap intent，或只存在保留的 open `bootstrap` intent | `{origin}`、`{goal}`、`{hints}` | 主阶段成功时固定返回 `fact + complete`；收尾阶段只返回 `fact` | 双阶段：`timeout` 后可进入 `conclude_timeout` 收尾；两阶段都失败则 release 保留 intent，下轮仍按新项目重试 |
| `reason` | 项目 `active`；当前项目无未认领 intent；当前项目内无其他 `reason`；首次触发或满足“新态势”重触发条件 | `{graph_yaml}`、`{fact_ids}`、`{open_intents}` | `complete` 对象；或 `intent` 对象；或空 `data` | 仅 `timeout`；超时或非法结果直接作废，不写图 |
| `explore` | 项目 `active`；存在一个当前可认领的未结论 intent | `{graph_yaml}`、`{intent_id}`、`{intent_description}` | 一个 Fact 结论描述 | 单阶段：超时直接作废；双阶段：超时或输出解析失败时可进入 `conclude` 收尾 |

### `bootstrap`

#### 触发条件

- 当前项目仍然是 `active`
- `project.bootstrap_enabled = true`，且配置中存在支持 `bootstrap` 的 Worker 或项目已经存在保留 bootstrap intent
- facts 恰好只有 `origin` 和 `goal`
- intents 为空，或只存在保留的 open `bootstrap` intent
- 保留 `bootstrap` intent 的约定固定为：
  - `description = "bootstrap"`
  - `creator = "dispatcher.bootstrap"`
  - `from = ["origin"]`

#### 输入

- `{origin}`
- `{goal}`
- `{hints}`

其中：

- `{hints}` 是 JSON 数组文本，便于 Worker 在项目起始阶段快速吸收策略信息
- `bootstrap` 不读取图 YAML，不依赖普通 intent 图结构

#### 输出契约

`bootstrap` 使用独立输出契约：

- 主阶段只有在已经解决问题时才返回
- 主阶段返回时必须同时包含 `fact` 和 `complete`
- 如果主阶段没有在超时前解决问题，就不会返回合法结果；Dispatcher 会终止它并进入 `bootstrap_conclude`
- `bootstrap_conclude` 只负责收尾总结，因此只返回 `fact`

```json
{
  "accepted": true,
  "data": {
    "fact": {
      "description": "拿到两个 flag，分别为 flag{...} 与 flag{...}；同时获得管理员 shell，权限证明见 /tmp/proofs/root.txt"
    },
    "complete": {
      "description": "已拿到并验证完成 Goal 所需的全部关键结果，Goal 达成"
    }
  }
}
```

约束：

- 主阶段返回时，`data.fact.description` 和 `data.complete.description` 都必须存在
- 主阶段不允许只返回 `fact`
- `bootstrap_conclude` 只允许返回 `fact`，不允许返回 `complete`

#### 接口映射

`bootstrap` 复用普通 intent 协议，但使用保留 intent：

| 接口 | 用途 | 何时使用 |
| --- | --- | --- |
| `POST /projects/{project_id}/intents` | 创建保留 `bootstrap` intent | 初始态项目首次进入时，如尚不存在该 intent |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | claim 并维持 `bootstrap` intent | 派发前先 claim；执行中按 `interval` 周期发送 |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | 将 `bootstrap` 产出的关键结果写成 Fact | `bootstrap` 或 `bootstrap_conclude` 返回合法 `fact` 后调用 |
| `POST /projects/{project_id}/complete` | 基于刚写入的 bootstrap fact 直接完成项目 | 仅当 `bootstrap` 主阶段成功返回 `fact + complete` 时调用 |
| `POST /projects/{project_id}/intents/{intent_id}/release` | 放弃本次 bootstrap 尝试 | 两阶段都失败，或命令直接失败时调用 |

#### 超时与失败

- `bootstrap` 第一阶段使用 `timeout`
- `bootstrap_conclude` 第二阶段使用 `conclude_timeout`
- 主阶段如果在 `timeout` 内解决问题，就返回 `fact + complete`
- 第一阶段超时、输出解析失败或返回了不满足契约的结果时，如果 Worker 支持 session / conclude，则进入 `bootstrap_conclude`
- `bootstrap_conclude` 的 prompt 必须明确要求“不继续推进，不等待未完成任务，只总结当前最关键事实”，因此它只产出 `fact`
- 如果 `bootstrap` 主阶段成功返回合法 JSON，则 Dispatcher 会先 conclude 写入 fact，再立即 complete
- 如果 `bootstrap_conclude` 成功返回合法 JSON（或按“运行时校验”中的保守修复规则取回）且 conclude 写回成功，则保留 intent 被结论落定，项目不再视为初始态
- 如果主阶段的 `complete` 写回失败，已写入的 fact 仍然保留，后续可由下一轮 `reason` 再完成项目
- 如果两阶段都失败，或 conclude 写回失败，则 release 当前 `bootstrap` intent，不写 Fact；项目下轮仍然按新项目处理

### `reason`

#### 触发条件

- 当前项目仍然是 `active`
- 当前项目没有未认领 intent
- 当前项目的 `project.reason` 为空，也就是当前没有其他 `reason` lease 正在运行
- 首次触发只发生在“当前没有任何 open intent”时
- 之后只有出现新的态势才重新触发；这里的“新态势”限定为：
  - Fact 数量增加
  - Hint 数量增加
  - 项目从“存在 open intents”进入“没有 open intents”
- 单次 `explore` 失败、掉心跳、释放但 intent 仍保持 open，不构成新的态势，不应触发新的 `reason`

#### 输入

- `{graph_yaml}`
- `{fact_ids}`
- `{open_intents}`

其中：

- `{fact_ids}` 是 JSON 数组文本，用于显式列出当前合法的 Fact id
- `{open_intents}` 是 JSON 数组文本，用于显式列出当前所有未结论的 intent；因此即使有别的 intent 正在 `explore`，只要出现了新的 Fact / Hint，`reason` 仍可能再次被触发
- 这两个占位符都是 prompt 层辅助，不替代 server 的最终校验

#### 输出契约

已完成：

```json
{
  "accepted": true,
  "data": {
    "complete": {
      "from": ["f008"],
      "description": "flag{abc} 满足 goal 要求"
    }
  }
}
```

未完成，提出新 intent：

```json
{
  "accepted": true,
  "data": {
    "intent": {
      "from": ["f003"],
      "description": "尝试 SQL 注入"
    }
  }
}
```

未完成，不提新 intent：

```json
{
  "accepted": true,
  "data": {}
}
```

约束：

- `data.complete` 存在时，它必须是对象，且 `complete.from` 和 `complete.description` 都必须存在
- `data.complete` 存在时，不应再带 `intent`
- 如果 `intent` 存在，则 `intent.from` 和 `intent.description` 都必须存在
- 如果 `{open_intents}` 为空，说明当前图里没有任何进行中的探索；此时若没有 `data.complete`，则必须返回 `intent`
- 如果 `{open_intents}` 非空，且没有 `data.complete`，则允许不返回 `intent`

#### 接口映射

`reason` 启动前，Dispatcher 必须先 claim 项目级 reason lease，并在执行期间持续 heartbeat；这一状态会直接出现在 `GET /projects` 和 `GET /projects/{project_id}` 的 `project.reason` 字段中，供前端和其他消费者观察。

| `reason` 输出 | Dispatcher 动作 | 备注 |
| --- | --- | --- |
| `data.complete` 存在 | 调用 `POST /projects/{project_id}/complete` | `worker` 使用当前执行该任务的 `workers[].name` |
| `data.complete` 不存在，且带 `intent` | 调用 `POST /projects/{project_id}/intents` | `creator` 使用当前 Worker 名；`worker` 固定写 `null` |
| `data` 为空对象 | 不写图 | 不写 Fact / Intent / Complete |

写回失败的日志语义：

- 如果 `POST /projects/{project_id}/complete` 或 `POST /projects/{project_id}/intents` 返回 `403`，通常表示项目已不再是 `active`，本次任务直接作废，记 `info`
- 其他写入失败也直接作废，不做立即重试，只记日志
- 无论本轮是否写图，只要项目仍是 `active` 且 reason lease 仍在自己手里，Dispatcher 都会在收尾时调用 `POST /projects/{project_id}/reason/release`；如果项目已 `completed` 或 `stopped`，则由服务端直接清空该 lease

#### 超时与失败

- `reason` 只使用 `timeout`
- 超时直接作废
- `accepted: false` 直接作废，记 `warn`
- 其他执行错误也直接作废，例如：
  - 命令退出码非 `0`
  - 输出不是合法 JSON
  - JSON 缺少必要字段
- 以上情况都不写 Fact / Intent / Complete，只记日志

### `explore`

#### 触发条件

- 当前项目仍然是 `active`
- 存在一个当前可认领的、尚无结论的 intent

#### 输入

- `{graph_yaml}`
- `{intent_id}`
- `{intent_description}`

#### 输出契约

正常返回：

```json
{
  "accepted": true,
  "data": {
    "description": "发现 /search 参数存在报错注入"
  }
}
```

即使没有打出漏洞，也应返回一个客观探索结论，而不是空响应。例如：

```json
{
  "accepted": true,
  "data": {
    "description": "对 /search 参数测试常见 SQL 注入 payload，未发现可利用注入迹象"
  }
}
```

约束：

- `data.description` 必须存在，且必须是客观事实结论
- 不允许输出“我拒绝帮助渗透”“这不安全”等文本作为 `description`

#### 接口映射

`explore` 会涉及三类协议接口：

| 接口 | 用途 | 何时使用 |
| --- | --- | --- |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | claim 并维持持有 | 派发前先 claim；执行中按 `interval` 周期发送 |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | 产出 Fact 并结论落定 Intent | `execute` 或 `conclude` 返回合法结论后调用 |
| `POST /projects/{project_id}/intents/{intent_id}/release` | 放弃当前尝试 | 失败路径使用 |

派发顺序要求固定为：

1. Dispatcher 先选中一个可认领 intent
2. Dispatcher 先调用一次 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` 作为 claim
3. 只有 heartbeat 成功后，才真正启动 `explore` 对应的 Worker

#### 超时与失败

`explore` 要兼容两种模式：

1. 单阶段模式：只有 `execute`
2. 双阶段模式：`execute + session + conclude`

其中 `conclude` 是附加收尾阶段，不是主流程必经阶段。

第一阶段使用 `timeout`。
如果是双阶段模式，第二阶段使用 `conclude_timeout`。

正常完成：

- 如果 `execute` 在 `timeout` 内正常返回合法 JSON，且 `accepted: true`
- Dispatcher 直接调用 `POST /projects/{project_id}/intents/{intent_id}/conclude`
- `POST /projects/{project_id}/intents/{intent_id}/conclude` 成功即完成结论落定，无需额外 `release`
- 如果 `POST /projects/{project_id}/intents/{intent_id}/conclude` 写入失败，本次任务直接作废，不做立即重试，释放当前 intent，只记日志

可进入二阶段收尾的异常：

- 这类异常只在“双阶段 Worker”上进入 `conclude`
- 适用异常只有两种：
  - 执行超时
  - Dispatcher 无法从第一阶段输出里正确提取并解析结果，例如：
    - 输出不是合法 JSON
    - JSON 缺少必要字段
    - `accepted: true` 但 `data` 结构不符合当前任务要求
- 这类异常在“单阶段 Worker”上不进入 `conclude`

双阶段收尾流程固定为：

1. Dispatcher 杀掉当前进程
2. 保留这次任务对应的 session id
3. 在保持 heartbeat 的前提下，用同一个 session 直接进入 `conclude`
4. `conclude` 的 prompt 必须明确要求“不要继续探索，只总结截至目前已经完成的探索与结论”
5. 如果 `conclude` 在 `conclude_timeout` 内返回合法 JSON，且 `accepted: true`：
   - Dispatcher 调用 `POST /projects/{project_id}/intents/{intent_id}/conclude`
   - 成功则结束
   - 输出未通过严格解析或结构校验时，先按“运行时校验”中的保守修复规则尝试取回，取回成功同样按本条处理，否则按第 6 条处理
6. 如果 `conclude` 再次超时，或输出不合法，或返回 `accepted: false`，或 `POST /projects/{project_id}/intents/{intent_id}/conclude` 写入失败：
   - 整次探索作废
   - 不写任何图数据
   - 释放当前 intent
   - 只记日志

单阶段 Worker 的异常处理：

- 如果当前 Worker 不支持 `session` 或 `conclude`，则它属于单阶段模式
- 这时第一阶段一旦出现“超时”或“输出解析 / 结构校验失败”，直接按失败处理
- 处理方式是：杀进程、整次探索作废、不写任何图数据、释放当前 intent、记 `warn`

直接失败，不进入 `conclude`：

- 第一阶段返回 `accepted: false`
- 命令退出码非 `0`
- Worker 进程根本没有产生可读取结果
- Dispatcher 在进入结果解析前就已经确定本次执行失败

以上情况都：

- 不进入 `conclude`
- 不写任何图数据
- 释放当前 intent
- 清理本地任务状态
- 只记日志

---

## 调度策略

### 全局调度

核心规则：

1. 已运行项目优先，但只优先可立即派发的任务
2. 如果某个运行中项目处于初始态且可执行 `bootstrap`，优先继续它
3. 否则如果某个运行中项目存在可执行的 `explore`，优先继续探索它
4. 如果所有运行中项目都暂时没有可派发任务，且 `runtime.max_running_projects` 还有余量，就启动一个未开始的新项目

`runtime.interval` 的设计约定：

- `runtime.interval` 不只是一个普通轮询间隔
- 它被刻意复用为两个地方的统一节拍：
  - Dispatcher 主循环间隔
  - 带 claim 任务（`bootstrap` / `explore`）的 heartbeat 周期
- 这样做的目标是减少额外时序参数，先保持实现简单
- 这是一项明确设计决策，不是偶然耦合

调度伪代码可以保持成下面这种粒度：

```text
for project in running_projects_round_robin:
  if has_dispatchable_bootstrap(project):
    dispatch_bootstrap(project)
    continue
  if has_dispatchable_reason(project):
    dispatch_reason(project)
    continue
  if has_dispatchable_explore(project):
    dispatch_explore(project)
    continue

if running_project_count < runtime.max_running_projects:
  maybe_start_one_new_project()
```

### 项目内调度

对于单个项目，Dispatcher 读完整项目状态后，按下面顺序调度：

1. 如果项目仍处于初始态，先按 `project.bootstrap_enabled` 和 Worker 能力决定路径：未开启或没有支持 `bootstrap` 的 Worker 时直接 `reason`，否则执行 `bootstrap`；若已经存在保留 bootstrap intent，则继续该阶段
2. 如果满足“新态势”重触发条件，优先派发 `reason`
3. 否则如果存在未认领 intent，派发 `explore`
4. `reason` 的去重按“态势”做，而不是按总图变化做：首次只有在当前没有任何 open intent 时才触发；之后只有当前 Fact / Hint 数量增加，或项目从“存在 open intents”进入“没有 open intents”时，才重新触发
5. 如果 `reason` 返回 `data.complete`，Dispatcher 调用 `POST /projects/{project_id}/complete`
6. 如果 `reason` 没有返回 `data.complete` 且带 `intent`，Dispatcher 调用 `POST /projects/{project_id}/intents`
7. 如果 `reason` 既没有返回 `data.complete`，也没有返回 `intent`，则本轮不写图

另外：

- 初始态项目里，如果选择了 `bootstrap` 路径且 bootstrap intent 已被 claim，则这一轮不再派发 `reason` 或普通 `explore`
- 即使同一项目里已经有进行中的 `explore`，也允许继续派发一个 `reason` 任务
- 但前提不是“刚新增了 intent”，而是“确实出现了新的 Fact / Hint 等新态势”；仅仅因为上一个 `reason` 刚创建了新的 intent，不应该立刻再次 `reason`
- 仍然要求当前没有未认领 intent、当前项目内没有其他 `reason` 任务在运行、且没有超过 `runtime.max_project_workers`

`reason` 的去重规则建议保持简单：

- 记录该项目上次成功完成 `reason` 时的 Fact 数量、Hint 数量，以及当时是否仍存在 open intents
- Dispatcher 对“当前已观察到、且已有 open intents、但尚无 checkpoint 的 active 项目”建立基线 checkpoint；启动时已有项目和运行中晚到项目都适用，不应吞掉运行过程中第一次新增的 Fact / Hint
- 首次没有历史记录且当前没有 open intents 时，直接触发
- 之后只有 Fact / Hint 数量增加，或项目从“有 open intents”变为“无 open intents”时，才再次触发 `reason`
- 不把“总 intent 数量增加”当作重触发条件；因为这通常只是上一次 `reason` 刚创建了新 intent，并不代表出现了新的态势
- `explore` 的执行失败、掉心跳、临时 release 只会让 intent 重新等待被探索，不会额外触发 `reason`

### 并发约束

- 当前设计下，只支持一个 Dispatcher 实例连接同一服务端执行调度
- 如果同时运行多个 Dispatcher，本地维护的 admission、Worker 健康状态、并发计数、容器清理和 bootstrap 去重都不会跨进程协调，因此不属于支持场景
- 单个项目内，同一时刻最多只能有一个 `bootstrap` 任务在运行
- Dispatcher 会尽力让单个项目在初始态时只保留一个 open `bootstrap` intent
- 单个项目内，同一时刻最多只能有一个 `reason` 任务在运行
- 跨项目允许并行运行多个 `reason`
- `reason` 也计入对应项目的 `runtime.max_project_workers`
- `runtime.max_workers`：Dispatcher 同时运行中的任务总数上限
- `runtime.max_running_projects`：当前 dispatcher 运行期内已接手且仍为 `active` 的项目 admission 上限；项目即使暂时没有可派发任务，只要仍为 `active`，也继续占用该名额，直到其退出 active
- `runtime.max_project_workers`：单个项目内同时运行的任务上限，统一计入 `bootstrap`、`reason` 和 `explore`
- `workers[].max_running`：单个 Worker 自身的并发上限；达到上限后，这个 Worker 暂时不再参与派发

---

## Worker 配置

### 字段定义

Dispatcher 固定从 `stdout` 取全文作为模型正文输出。

`dispatch.yaml` 中与 Worker 相关的运行期字段如下：

| 字段 | 含义 | 说明 |
| --- | --- | --- |
| `name` | Worker 静态标识 | 协议写回时作为 `creator` 或 `worker` |
| `type` | Worker driver 名 | 支持 `claudecode`、`codex`、`mock` |
| `task_types` | 支持的任务类型 | `bootstrap`、`reason`、`explore` |
| `max_running` | Worker 并发上限 | 达到上限后暂不派发 |
| `priority` | 选择优先级 | 数字越小越优先 |
| `env` | 运行时环境变量 | 由对应 driver 使用；`mock` 的 phase 耗时和结果概率也通过这里配置 |

系统提供三类 Worker driver：

- `claudecode`
- `codex`
- `mock`

也就是说：

- `dispatch.yaml` 负责声明“用哪个 driver、能跑什么任务、并发多少、环境变量是什么”；如果是 `mock`，各 phase 的模拟分布也放在 `env`
- 具体怎么健康检查、怎么启动命令、怎么提取 session、怎么恢复 `conclude`，都由 driver 代码负责

### Driver 接口

每个 Agent / CLI 工具在代码里对应一个独立 driver 文件，并实现统一接口。

统一接口至少应覆盖这些能力：

- `check_health(worker, *, timeout)`：进程内发起一次 HTTP 探活，返回 `HealthResult`
- `prepare_session()`：需要时预先生成 session id
- `build_execute(worker, prompt, session)`：构造第一阶段执行命令
- `extract_session(session, stderr)`：需要时从 `stderr` 提取 session id，或继续使用预生成 session
- `build_conclude(worker, prompt, session)`：在双阶段 `explore` 中恢复同一 session 做收尾
- `supports_conclude()`：声明该 driver 是否支持双阶段 `explore`

这些 driver 的能力约定是：

- `claudecode` 支持双阶段 `explore`
- `codex` 支持双阶段 `explore`
- `mock` 支持双阶段 `explore`

并发建模约定：

- 一个 Worker 应代表一个独立的 LLM 并发配额单元
- 不考虑“多个 Worker 共用同一个 key”的情况
- 并发控制使用 `workers[].max_running`

### 健康检查

健康检查由 driver 的 `check_health` 实现，Dispatcher 在自己进程内直接发起（用 `requests`），而不是在容器里执行 curl。

目标不是验证“容器活着”，而是验证某个具体 Worker 的 LLM 配置真的可用：

- base URL 可达
- API key 有效
- model 可调用

driver 按各自 provider 的协议构造请求：`claudecode` 打 `{base}/v1/messages`，`codex` 打 `{base}/responses`，`pi` 按 `PI_PROVIDER_API` 选择 `/chat/completions`、`/responses` 或 `/v1/messages`。请求会带上 worker 环境里的 `http(s)_proxy`，从而与 worker 实际出网路径一致。

执行规则：

- 每次准备启动某个 Worker 进程前，都先执行一次健康检查
- `explore` 进入 timeout / parse-fail 后的 `explore_conclude` fallback 不再重复执行健康检查，而是直接尝试 conclude，并继续依赖 JSON / schema 校验（含“运行时校验”中的保守修复规则）决定是否写回
- HTTP 2xx 视为健康
- 非 2xx / 连接失败 / 超时（`requests` 超时由 `runtime.healthcheck_timeout` 控制）视为不健康
- 如果这次失败，本次任务直接作废，并将这个 Worker 放入一个短暂不可选窗口
- 窗口结束后，后续轮次再次选择到这个 Worker 时重新检查
- Dispatcher 内部会为最近失败的 Worker 记录一个本地 `retry_after`，在这之前不再派发给它

Dispatcher 只看 HTTP 状态码，不解析响应体。`local` 模式下不打 API（本机 CLI 自带鉴权），改为启动时对各 CLI 执行 `--help` 探测。

### CLI 接入约定

#### `claudecode` driver

依赖环境变量：

- `ANTHROPIC_MODEL`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`

健康检查等价于下面这条请求（实现为 Dispatcher 进程内的 HTTP 调用，不再在容器里执行 curl）：

```bash
curl -sS --fail -o /dev/null \
  "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "content-type: application/json" \
  -d "{\"model\":\"$ANTHROPIC_MODEL\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

已知行为：

- driver 预先生成 session id
- 首轮可以预先指定 session id
- 如果该 id 已存在，命令会报错，不会复用旧会话
- Dispatcher 固定从 `stdout` 取全文作为结果正文

第一阶段执行：

```bash
claude --session-id "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

二阶段收尾：

```bash
claude -r "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

#### `codex` driver

依赖环境变量：

- `CODEX_MODEL`
- `CODEX_BASE_URL`
- `OPENAI_API_KEY`

健康检查等价于下面这条请求（实现为 Dispatcher 进程内的 HTTP 调用，不再在容器里执行 curl）：

```bash
curl -sS --fail -o /dev/null \
  "$CODEX_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "content-type: application/json" \
  -d "{\"model\":\"$CODEX_MODEL\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

已知行为：

- 首轮 session id 会打印在 `stderr`
- 可以用正则 `session id:\s*([0-9a-fA-F-]+)` 提取
- Dispatcher 固定从 `stdout` 取全文作为结果正文

第一阶段执行：

```bash
codex exec --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="cairn"' \
  -c 'model_providers.cairn.name="cairn"' \
  -c 'model_providers.cairn.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.cairn.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.cairn.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

二阶段收尾：

```bash
codex exec resume "{session}" --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="cairn"' \
  -c 'model_providers.cairn.name="cairn"' \
  -c 'model_providers.cairn.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.cairn.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.cairn.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

#### `mock` driver

`mock` driver 用于本地观察 dispatcher 的成功、失败和超时路径。

行为约定：

- `runtime.prompt_group: "mock"` 时，prompt 本身是结构化 JSON，不再依赖自然语言说明
- `reason` prompt 最少包含 `phase`、`fact_ids`、`open_intents`
- `explore` / `conclude` prompt 最少包含 `phase`、`intent_id`
- `bootstrap` / `bootstrap_conclude` prompt 最少包含 `phase`、`origin`、`goal`、`hints`
- driver 会先解析 prompt 里的 `phase` 字段，再读取对应的 `MOCK_<PHASE>` JSON 环境变量选择当前 phase 的模拟结果
- 每个 phase 都在自己的 JSON 里配置 `delay: [min, max]`；单位是秒，支持小数；随机耗时超过 Dispatcher 外层 timeout 时，就会自然表现为超时
- `reason.noop` 只会在 `open_intents` 非空时参与抽样；mock 会自动避开当前上下文下不合法的结果

`mock` 支持六个 phase：

- `healthcheck`
- `bootstrap`
- `bootstrap_conclude`
- `reason`
- `explore_execute`
- `explore_conclude`

支持的结果如下：

- `healthcheck.outcomes`: `ok`、`fail`
- `bootstrap.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `bootstrap_conclude.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `reason.outcomes`: `complete`、`intent`、`noop`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `explore_execute.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `explore_conclude.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`

命名约定：

- 每个 phase 一个变量：`MOCK_<PHASE>`，例如 `MOCK_REASON`
- 变量值必须是 JSON 对象，结构为：`{"delay":[min,max],"outcomes":{...}}`
- `delay` 必须是两个非负数字，单位是秒
- 每个 phase 的所有结果概率都使用 `0` 到 `1` 的小数，并且总和必须严格等于 `1.0`

### 配置校验规则

#### 加载时校验

启动 Dispatcher 时，应完成静态校验：

- `dispatch.yaml` 必须存在且可读取
- `runtime.max_workers` 必须存在
- `runtime.max_running_projects` 必须存在
- `runtime.max_project_workers` 必须存在
- `runtime.interval` 必须存在
- `runtime.healthcheck_timeout` 必须存在
- `runtime.worker_healthcheck` 如果存在，只允许 `startup_and_task`、`startup_only`、`disabled`
- `runtime.prompt_group` 必须存在
- `tasks.bootstrap.timeout` 必须存在
- `tasks.bootstrap.conclude_timeout` 必须存在
- `tasks.reason.timeout` 必须存在
- `tasks.explore.timeout` 必须存在
- `tasks.explore.conclude_timeout` 必须存在
- 每个 Worker 都必须有 `type`
- 每个 Worker 都必须有 `max_running`
- `task_types` 只允许 `bootstrap`、`reason`、`explore`
- `type` 只允许 `claudecode`、`codex`、`mock`
- `max_running` 必须是正整数
- `claudecode`、`codex`、`mock` 都支持双阶段 `explore`
- `runtime.prompt_group` 对应的 prompt 目录必须存在
- 代码工程中的 prompt 资源必须存在
- 默认 prompt 组下，`reason.md` 必须至少覆盖 `{graph_yaml}`、`{fact_ids}`、`{open_intents}`
- 默认 prompt 组下，`explore.md` 必须至少覆盖 `{graph_yaml}`、`{intent_id}`、`{intent_description}`
- 默认 prompt 组下，`bootstrap.md` 和 `bootstrap_conclude.md` 必须至少覆盖 `{origin}`、`{goal}`、`{hints}`
- `mock` prompt 组下，`reason.md` 必须至少覆盖 `{fact_ids}`、`{open_intents}`
- `mock` prompt 组下，`explore.md` 和 `explore_conclude.md` 必须至少覆盖 `{intent_id}`
- `mock` prompt 组下，`bootstrap.md` 和 `bootstrap_conclude.md` 必须至少覆盖 `{origin}`、`{goal}`、`{hints}`
- `mock` worker 的 `MOCK_*` 变量名只能使用系统支持的 phase
- `mock` worker 每个 phase 的概率都必须在 `0` 到 `1` 之间，且总和必须严格等于 `1.0`

#### 运行时校验

任务真正派发时，还需要做运行时校验：

- driver 必须存在且支持该任务类型
- 当 `runtime.worker_healthcheck=startup_and_task` 时，真正启动任务前，driver 的健康检查必须能成功执行；退出码 `0` 才算健康
- `explore` 进入 timeout / parse-fail 后的 `explore_conclude` fallback，不再重复执行健康检查，而是直接尝试 conclude 并继续走结果校验
- 只有支持当前任务类型的 Worker 才能被选中
- 只有当前运行中任务数小于 `max_running` 的 Worker 才能被选中
- 处于本地 `retry_after` 窗口内的不健康 Worker 不参与派发
- `bootstrap` 和 `explore` 都必须先完成 claim，成功后才真正启动任务线程
- 如果要走双阶段 `explore`，则第一阶段必须成功拿到 session id，才能进入 `conclude`
- 如果要走双阶段 `bootstrap`，则第一阶段必须成功拿到 session id，才能进入 `bootstrap_conclude`
- Dispatcher 必须对 `stdout` 全文做 JSON 解析和任务级结构校验
- 保守修复：`explore_conclude` 与 `bootstrap_conclude` 的输出若未通过严格解析或结构校验，Dispatcher 会先做一次确定性的修复尝试（不会再次调用模型），其他阶段的解析行为不变：
  - 仅当整段输出恰好是一个 `{"accepted": true, "data": ...}` 信封（可单独放在一个代码块中）、且各层键与 prompt 要求完全一致时才尝试；拒绝结果、旧格式、信封外还有其他文字都不修复
  - 若输出除字符串内含原始 TAB / LF / CR 等控制字符外是合法 JSON，则按 JSON 解析，出现重复键即放弃
  - 若输出本身不是合法 JSON（通常是描述文本含未转义双引号），则按固定前后缀把中间文本原样作为描述；描述中出现反斜杠、信封结束序列、括号不配对，或在内嵌 JSON 对象之外出现键值分隔时即放弃
  - 修复出的描述不得含 TAB / LF / CR 以外的控制字符，且仍须通过同样的结构校验；任何一步不满足都按原解析失败处理。修复成功时记录一条 warning 日志
  - 修复成功时，conclude 请求附带可选字段 `parse_repair`（`{"strategy": ..., "error": <严格解析的错误>}`），服务端据此为该 Intent 记录一条 `json_repaired` issue，界面在其产出的 Fact 节点上显示提示图标；未经修复的 conclude 请求体与之前完全相同。该字段只作记录，不影响写回结果
- `accepted: false`、JSON 非法、字段缺失、接口写回失败等情况都必须记日志，且不做立即重试

---

## 修复层（影子模式）

修复层是叠加在调度流程之上的可选附加层：在长时间的 `explore` 接近超时时，由独立的修复代理（repair agent）判断这次运行是在推进还是卡住了。当前阶段（Phase 2）是**影子模式**：Dispatcher 登记运行、复制会话记录，修复代理评审并把结论作为影子 issue 记录到服务端，任何结论都不会被执行。服务端的运行表、评审窗口与接口见 `docs/specs/server-protocol.md`「修复层（影子模式）」，修复代理见 `docs/specs/repair-agent.md`。

修复层由两处控制：

- **项目开关** `project.repair_enabled`：保存在服务端，默认关闭；在新建项目对话框中勾选 “Repair long explores”，或用项目标题栏的 Repair 按钮、`PUT /projects/{project_id}/repair` 切换。打开后，该项目的每次 `explore` 运行都会被登记和复制。
- **`dispatch.yaml` 的可选 `repair:` 部分**：Dispatcher 的修复设置与评审模型（reviewer）列表，见下文「`repair:` 配置」。没有这一部分时，Dispatcher 不为修复读写任何东西，也不启动任何东西。

开关关闭时（每个项目的默认状态），运行与今天完全相同。开关打开时，运行同样按今天的方式开始、执行和结束：透传层原样转发每个调用，所有修复 I/O 都在单独的守护线程上进行（container 模式下的容器锁见「会话记录副本」）。

### 接入点 (a)

调度器的唯一改动在 `scheduler/loop.py`，称为接入点 (a)，分两部分（约 41 行新增、1 行删除）。

**第一部分：explore 任务的选择。** `_dispatch_explore` 提交任务时，把原来的 `run_explore_task` 换成 `_explore_task_for(project)` 的返回值：

```python
def _explore_task_for(project: ProjectDetail) -> Callable[..., str]:
    global _repair_module_failed
    if getattr(project.project, "repair_enabled", False) is not True or _repair_module_failed:
        return run_explore_task
    try:
        from cairn.dispatcher.repair.explore import run_explore_task_with_repair
    except Exception:
        _repair_module_failed = True
        LOG.exception("repair module unavailable, running today's explore task project=%s", project.project.id)
        return run_explore_task
    return run_explore_task_with_repair
```

- 它读取本次派发已经取到的 `ProjectDetail`，因此路径在运行开始时选定，运行始终保持该路径，不需要额外的请求。
- 开关的值在到达这里之前都经过解析：Dispatcher 用 `ProjectMeta` 解析每个项目，`repair_enabled` 缺失（旧服务端）时为默认的 false，其 `mode="before"` 校验器把 JSON `true` 以外的任何值（`null`、`1`、`2`、`"yes"`、`"shadow"`）都读作关闭，且从不让其他项目的解析失败。`getattr(…, False) is not True` 的判断针对没有经过该模型的对象：测试替身，以及字段加入之前的 `ProjectMeta`。
- 在开关打开的项目被派发之前，`cairn.dispatcher.repair` 下的任何模块都不会被导入（`dispatch.yaml` 没有 `repair:` 部分时）。
- 调用位于原有的 `try` 之内，因此即使出现意外的异常，也会走今天的释放路径。

**第二部分：修复侧的启动。** `DispatcherLoop.run` 的 `try` 中的第一条语句、启动健康检查之前，调用一次 `_start_repair_side(self.config)`：

```python
def _start_repair_side(config: DispatchConfig) -> None:
    global _repair_module_failed
    if getattr(config, "repair", None) is None or _repair_module_failed:
        return
    try:
        from cairn.dispatcher.repair.housekeeping import start_repair_side
        start_repair_side(config)
    except Exception:
        _repair_module_failed = True
        LOG.exception("repair module unavailable, running today's explore task project=%s", "-")
```

- 没有 `repair:` 部分（`config.repair` 为 None）时立即返回：不导入、不启动、不写任何东西，调度器与今天完全相同。
- 有这一部分时，它导入 `cairn.dispatcher.repair.housekeeping` 并调用 `start_repair_side`，后者启动一个守护线程后立即返回，并自己捕获所有异常；调用也在 `try` 之内，`run()` 永远看不到它的异常。
- 导入失败，或 `start_repair_side` 让异常漏出，会设置与 `_explore_task_for` 相同的标志 `_repair_module_failed`：之后不再尝试该模块，所有运行都使用今天的 explore 任务，并且每个进程只记录一次 `repair module unavailable, running today's explore task project=%s`（此处为 `project=-`）。
- 修复代理的设置文件要在 Dispatcher 启动时写入，被删除项目的副本无论是否有运行在报告都要删除，两者都需要在 Dispatcher 启动时运行一次的代码，而 `DispatcherLoop.run` 是唯一可以修改的位置。`cairn dispatch --startup-healthcheck-only` 调用的是 `run_startup_healthchecks_only` 而不是 `run()`，因此不会启动任何东西；`DispatchConfig.load` 仍然只是解析，没有副作用。

两个函数都不给 `DispatcherLoop` 增加属性。除 `scheduler/loop.py` 与新的 `dispatcher/repair/` 包之外，只有 `dispatcher/config.py` 增加了 `repair` 字段及其三个模型；其他 Dispatcher 文件都没有改动。

### 模块结构

```text
cairn/src/cairn/dispatcher/repair/
  __init__.py      只有 docstring，不导入任何东西
  config.py        dispatcher 令牌的进程级加载与“已配置”状态
  housekeeping.py  start_repair_side：housekeeping 线程、共用 key 检查、修复代理的设置文件、副本删除
  client.py        RepairRunsClient：登记、结束，以及 housekeeping 的两次项目读取；自带 requests.Session，每个线程一个实例
  explore.py       run_explore_task_with_repair、ObservedBackend、ObservedProcess、_quietly
  shadow.py        ShadowRun：任务线程上的钩子与报告线程
  session_ids.py   会话 id 规则、argv 中的 id、输出探测
  transcripts.py   定位、复制、写入、meta.json；每个项目“正在报告的运行”计数
  redaction.py     秘密值的收集与按值脱敏；tools/repair-phase2/redact_eval_set.py 也使用它
```

### 提交的任务：`run_explore_task_with_repair`

```python
def run_explore_task_with_repair(config, client, container_manager, project, export_yaml, intent, worker, cancellation) -> str:
    run = _new_shadow_run(config, container_manager, project, intent, worker, cancellation)  # ShadowRun | None；无 I/O；从不抛出
    backend = container_manager if run is None else ObservedBackend(container_manager, run)
    outcome: str | None = None
    try:
        outcome = run_explore_task(config, client, cast(ContainerManager, backend), project,
                                   export_yaml, intent, worker, cancellation)
        return outcome
    finally:
        if run is not None:
            _quietly(run, "task_finished", run.task_finished, outcome)
```

- 与 `run_explore_task` 的签名相同：同样 8 个位置参数，返回值与异常也相同。`run_explore_task` 自己捕获 `Exception`，因此只有 `BaseException` 可能穿过。`cast()` 在运行时就是原值。
- `_new_shadow_run` 在缓存的配置状态为“未配置”（没有 `repair:` 部分，或令牌无效）时返回 None，不做任何 I/O，此时运行与今天完全相同。否则构造 `ShadowRun`（`run_id = uuid4().hex`，以及项目、Intent、Worker、`config.runtime.execution`、与任务查到的同一个 driver 实例、backend 与 `cancellation`）。任何异常都只记录一条 `repair shadow hook failed` WARNING 并返回 None。
- `cancellation` 是任务的 `TaskCancellation`，即包装函数的第 8 个参数，原样传给 `run_explore_task`。`ShadowRun` 只读取它的 `is_cancelled` 属性，从不调用 `cancel` 或 `attach_process`；副本用它在调度器取消任务时立即释放 Docker 的容器锁。
- `_quietly(run, hook, fn, *args)` 调用 `fn`；遇到 `Exception` 时只以异常类名记录 `repair shadow hook failed … hook=%s error=%s`，并返回 None。

透传规则（每条都有测试）：

- **B1 原样转发。** `write_text_file` 与 `build_exec_process` 以 `*args, **kwargs` 原样转发；`ensure_running` 与 `container_name` 转发其参数；`__getattr__` 转发其余一切。返回值与异常原样传递。从不添加 `kill_after_seconds`。
- **B2 只观察第一次 `build_exec_process`。** 在 `run_explore_task` 中第一次调用总是 execute；第二次是 conclude 收尾，它的真实进程原样返回。健康检查在进程内进行，不是 exec。
- **B3 prompt marker。** 第一次 build 之前 `write_text_file` 写入的路径被保存为 `prompt_marker`：execute 的图快照路径 `/tmp/cairn-prompts/explore_execute-<12 hex>/graph.yaml`。explore prompt 原样包含它，且每次运行都不同。
- **B4 `ObservedProcess`。** `start()` 先调用 `inner.start()` 再调用钩子；若 `inner.start()` 抛出，异常向上传播，什么也不登记。`communicate(t)` 先调用 `inner.communicate(t)` 再调用钩子，返回同一个 `ProcessResult`。`kill()` 与 `cancel(reason)` 直接转发；心跳租约与取消只调用这几个方法。Dispatcher 中没有任何地方检查进程的类型。
- **B5 钩子不破坏运行。** 每个钩子都经过 `_quietly`；任一钩子失败，运行照常进行，只是不再被观察。

### 登记的内容与时机

任务线程上的钩子只保存值：

| 钩子 | 时机 | 作用 |
| --- | --- | --- |
| `note_prompt_file(path)` | 第一次 build 之前的 `write_text_file` | 保存 marker |
| `note_execute(container_name, command, timeout_seconds)` | 第一次 `build_exec_process` | 除非 `timeout_seconds` 是正整数且 `command` 是列表，否则返回 False（不观察该运行）；保存预算与容器名（或本地工作目录）；`claudecode` 还保存 argv 中 `--session-id` 的值 |
| `execute_started(process)` | `inner.start()` 之后 | 记录单调时钟与墙钟的开始时间，保留内层进程，启动报告线程（守护线程，名为 `repair-shadow-<project>-<intent>`）；启动失败会被捕获 |
| `execute_finished(result)` | `inner.communicate()` 之后 | 保存结果与退出时间，设置 `ended` |
| `task_finished(outcome)` | 包装函数的 `finally` | 保存结果，设置 `ended` 与 `task_done`；`communicate()` 抛出时，这是唯一的结束信号 |

报告线程完成所有 I/O，顺序如下：

1. 加入其项目的“正在报告的运行”计数（第 9 步在 `finally` 中离开，无论线程如何结束）。若还没有线程读过令牌，就自己读取。未配置：停止。
2. **登记**：`PUT /repair/runs/{run_id}`，至多 3 次、间隔 2 s，每次重新计算 `started_seconds_ago`。得到 2xx 的那次请求体被保存为已接受的登记体。没有登记成功：停止（不复制，也不发 `/end`）。
3. 在 `ended` 之前：每 1 s 探测一次输出中的 codex 或 pi 会话 id，得到合法的 id 时发送它（session 调用：已接受的登记体加上 `session_id`，至多 2 次）；执行复制计划（见「会话记录副本」）。两步之间在 `ended` 上等待。
4. `ended` 之后：若尚未发送 id，且 driver 从输出中解析 id（codex、pi），对完整输出解析一次（与 explore 任务结束时相同的解析），合法则同样发送。local 模式下只在结束时才出现的 id（包括超时之后）就是这样记录的：session 调用复用已接受的登记体，因此其 `started_seconds_ago` 永远不会被重新计算到超过 `budget_seconds`。对 `claudecode` 与 `mock`、已经发送过 id，或本运行的某次探测抛出过异常时跳过。
5. **结束**：`POST /repair/runs/{run_id}/end`，携带退出字段，至多 2 次，每次重新计算 `exited_seconds_ago`。
6. 等待 `task_done`，至多 `tasks.explore.conclude_timeout + 120 s`。
7. **结果**：`POST /repair/runs/{run_id}/end {"outcome": …}`，至多 2 次。
8. **最终副本**：若运行达到预算的 70%、可评审，且任务没有被取消（`cancellation.is_cancelled` 为假），在 conclude 之后复制一次，因此从不与 conclude 的 Docker 调用竞争。被取消的任务属于正在被停止、完成或删除的项目，调度器的主循环此刻正在检查并停止它的容器。
9. 离开该项目的“正在报告的运行”计数，使项目被删除，或在 `completed_action: remove` 下完成之后，其副本目录可以被删除。副本没有按时间的保留期。
10. 关闭会话，丢弃进程引用。

session 或结束调用被拒绝或得到 422，都不会停止之后的步骤；探测或结束时的解析中出现异常也不会：副本、两次 `/end` 与结果照常进行。

| 常量 | 值 | 说明 |
| --- | --- | --- |
| `PROBE_INTERVAL_SECONDS` | 1.0 | id 进入缓冲区后约 1 s 到达服务端 |
| `PROBE_MAX_CHARS` | 1,048,576 | driver 读取的流（codex：stderr；pi：stdout）达到这么多字符仍没有 id 时停止探测 |
| `START_ATTEMPTS` / `UPDATE_ATTEMPTS` | 3 / 2 | 应对短暂的抖动；更长的中断也会在 2 × `runtime.interval` 之后触发运行自己的心跳租约 |
| `RETRY_DELAY_SECONDS` | 2.0 | |
| 连接 / 读取超时 | 2.0 s / 3.0 s | 被拒绝的连接约 0.001 s 返回；不回答的服务端 3.0 s 后返回 |

报告线程上的最坏情况：登记 19 s，其他每次调用 12 s。任务线程从不等待其中任何一个。

### 登记调用

`RepairRunsClient` 自带 `requests.Session`，每个线程一个实例（每个报告线程一个，housekeeping 线程一个）。它发送 `X-Cairn-Repair-Token: <dispatcher 令牌>`，在 dev-0.3.0 移植中还像该分支的 `CairnClient` 一样发送 `X-API-Key`。id 经 URL 编码后放进路径，与 `CairnClient` 相同；base URL 为 `config.server`。

请求体的键集合是确定的：

- **登记**：`project_id`、`intent_id`、`worker`、`cli_type`（= `worker.type`）、`execution`、`budget_seconds`（execute 的 `timeout_seconds`，即 `tasks.explore.timeout`）、`session_id`（合法的 id 或 null）、`started_seconds_ago`、`locator` {`container`、`prompt_marker`、`dispatcher_started_at`}。local 模式下 `container` 是项目的工作目录。
- **session**：已接受的登记体，填上 `session_id`；不重新计算 `started_seconds_ago`。超时之后才找到的 id，重新计算的值会超过 `budget_seconds`，而服务端只在 PUT 创建运行时检查该值。
- **退出时的结束**：`timed_out`（`did_timeout(result)`）、`cancelled`（`result.cancelled`）、`returncode`、`exited_seconds_ago`；`communicate()` 抛出时，除 `exited_seconds_ago` 外都为 null。
- **结果**：`outcome`（`run_explore_task` 的返回值）。

回答的处理：

| 回答 | 报告线程 |
| --- | --- |
| 2xx | 继续。忽略响应体（Phase 3 才会读取其中的 `deadline_at` 与 `version`）；即使响应体中的截止时间被移动、版本为 2，也不改变任何东西 |
| 0（没有回答）、408、429、500、502、504，或不带 code `token_not_configured` 的 503 | 重试到次数上限，然后为该运行与该调用记录一条 WARNING `repair shadow registration failed …` |
| 401、503 `token_not_configured`，或不带修复层 `{detail: {code}}` 格式的 404（服务端没有修复路由） | 配置错误：每个进程一条 WARNING `repair shadow server not configured status=%s code=%s`；该运行停止报告 |
| 403 `project_inactive`、带 code 的 404、409（`repair_disabled`、`intent_concluded`、`not_explore`、`not_claimed`、`run_conflict`、`session_conflict`） | 预期中的拒绝：每个运行一条 INFO `repair shadow registration refused … call=%s code=%s`。登记被拒绝即停止报告；session 或结束调用被拒绝则不停止 |
| 422 | 版本不一致造成的缺陷，不重试：每个运行与调用一条 WARNING `repair shadow registration failed … call=%s status=422 attempts=1`。登记的 422 停止报告；session 或结束调用的 422 只记录一次，其余调用与最终副本照常进行，session 调用得到 422 之后，两次 `/end` 仍会发送 |

日志中的 `call` 为 `register`、`session`、`end`（退出时的 `/end`）或 `outcome`（结果 `/end`）。

housekeeping 线程用自己的实例只读取两个现有接口，不带 `X-Cairn-Repair-Token`（dev-0.3.0 移植中带 `X-API-Key`，与 `CairnClient` 相同），连接超时 2 s，读取超时取 `CairnClient` 的 10 s：

- `list_projects()`：`GET /projects`，与 `CairnClient.list_projects` 一样用 `TypeAdapter(list[ProjectSummary])` 解析；非 2xx、没有回答或无法解析都视为没有列表，该轮不删除任何东西。
- `project_gone(project_id)`：`GET /projects/{project_id}`，以 `stream=True` 与 `Accept-Encoding: identity` 发送。只有回答是 404、且响应体恰好是 30 字节的 `{"detail":"Project not found"}`（服务端对不存在的项目给出的回答）时才为真。404 的响应体至多读取前 64 字节，其他响应体一概不读，随后关闭响应；不做任何 JSON 解析。

### 会话 id

| CLI | container 模式 | local 模式 |
| --- | --- | --- |
| claudecode | 在登记中，来自 argv（driver 预先生成的 `--session-id`） | 同左 |
| codex | stderr 头部的 `session id: <uuid>`，启动后约 0.1 s 出现在 `ManagedProcess._stderr` 中 → session 调用 | stderr 读满 64 KiB 之后，否则只在结束时 |
| pi | stdout 的第一个事件 `{"type":"session","id":…}`，启动后约 0.05 s 出现在 `ManagedProcess._stdout` 中 | stdout 读满 64 KiB 之后，否则只在结束时 |
| mock | 从不（没有会话记录）；不可评审 | 从不 |

- **探测**：用 `list[:]` 对进程自己的缓冲区做快照（`ManagedProcess._stdout/_stderr`，或 `LocalProcess._stdout_chunks/_stderr_chunks`），在 driver 读取的那个流中至多拼接 `PROBE_MAX_CHARS` 个字符，截到最后一个 `\n`，然后调用 driver 自己的 `extract_session`。chunk 数没有变化时跳过。
- **受保护的解析**：每次探测与结束时的解析都在 `try … except Exception` 中调用 `extract_session`：嵌套足够深的一行会让 `json.loads` 抛出 `RecursionError`，而 pi 的解析只捕获 `json.JSONDecodeError`。异常以类名每个运行记录一次 `repair shadow session probe failed project=%s intent=%s worker=%s run=%s error=%s`；该运行之后的探测与结束时的解析都跳过，视为输出中没有 id，副本改按 marker 查找。它从不停止副本、`/end` 或结果上报。`workers/adapters/pi.py` 没有改动。
- **早出现的 id 就是最终的 id**：codex 的模式 `session id:\s*([0-9a-fA-F-]+)` 不跨越 `\n`，pi 取完整行中的第一个 `session` 事件，因此整行的前缀要么什么也给不出，要么给出与结束时相同的 id。
- **一条规则适用于所有 id**：`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` 且不含 `..`，整串匹配（`re.fullmatch`，从不用 `re.match`，后者的 `$` 也匹配末尾的 `\n` 之前）。Dispatcher 在 id 进入请求体、路径、命令或日志之前检查它，服务端的 `RunRegisterRequest` 与副本也使用同一规则；它还用于在把 `project_id` 用作项目目录名之前检查它。不满足规则的 id 从不使用：运行在没有 id 的情况下登记，并记录一条 WARNING `repair shadow session rejected project=%s intent=%s worker=%s run=%s len=%s sha256=%s`，只给出长度与 SHA-256 的前 12 位十六进制。这是必要的：pi 在 `/bin/sh -lc` 下运行，登录 shell 会读取 `~/.profile`，而之前的运行可以写这个文件，从而伪造 pi 的 id。
- **日志中从不出现会话 id**：运行与会话记录通过 `run_id` 关联；副本的 `meta.json` 保存 `source_path`，`GET /repair/runs/{run_id}` 以 agent 令牌返回 id。
- **local 模式保持今天的 64 KiB 读取器**：id 仍然未知的运行按 prompt marker 查找，修复代理不需要 id。`repair shadow session registered` 的 `after_s` 字段与 `transcript_unreadable` 评审的数量用于衡量是否值得增加逐行读取（那会是 Phase 3 的改动）。

### 会话记录副本

全部在 `repair/transcripts.py` 与 `repair/redaction.py` 中，运行在该运行的报告线程上，只对修复路径的运行存在。修复代理在两种执行模式下都只读取这些副本，从不访问容器、CLI 的主目录或凭据文件，也不需要 Docker 权限。

**计划**：只在登记得到 2xx 之后，只针对 `cli_type` 为 claudecode、codex 或 pi 的运行，并且只针对预算至少 350 s 的运行。已用时间是 Dispatcher 自 `execute_started` 起的单调时钟时间，与服务端的 `started_at` 相差约一秒。

- 第一次复制在 `0.70 × 预算`；之后每 15 s 一个 tick，直到 `0.80 × 预算 + 15 s`。每个 tick 读取源文件的大小与修改时间（stat），只有其中之一自上次成功复制后发生了变化才复制文件。
- **检查时间**：stat 显示源文件自上次成功复制后未变的 tick，会原子地重写 `meta.json`：保留上次成功复制的各键，把 `checked_at` 与 `checked_elapsed_seconds` 设为该 tick 的 stat 时间。`copied_at` 与 `copied_elapsed_seconds` 保持字节被复制的时间；一次复制把两组都设为它自己的时间。没有得到回答的 stat（超时、连接错误或 Docker 的 5xx）不写任何东西，因此不记录新的检查时间。一个长时间运行的工具调用期间，会话文件不会变化；没有检查时间，唯一的副本就会停在调用开始时，在修复代理看来已经过期，而修复代理的新鲜度检查与 digest 的截断点都使用 `checked_elapsed_seconds`。
- 任务结束之后再做一次最终复制（报告线程第 8 步），条件是运行达到 70% 且任务没有被取消；它供审计与盲标使用。源文件未变时只重写 `meta.json`，并设 `final: true`。
- **取消**：每次定位、stat 与复制之前，以及 container 模式下每两次读取数据块之间，计划都检查任务的 `cancellation.is_cancelled`。一旦为真，进行中的复制立即关闭响应（从而释放 Docker 的容器锁），之后不再定位、stat、复制或写 `meta.json`：上一次成功的副本及其 `meta.json` 保持原样。
- 常量：`COPY_START_FRACTION = 0.70`、`COPY_INTERVAL_SECONDS = 15`、`COPY_STOP_FRACTION = 0.80`、`COPY_STOP_GRACE_SECONDS = 15`、`COPY_MAX_BYTES = 64 MiB`、`COPY_TOTAL_SECONDS = 30`。

**定位**（每个运行一次并缓存；找到之前每个 tick 重试）：

| CLI | container 模式 | local 模式 |
| --- | --- | --- |
| claudecode | `${CLAUDE_CONFIG_DIR:-/home/kali/.claude}/projects/<slug(cwd)>/<id>.jsonl`。`CLAUDE_CONFIG_DIR` 取自 `worker.env`；cwd 是容器的 `Config.WorkingDir`（每个运行一次 inspect），没有时为 `/home/kali/workspace`。兜底：`find <config dir>/projects -maxdepth 2 -type f -name <id>.jsonl` | 同样的路径，`HOME` 与 `CLAUDE_CONFIG_DIR` 取自合并环境 `{**os.environ, **worker.env}`，cwd 为 `os.path.realpath(<项目工作目录>)`（CLI 看到的是物理路径）。兜底：glob `<config dir>/projects/*/<id>.jsonl` |
| codex | `find ${CODEX_HOME:-/home/kali/.codex}/sessions -maxdepth 4 -type f -name 'rollout-*-<id>.jsonl'`。codex 按容器的本地日期归档会话，因此从不按 UTC 日期查找 | glob `${CODEX_HOME:-$HOME/.codex}/sessions/*/*/*/rollout-*-<id>.jsonl`（合并环境） |
| pi | `find /tmp/cairn-pi/<worker.name>/sessions -maxdepth 1 -type f -name '*_<id>.jsonl'` | 宿主机上的同一路径 |

- slug：`[a-zA-Z0-9]` 之外的每个字符都变为 `-`；超过 200 个字符的路径会被截断并加上哈希，由兜底搜索覆盖。
- 在容器中，每个 `find` 都是一次以 argv 列表、不经 shell 创建的 exec（`exec_create`），在报告线程上以 `exec_start(socket=True)` 读取。Dispatcher 自己限定这次读取，因为 docker-py 自带的 exec 读取没有超时，而 Worker 能替换容器内的 `timeout` 或 `find`：总共至多 `FIND_READ_SECONDS = 5`（在原始 socket 上用带超时的 `select.poll()` 等待），帧数据至多 `FIND_MAX_BYTES = 1 MiB`，之后关闭 socket。达到任一上限，本 tick 的定位即记为 `not_found`，下一个 tick 再试。这个上限不依赖容器内的任何程序；`find` 之前的 `timeout -k 1 5` 只是第二道保护。exec 没有自己的环境变量，看不到任何 Worker 秘密。
- **id 尚未知时**（codex 或 pi，主要在 local 模式下）：候选为该 CLI 的目录中、修改时间晚于“运行开始 − 60 s”的会话文件，至多 10 个，按新到旧。在容器中是一次 `find <folder> -maxdepth 4 -type f -name '*.jsonl' -newermt @<epoch> -printf '%T@ %p\n'`（同样受限的 exec），每个候选的前 1 MiB 经归档接口读取，然后关闭响应；在本地是 glob 加 `os.stat`。
- **每个候选（无论按 id 还是按 marker 找到）都必须在第一个工具调用之前的某条用户消息中包含本运行的 prompt marker**，且在前 50 行与前 1 MiB 之内。这排除了只是提到该路径的其他运行的文件（例如 `ls /tmp/cairn-prompts` 的输出），因为工具的输出从来不是用户消息。`json.loads` 无法解码的行（`ValueError`，例如写了一半的行；或 `RecursionError`，例如嵌套 200,000 层的行）、解码结果不是对象的行，以及所读字段（如 `type`、`role`、`message`、`payload`、`content`）的类型与该 CLI 所写的不符的记录，都不是用户消息；每行都在同一个保护中解码与检查，任何行的内容都无法让检查抛出。这样的行仍计入 50 行，也不会因为大小而跳过任何行。候选文件按 UTF-8 解码（非法字节替换），只在 `\n` 处分行，从不用 `str.splitlines` 或其他还会在别的字符处断行的读取方式：CLI 会把 U+2028、U+2029 与 U+0085 原样写进 JSON 字符串，而 prompt 包含不可信的 Intent 文本，在这些字符处断行会把 prompt 的记录切成无法解码的片段，运行就得不到副本。空片段（例如最后一个 `\n` 之后的那一段）跳过且不计数。各 CLI：
  - claudecode：`type == "user"`、内容是文本而不是 `tool_result` 的记录；第一个工具调用是 assistant 的 `tool_use` 块。
  - codex：`type` 为 `message`、role 为 `user` 的 `response_item`，或 `type` 为 `user_message` 的 `event_msg`。codex 0.118.0 先写 `<environment_context>` 作为第一条用户消息，prompt 是第二条，因此第一个 `function_call` 之前的任何用户消息都算。
  - pi：role 为 `user` 的 `message` 记录；第一个工具调用是 `toolCall` 块。
- 只有通过会话 id 规则的 id 才会用于路径或命令。

**复制，container 模式**：修复模块使用自己的 Docker 客户端 `docker.from_env(timeout=10)`（首次使用时创建），从不使用 `ContainerManager` 的私有属性，并按名称访问容器。

- docker-py 的 `get_archive` 流式读取时会关闭 socket 超时，不能安全使用。模块直接用客户端自己的 `requests.Session` 访问归档接口：stat 用 `api.head(url, params={"path": p}, timeout=(5, 10))`，读取字节用 `api.get(url, params={"path": p}, stream=True, timeout=(5, 10), headers={"Accept-Encoding": "identity"})`，其中 `url = f"{api.base_url}/v{api.api_version}/containers/{name}/archive"`。
- 除非 stat 的 `linkTarget` 为空、`mode` 不含 Go 的目录、符号链接、命名管道、socket 或设备位，且 `size` 不超过上限，否则拒绝。大小与修改时间自上次成功复制后未变时跳过复制。
- 全速把流读入临时文件，`tarfile.open(fileobj=…, mode="r|")`：恰好一个成员、普通文件、不超过上限、在 `COPY_TOTAL_SECONDS` 之内完成，每两次读取数据块之间检查 `cancellation.is_cancelled`。响应在 `finally` 中关闭，被拒绝与被取消的复制也一样。从不调用 `extract`，也从不把成员名当作路径使用。
- Docker 在归档流被读完并关闭之前一直持有该容器的锁。复制流进行期间，同一容器上的其他调用都会等待：`exec_create`（另一个运行的启动、conclude、kill exec）、`inspect` 与 `put_archive`。其中两个运行在所有项目共享的调度主循环上：已停止、完成或删除项目的任务取消（kill），以及已停止或完成项目的清理 inspect。先 stat、全速读取、在 `finally` 中关闭、任务取消时立即关闭、最终副本只在 conclude 之后且从不为被取消的任务制作，这些使等待保持很短。在真实的 daemon 上验证过（Docker 29.9.2，`COPY_MAX_BYTES` 64 MiB、`COPY_TOTAL_SECONDS` 30，都没有调低）：64 MiB 的复制流进行期间同一容器上的 `exec_create` 与 `inspect`、被拒绝的复制之后的调用，以及在复制期间（在线复制或任务已返回后的最终复制）停止该项目的一次主循环迭代，每次试验都在 1 s 之内完成。

**复制，local 模式**：`os.open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)`，然后 `fstat`：必须是不超过上限的普通文件。`O_NONBLOCK` 防止预先放置的 FIFO 阻塞线程（普通文件忽略该标志）。

**脱敏**（`repair/redaction.py`），写入之前作用于每一个复制的字节：

- `secret_values(config, execution, environ, repair_token)` 收集：
  - 每个已配置 Worker 的 `config.workers[*].env`（已含 `common_env`）中，名称匹配秘密名规则 `(?i)(key|token|secret|pass|auth|credential|cookie)`（搜索匹配，因此 `accessToken`、`refresh_token`、`apiKey`、`primaryApiKey` 都算）、且至少 8 个字符的值。包括所有 Worker，而不只是本运行的：在一个容器中，项目的所有 Worker 共享同一个容器，pi 会把每个 Worker 的 key 写到所有人可读的 `/tmp/cairn-pi/<worker>/models.json`，其他进程的环境可经 `/proc` 读取，而 `kali` 有免密 sudo；
  - local 模式下，Dispatcher 自己的 `os.environ` 中同样的值：local Worker 继承它，而此时 `worker.env` 通常为空；
  - local 模式下，local Worker 所用 CLI 的凭据文件。local Worker 不从 `dispatch.yaml` 或环境中取得 key，而是运行宿主机上的 CLI，使用 CLI 自己文件中的凭据；local Worker 以 Dispatcher 的用户运行，能把它们打印出来。对每个已配置的 Worker，路径在它的合并环境 `{**os.environ, **worker.env}` 中解析，`~` 为该环境的 `HOME`：
    - 只存放凭据的文件 `${CLAUDE_CONFIG_DIR:-~/.claude}/.credentials.json`、`${CODEX_HOME:-~/.codex}/auth.json` 与 `${PI_CODING_AGENT_DIR:-~/.pi/agent}/auth.json`：任意深度上每个至少 16 个字符的字符串值（pi 把 OAuth 令牌存在 `access` 与 `refresh` 下，名称规则匹配不到它们）；
    - `~/.claude.json`（设置了 `CLAUDE_CONFIG_DIR` 时为 `$CLAUDE_CONFIG_DIR/.claude.json`）、`${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json` 的 `env` 对象、`${CODEX_HOME:-~/.codex}/config.toml` 与 `${PI_CODING_AGENT_DIR:-~/.pi/agent}/models.json`：任意深度上每个至少 8 个字符、且自身的键（列表项取列表的键）匹配秘密名规则或为 `key`、`apiKey` 的字符串值；
    - 以上七个文件中，在读取的范围内（`settings.json` 只读其 `env` 对象），任意深度上、不论键名，每个带密码的 URL 字符串值（`scheme://user:pass@host`）里的 `user:pass` 与密码（各至少 8 个字符；百分号编码的密码另给出解码后的形式），同样计入该文件的标签。例如 `settings.json` 的 `env` 或 `config.toml` 的 `shell_environment_policy.set` 中的 `HTTPS_PROXY`（CLI 以及 Worker 运行的每个命令都会看到它），或提供方、MCP 服务器的 URL。

    JSON 文件用 `json.loads` 解析，`config.toml` 用 `tomllib.loads`；解析结果用显式的栈遍历，从不递归。顶层不是对象，或 `settings.json` 的 `env` 不是对象时，不给出任何值。文件缺失、不可读、不是普通文件、无法解析（任何 `ValueError` 或 `RecursionError`）或大于 `CREDENTIAL_FILE_MAX_BYTES = 16 MiB` 时跳过，读取、解析或遍历时抛出其他任何异常的文件也跳过，因此凭据文件永远不会让报告线程停止；过大的文件每个进程每个文件记录一次 `repair shadow credential file too large, skipped file=%s bytes=%s`（`file` 为文件的标签）。每个文件以 `O_RDONLY | O_NONBLOCK` 打开，读取之前先 `fstat`。这些文件在运行第一次被观察时读取，之后每次复制之前再读一次（令牌会轮换），运行保留它读到过的所有值。这些值只保存在内存中，从不记录；匹配次数按文件的标签计入 `meta.json` 的 `redactions`：`claude:.credentials.json`、`claude:.claude.json`、`claude:settings.json`、`codex:auth.json`、`codex:config.toml`、`pi:auth.json` 或 `pi:models.json`。macOS 上 Claude Code 把登录凭据保存在钥匙串中而不是 `.credentials.json` 中，那个令牌只受修复代理模式层的保护；
  - Dispatcher 的修复令牌；
  - `dispatch.yaml` 的 `server` 是带密码的 URL 时（例如位于需要 basic 认证的代理之后的 Cairn 服务端，`requests` 从 URL 中取出它并发送），其中的 `user:pass` 与密码（各至少 8 个字符；百分号编码的密码另给出解码后的形式），计入 `server`：local 模式下 Worker 能在 `dispatch.yaml` 与修复代理的设置文件中读到该 URL 并打印它；
  - 每个评审模型至少 8 个字符的 `REVIEW_API_KEY`，计入 `REVIEW_API_KEY`；它的 `REVIEW_BASE_URL` 与 `proxy` 是带密码的 URL 时（例如需要 basic 认证的模型服务器，或公司代理），其中的 `user:pass` 与密码（各至少 8 个字符；百分号编码的密码另给出解码后的形式），分别计入 `REVIEW_BASE_URL` 与 `REVIEW_PROXY`：local 模式下 Worker 能读取 `dispatch.yaml` 与修复代理的设置文件并打印它们，否则它们会进入 digest；
  - 上述各环境中带密码的 URL 值（`scheme://user:pass@host`，常见于 `*_PROXY` 与 `*_BASE_URL`）里的 `user:pass` 与密码（各至少 8 个字符；百分号编码的密码另给出解码后的形式），计入该变量的名称。
- `redact_exact(data, values)` 把每个值的 UTF-8 字节及其 JSON 转义形式（`json.dumps(v)[1:-1]`，含与不含 `ensure_ascii`）按从长到短替换为 `[REDACTED:<name>]`，其中 name 是变量名、凭据文件的标签，或固定的名称（修复令牌的 `repair_token`，服务端 URL 的 `server`，评审模型代理的 `REVIEW_PROXY`）。它返回每个名称的计数，从不返回值。`/proc/<pid>/environ` 的输出（NUL 分隔）或 JSON 字符串中的值同样会被替换，因为值本身是完整的。
- 修复代理基于模式的脱敏是第二层，处理这组已知值之外的 key；对其他一切（包括 Intent 文本，以及修复代理从服务端读取的 Fact），它是唯一的一层。

**写入**：副本写到 `<root>/<project_id>/<run_id>/`，按项目存放，以便随项目删除（见「副本删除」）。在该目录中，先写 `transcript.jsonl.tmp`、`fsync`，再用 `os.replace` 移为 `transcript.jsonl`；然后以同样方式经 `meta.json.tmp` 写 `meta.json`。除根目录下修复代理的设置文件之外，不会在 `<root>/<project_id>/<run_id>/` 之外写任何东西。`run_id` 必须整串通过服务端的规则，`project_id` 必须整串通过会话 id 的规则；`project_id` 不通过的运行不复制（`repair shadow transcript copy failed … reason=bad_project_id`）。服务端创建的每个 id（`proj_` 加至少三位数字）都能通过。

- **根目录之下不跟随任何符号链接**：每次写入都经由目录描述符，而不是拼接出来的路径。根目录以 `os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)` 打开，项目目录相对它打开，运行目录相对项目目录打开，都用 `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`（`dir_fd=`），缺失时先创建。根目录、项目目录或运行目录处是符号链接或不是目录时，拒绝写入（`repair shadow transcript copy failed … reason=unsafe_path`），什么也不写。文件相对运行目录的描述符创建、替换与删除（`dir_fd=`、`src_dir_fd=`、`dst_dir_fd=`）。
- **权限显式设置**，使持有副本根目录所属组的修复代理用户能读取每个副本，不受 Dispatcher 的 umask 影响，也不论根目录是否设置了 setgid：`root_gid = os.fstat(<根目录描述符>).st_gid`；缺失的项目或运行目录以 `os.mkdir(name, 0o750, dir_fd=<父目录描述符>)` 创建，按上面的方式打开，再 `os.fchown(fd, -1, root_gid)` 与 `os.fchmod(fd, 0o750)`；每个文件以 `os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=<运行目录描述符>)` 创建，然后 `os.fchown(fd, -1, root_gid)` 与 `os.fchmod(fd, 0o640)`，写入后用 `os.replace` 移到位。上一次写入失败留下的 `.tmp` 文件在下一次写入之前以 `os.unlink(name, dir_fd=…)` 删除（它从不跟随符号链接）。umask 从不修改：它是进程级的，其他线程也在创建文件。
- `os.fchown` 抛出 `PermissionError`（Dispatcher 的用户不在 `root_gid` 的组中）时，每个进程记录一次 `repair shadow transcript copy group not set root_gid=%s`，副本保留 Dispatcher 的组，修复代理随后会报告 `transcript_unreadable`。宿主机部署要求把 Dispatcher 的用户加入该组；Compose 中 Dispatcher 以 root 运行。
- 根目录缺失时以 `os.makedirs(root, 0o750)` 创建，再 `os.chmod(root, 0o750)`：在启动时为写设置文件而创建，或者若启动时没有创建，在第一次复制时创建。已存在的根目录从不修改。

`meta.json`（version 1，键是确定的）：

```json
{"version": 1, "run_id": "5f2c9a1e7b3d4c0a9e8d7c6b5a493827", "project_id": "proj_027", "intent_id": "i014",
 "worker": "codex-1", "cli_type": "codex", "status": "ok", "copied_at": "2026-11-14T10:22:16Z",
 "copied_elapsed_seconds": 1336.2, "checked_at": "2026-11-14T10:22:31Z", "checked_elapsed_seconds": 1351.2,
 "source_path": "/home/kali/.codex/sessions/2026/11/14/rollout-…-0199….jsonl",
 "source_bytes": 812345, "copied_bytes": 812301, "sha256": "<of transcript.jsonl as written>",
 "redactions": {"OPENAI_API_KEY": 0}, "copies": 6, "final": false, "located_by": "session_id"}
```

`copied_at` 与 `copied_elapsed_seconds` 表示 `transcript.jsonl` 的字节是何时复制的；`checked_at` 与 `checked_elapsed_seconds` 表示最后一次发现会话文件自那以后未变的时间，刚复制之后两者相等。本例中文件在 1335 s 的 tick 复制，在 1350 s 的 tick 发现未变。时间为 Dispatcher 时钟的 `YYYY-MM-DDTHH:MM:SSZ`；已用秒数是 Dispatcher 自 `execute_started` 起的单调时钟时间。

`status` 为 `ok`、`not_found`、`no_session`、`too_large`、`not_regular` 或 `error`。失败的复制（包括被拒绝的 stat）仍会写入带其状态的 `meta.json`，并保留上一次成功的副本及其 `copied_*` 与 `checked_*` 值；修复代理随后拒绝这个副本，因此过期的副本永远不会看起来是新鲜的。第一次成功复制之前，`copied_*`、`checked_*`、`copied_bytes` 与 `sha256` 为 null，`copies` 为 0，`redactions` 为 `{}`；文件被找到之前，`source_path`、`source_bytes` 与 `located_by` 为 null。`located_by` 为 `session_id`、`prompt_marker` 或 `path`。

### `repair:` 配置

`dispatch.yaml` 的可选 `repair:` 部分是 Dispatcher 的修复设置与评审模型列表。评审模型像 Worker 的模型一样在这里配置，它的 API key 也只在这里填写一次。它取代了早期设计中的 `repair-dispatcher.yaml`：没有单独的 Dispatcher 修复配置文件，也没有 `CAIRN_REPAIR_DISPATCHER_CONFIG` 变量。

```yaml
repair:                                   # 可选；不写时即今天的 Dispatcher，不读、不写、不启动任何东西
  token_file: "/home/<you>/cairn/secrets/repair-dispatcher.token"   # dispatcher 令牌；Compose 中为 /run/secrets/repair-dispatcher.token
  transcript_root: "/var/lib/cairn-repair/transcripts"             # 副本根目录
  projects: null                          # 可选，项目 id 列表；null 表示所有打开开关的项目
  reviewers:                              # 至少一个；门禁回放按顺序逐个运行，在线修复代理使用第一个
    - name: "reviewer_local"
      env:
        REVIEW_MODEL: "<model name>"
        # REVIEW_API_KEY: 这里可以不写（没有认证的本地服务）
        REVIEW_BASE_URL: "http://127.0.0.1:11434/v1"   # OpenAI 兼容的 base URL，按修复代理访问到的地址填写
      context_window: 16384               # 服务端给模型的上下文（token），与服务端的设置相同，绝不能更大；本地模型服务必须设置
    - name: "reviewer_provider"
      env:
        REVIEW_MODEL: "<model name>"
        REVIEW_API_KEY: "<the reviewer's own key>"     # 写在 REVIEW_MODEL 与 REVIEW_BASE_URL 之间（见下文「加载错误会打印什么」）
        REVIEW_BASE_URL: "https://<your provider>/v1"
      timeout_seconds: 180                # 可选的普通设置，不写时取修复代理的默认值
    - name: "reviewer_claude"
      api_format: anthropic_messages      # 可选：Claude Messages API；不写时为 openai_chat
      env:
        REVIEW_MODEL: "<model name>"
        REVIEW_API_KEY: "<the reviewer's own Claude API key>"   # anthropic_messages 必填，以 x-api-key 发送
        REVIEW_BASE_URL: "https://api.anthropic.com"   # 不带 /v1：会追加 /v1/messages
```

**解析**：`DispatchConfig`（`extra="forbid"`）只增加一个字段 `repair: RepairConfig | None = None`。它的模型 `RepairConfig`、`RepairReviewerConfig` 与 `RepairReviewerEnv` 同在 `dispatcher/config.py` 中，都是 `extra="forbid"`，因此加载 `dispatch.yaml` 从不导入 `cairn.dispatcher.repair`。`config.py` 的其他部分不变：`merge_common_env` 只把 `common_env` 合并进 `workers`，从不进入评审模型条目；`validate_workers`、`validate_execution_mode`、其他所有键与错误信息都保持原样。不含 `repair:` 的文件解析结果与今天相同，`repair` 为 None；值为空的 `repair:` 键（例如其下所有键都被注释掉）等同于没有这一部分。修复层之前的 Dispatcher 仍会拒绝该键（见「版本兼容」）。该部分无效时，与其他 `dispatch.yaml` 错误一样：`DispatchConfig.load` 抛出，Dispatcher 不启动，pydantic 的信息给出键名（例如 `repair.reviewers.0.env.REVIEW_BASE_URL`）；段内的错误只给出键名与规则，不打印取值。解析不读取任何文件，也不发送任何请求。

**加载错误会打印什么**：上面“不打印取值”只适用于三个修复模型自己拒绝的内容。下面两类错误不经过段内的隐藏：pydantic 把某个输入的 repr 打印在错误信息中，超过 50 个字符时只打印其开头 25 个与末尾 24 个字符。这些信息出现在 Dispatcher 的标准错误中（Compose 中即 `docker compose logs cairn-dispatcher`）。

- **打印 `repair:` 部分自己的输入**：`repair` 这个键拼错（例如 `repairs:`、`Repair:`；`DispatchConfig` 是 `extra="forbid"`，错误针对这个未知键，输入就是它的值）；整个部分缩进错位，落进某个 Worker 条目（`workers.0.repair`，`WorkerConfig` 也是 `extra="forbid"`），或落进 Worker 的 `env` 或 `common_env`（其值必须是字符串）；以及修复层之前的 Dispatcher 加载含 `repair:` 的文件（见「版本兼容」）。开头是该部分的第一个键（示例中为 `token_file`），末尾是最后一个评审模型条目的最后一个值。
- **打印整个文件的输入**：缺少必填的顶层键（例如把 `tasks:` 写成 `task:` 时缺少 `tasks`；拼错的 `task` 自己的错误只打印它自己的值），以及 `DispatchConfig` 自己的检查（Worker 名称重复、`max_project_workers` 大于 `max_workers`、container 模式下 Worker 缺少 env 键）。开头是文件的第一个键，末尾是文件的最后一个值。
- 缩进错位而落进 `runtime:`、`tasks:`、`container:` 或 `local:` 的 `repair:` 不报错：这些部分忽略未知的键，Dispatcher 像没有 `repair:` 一样启动，打开开关的项目被派发时记录 `repair shadow not configured, runs are not reported reason=no_repair_section`。

因此本文与运行手册的示例都按以下两条书写：

1. **在每个评审模型的 `env` 中，把 `REVIEW_API_KEY` 写在 `REVIEW_MODEL` 与 `REVIEW_BASE_URL` 之间**。这样该部分的末尾是最后一个评审模型的 `REVIEW_BASE_URL`（或写在 `env` 之后的普通设置），开头是 `token_file`，上面两类错误打印的片段中都没有 key。`REVIEW_API_KEY` 写在 `env` 最后时，这些片段含有最后一个评审模型 key 的末尾约 19 个字符，key 不超过 19 个字符时就是整个 key。也不要把它写在 `env` 的第一行：许多 YAML 语法错误会把出错所在映射的第一行一并打印出来（见下）。YAML 中键的顺序不影响加载，设置文件也按固定的键顺序写出每个评审模型（见下文「修复代理的设置文件」），与这里的顺序无关。
2. **把 `repair:` 放在 `workers:` 之前**（例如紧接在 `server:` 之后），不要追加到文件末尾。这样即使某个 `env` 没有按第 1 条书写，关于整个文件的错误打印的末尾也仍是 Worker 条目的内容，与今天 `workers:` 位于最后时相同。`dispatch.example.yaml`、`dispatch.local.example.yaml` 与本文附录的示例都以 `workers:` 结尾，照抄示例再把 `repair:` 加在最后，正是要避免的写法。

`dispatch.yaml` 不是合法的 YAML 时（例如引号未闭合、缩进错位、漏写冒号），错误来自 PyYAML，而不是 pydantic：它打印出错的位置，以及作为上下文的位置（例如出错所在映射的第一行，或未闭合的引号开始的地方），各为该行在标记前后约 30 个字符。因此错误出在 key 附近时，信息中可能有这个 key 的一部分，与今天 Worker 的 key 一样，任何顺序都避免不了：按第 1 条的顺序，是 key 自己的一行与紧挨在它上面的 `REVIEW_MODEL` 一行（例如那一行的引号未闭合）；`REVIEW_API_KEY` 写在 `env` 第一行时，`env` 中之后各行的许多错误（例如值后多出的字符、多缩进一格、多出的引号）都会把 key 那一行作为映射的第一行打印出来。所以 Dispatcher 因 `dispatch.yaml` 无法加载而退出时，把它的标准错误当作可能含有 key 的内容，不要原样贴到别处。

| 键 | 规则 | 含义 |
| --- | --- | --- |
| `token_file` | 必填；展开 `~` 后必须是绝对路径 | 保存 dispatcher 令牌的文件（见下文「令牌」） |
| `transcript_root` | 默认 `~/.local/share/cairn/repair/transcripts`；展开 `~` 后必须是绝对路径 | 副本根目录：每个项目一个副本目录，以及修复代理的设置文件。修复代理以自己的用户运行时，使用家目录之外的路径，例如 `/var/lib/cairn-repair/transcripts`。它应是专用目录，不要在其中存放其他东西（见「副本删除」） |
| `projects` | null（默认），或由 1–128 个字符的字符串组成的列表 | 修复代理可以评审哪些打开开关的项目（见下文「同意」） |
| `reviewers` | 至少一个条目的列表（下表） | 评审运行的模型。在线修复代理使用第一个；门禁回放按顺序在相同的用例上逐个运行 |

评审模型条目的写法与 Worker 条目相同，有 `name` 与 `env`：

| 键 | 规则 | 含义 |
| --- | --- | --- |
| `name` | 必填；整串匹配 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`（`re.fullmatch`）；评审模型之间唯一 | 在日志与回放报告中标识该评审模型 |
| `env.REVIEW_MODEL` | 必填，非空 | 发送给端点的模型名。修复代理在启动时按其规则检查（至多 100 个字符，不含空白、`<` 或 `>`） |
| `env.REVIEW_BASE_URL` | 必填；以 `http://` 或 `https://` 开头 | 端点的 base URL（按修复代理访问到的地址），调用的路径追加在它后面：`openai_chat` 追加 `/chat/completions`，因此写 OpenAI 兼容的 base URL，如 `https://<your provider>/v1`（Compose 中宿主机上的模型服务为 `http://host.docker.internal:<port>/v1`）；`anthropic_messages` 追加 `/v1/messages`，因此不带 `/v1`，写作 `https://api.anthropic.com` |
| `env.REVIEW_API_KEY` | 可选；去掉首尾的 ASCII 空白（与令牌相同），之后为空即等同于不写；必须是可打印 ASCII | 评审模型自己的 key，只在这里填写，只发送给 `REVIEW_BASE_URL`：`openai_chat` 以 `Authorization: Bearer <key>` 发送，`anthropic_messages` 以 `x-api-key: <key>` 发送（该格式必须有 key）。没有认证的本地服务可以不写。设置文件、共用 key 检查与副本脱敏都使用去掉空白后、即实际发送的 key |
| `api_format` | 可选；`openai_chat` 或 `anthropic_messages`；不写或 null 为 `openai_chat` | 调用的线上格式。`openai_chat`（默认）：OpenAI 兼容的 chat completions，适用于任何 OpenAI 兼容的端点，托管服务或本地服务都可以。`anthropic_messages`：Claude Messages API，适用于 Claude API key。Dispatcher 只检查取值是这两者之一；`anthropic_messages` 的评审模型没有 `REVIEW_API_KEY`，或设置了 `structured_output: true`，由修复代理在启动时拒绝，因此这类错误只会让修复代理停止，从不让 Dispatcher 停止 |
| `structured_output`、`max_tokens`、`context_window`、`timeout_seconds`、`max_attempts`、`ca_bundle`、`proxy`、`price` | 可选。`structured_output` 为布尔值；`max_tokens`、`context_window`、`timeout_seconds` 与 `max_attempts` 为 ≥ 1 的整数；`ca_bundle` 与 `proxy` 为非空字符串；`price` 为恰好含 `input_per_mtok` 与 `output_per_mtok` 两个键的对象，各为 ≥ 0 的有限数 | 修复代理模型客户端的设置，含义与默认值见 `docs/specs/repair-agent.md`。不写或为 null 时修复代理取默认值。Dispatcher 只检查这些类型与 `price` 的两个键，取值由修复代理在启动时检查，它不接受的值会让它拒绝启动 |

- `env` 只允许这三个名称，条目只允许这些键，其他任何内容都是错误；仍含 `<` 或 `>` 的字符串（未替换的占位符）也是错误。只有一个模型客户端，它按 `api_format` 发出调用，因此没有 `provider` 键，早期设计中 Anthropic SDK 客户端的设置（`effort`、`server_fallbacks`）也不存在。
- 本地模型服务（例如 Ollama）的评审模型必须设置 `context_window`，等于服务端真正的上下文，绝不能更大，并与服务端的设置一起修改，如示例中的 `reviewer_local`（服务端的上下文设为 16,384）。这类服务端会悄悄截断超过其上下文的 prompt，只保留开头几个 token 与尾部，模型因此看不到系统 prompt 与可信头部。设置了 `context_window` 时，修复代理据此确定每个 digest 的大小，服务端不会截断它；不设置，或大于服务端真正的上下文时，以数字、十六进制或罕见字符为主的运行输出可以让 prompt 超出上下文，而修复代理的 `context_truncated` 检查只能发现上下文低于几千 token 的截断（见 `docs/specs/repair-agent.md`「digest 预算」）。托管的 API 对超长的 prompt 返回错误：设为其模型的上下文，或者不写。
- 每个评审模型都有自己的 key；没有复用某个 Worker 的 provider、模型或 key 的写法。
- 评审模型不是 Worker：它不是 `workers:` 条目，没有 `WorkerType` 或 `TaskType`，调度器、Worker 选择与 Worker 健康检查都看不到它。
- `common_env` 从不合并进评审模型条目，评审模型的 `env` 也从不传给任何进程：修复代理通过设置文件得到这些值。
- Worker 名称除唯一外没有规则；评审模型名称还要满足上面的模式，因为它会出现在日志中，并标记回放结果。

**启动时**：`dispatch.yaml` 含 `repair:` 时，`DispatcherLoop.run` 调用一次 `start_repair_side(config)`。它启动一个守护线程 `repair-housekeeping` 后返回；线程无法启动时记录 `repair shadow housekeeping failed step=start error=%s`，Dispatcher 在没有它的情况下运行。该线程依次：(1) 读取令牌；(2) 检查评审模型的 key（见下文「共用 key」）；(3) 写修复代理的设置文件；(4) 立即执行一次副本清理（见「副本删除」），之后在 Dispatcher 的整个生命周期内每 `HOUSEKEEPING_INTERVAL_SECONDS`（60）秒执行一次。每一步、每一轮清理都在 `try … except Exception` 中运行：异常每个进程每一步只记录一次 `repair shadow housekeeping failed step=%s error=%s`（`step` 为 `start`、`token`、`shared_keys`、`settings` 或 `removal`，`error` 为异常类名），线程继续下一步或下一轮。没有 `repair:` 时这些都不存在。

**令牌**：从 `token_file` 读取，内容去掉首尾的 ASCII 空白（空格、`\t`、`\n`、`\r`、`\v`、`\f`，即 `bytes.strip()` 去掉的字符），至少 32 个字符，每个字节都是可打印 ASCII（0x20–0x7E，即 `value.isascii() and value.isprintable()`）。这与服务端、修复代理对各自令牌文件的规则相同：0x1C–0x1F、U+0085、U+00A0 等其他空白字符不去掉，因此以它们开头或结尾的文件在三处都是 `not_ascii`，服务端的 `roles_configured` 与 Dispatcher 的结论一致。文件对组或其他用户可读（`mode & 0o044`）时，每个进程以 WARNING 记录一次 `repair shadow token file readable by others token_file=%s mode=%s`（mode 为八进制，如 `0640`），令牌仍会使用。

- **加载**：housekeeping 线程在启动时读取；需要令牌而尚无线程读取过的报告线程会自己读取。一把锁保证每个进程只读一次，结果在进程的生命周期内缓存；任务线程从不读取。成功时记录一次 `repair shadow configured transcript_root=%s reviewers=%s projects=%s`（`reviewers` 为数量；`projects` 为列表中的数量，或 `all`）。`dispatch.yaml` 没有 `repair:` 却派发了打开开关的项目，或令牌不可读、不是可打印 ASCII、太短时，每个进程记录一次 `repair shadow not configured, runs are not reported reason=%s`，`reason` 按顺序检查为 `no_repair_section`、`token_unreadable`、`not_ascii`（0x20–0x7E 之外的字节，请求头无法在各处以相同字节携带它）或 `shorter_than_32`。此后 `run_explore_task_with_repair` 用真实的 backend 运行今天的任务：没有 `ShadowRun`，没有线程，没有副本。修复代理的设置文件与副本清理不使用令牌，照常进行。修正后的令牌文件，以及对 `repair:` 的任何修改，都在 Dispatcher 重启后生效。
- Dispatcher 从不从环境变量读取令牌：local 模式下，Dispatcher 上设置的每个变量都会传给每个项目的每个 Worker。

**共用 key**：每个评审模型都有自己的 key。启动时，把每个已设置的 `REVIEW_API_KEY` 与副本脱敏同样收集的 Worker 值按值精确比较：每个 Worker 条目 `env`（含 `common_env`）中的秘密值及其中的 URL 密码，local 模式下还有 Dispatcher 自己环境中的值。每个值按原样以及去掉首尾空白后各比较一次（与评审 key 的处理相同），因此两处写有相同空格的 key 也能匹配。不读取 local CLI 的凭据文件：local 模式下 Dispatcher 只为从副本中删除其值而读取它们，因此与只存在于 local CLI 凭据文件中的 key 相同的评审 key 不会被报告。修复令牌不属于 Worker，不参与比较。匹配时以 WARNING 记录一次 `repair reviewer shares a worker key reviewer=%s worker=%s`，`worker` 按 `dispatch.yaml` 中的顺序以逗号列出匹配的 Worker 名，Dispatcher 自己的环境含有该值时再加上 `environment`；从不打印值。共用是允许的，除这一行日志外不发生任何事。

**修复代理的设置文件**：修复代理从不读取保存了所有 Worker key 的 `dispatch.yaml`。Dispatcher 把它需要的内容写进一个文件 `<transcript_root>/agent-settings.json`：

```json
{"version": 1, "server": "http://127.0.0.1:8000", "projects": null,
 "reviewers": [{"name": "reviewer_local", "model": "<model name>", "base_url": "http://127.0.0.1:11434/v1", "api_key": null,
                "api_format": "openai_chat", "context_window": 16384},
               {"name": "reviewer_provider", "model": "<model name>", "base_url": "https://<your provider>/v1",
                "api_key": "<the reviewer's own key>", "api_format": "openai_chat", "timeout_seconds": 180},
               {"name": "reviewer_claude", "model": "<model name>", "base_url": "https://api.anthropic.com",
                "api_key": "<the reviewer's own Claude API key>", "api_format": "anthropic_messages"}]}
```

- **内容**：`version`（1，第一个键）；`server`，即 `dispatch.yaml` 的 `server`；`projects`，按配置（null 或列表）；`reviewers`，按 `dispatch.yaml` 中的顺序，每个含 `name`、`model`（`REVIEW_MODEL`）、`base_url`（`REVIEW_BASE_URL`）、`api_key`（`REVIEW_API_KEY`，没有时为 null）、`api_format`（按设置；不写或为 null 时写 `openai_chat`，使修复代理永远不需要猜测格式），以及每个已设置且不为 null 的其他可选设置，键名不变。`dispatch.yaml` 的其他内容一概不写：没有 Worker 条目或 `env`，没有 `common_env`，没有令牌或 `token_file`，没有 `transcript_root`。评审 key 与某个 Worker 的 key 相同（见「共用 key」）是 Worker key 的值出现在该文件中的唯一方式；这是允许的，并在启动时记录（除非该 Worker 的 key 只保存在 local CLI 的凭据文件中）。与 dispatcher 令牌相同的 `REVIEW_API_KEY` 从不写入：这次启动不写文件，像写入失败一样删除旧文件，并记录 `repair shadow agent settings not written path=%s error=reviewer_key_is_token`。
- **写入**：只在启动时（上面的第 3 步），方式与副本相同：根目录缺失时按副本的方式创建，然后以 `os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)` 打开；遗留的 `agent-settings.json.tmp` 以 `os.unlink(…, dir_fd=…)` 删除；相对根目录以 `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW`、权限 0640 创建 `agent-settings.json.tmp`，再 `os.fchown(fd, -1, root_gid)` 与 `os.fchmod(fd, 0o640)`，以 `json.dumps(…, ensure_ascii=True, allow_nan=False)` 写入、`os.fsync`、关闭，最后用 `os.replace(…, src_dir_fd=…, dst_dir_fd=…)` 覆盖 `agent-settings.json`。读取方因此只会看到旧文件或新文件，永远不会看到半个文件；作为符号链接的 `agent-settings.json` 被替换而不是被跟随；最后一级是符号链接或不是目录的根目录被拒绝，什么也不写。
- 成功时记录 `repair shadow agent settings written path=%s reviewers=%s`（INFO）。失败时记录 `repair shadow agent settings not written path=%s error=%s`（WARNING；`error` 为异常类名、被拒绝根目录的 `unsafe_path`，或 `reviewer_key_is_token`），并且在根目录已安全打开时，以 `os.unlink("agent-settings.json", dir_fd=<根目录描述符>)` 删除旧的 `agent-settings.json`（文件不存在不算错误），使修复代理拒绝启动（`agent settings missing`），而不是使用 `dispatch.yaml` 中已经不存在的评审模型、key 或 `projects` 列表。根目录被拒绝时什么也不动；修复代理启动时的 `repair agent started … reviewer=… projects=…` 一行显示它使用的是哪份设置。`os.fchown` 的 `PermissionError` 按副本的方式处理：每个进程一条 `repair shadow transcript copy group not set root_gid=%s`，文件保留 Dispatcher 的组，修复代理的用户因此无法读取它。
- 修复代理只在自己启动时读取该文件。修改 `repair:` 之后，先重启 Dispatcher，等它记录 `repair shadow agent settings written` 之后，再重启修复代理。
- **谁能修改它**：该文件与副本根目录属于 Dispatcher 的用户。container 模式下 Worker 运行在创建时没有任何挂载的项目容器中，Worker 能控制的东西都写不到它。local 模式下 Worker 以 Dispatcher 的用户运行：它们能读取并修改 `dispatch.yaml` 与该文件，因此 local Worker 能读取评审模型的 key，或把评审模型指向它自己的监听端口，由后者接收 digest 并决定评审结论。Phase 2 什么也不执行，因此接受这一点；修复代理在该文件不属于 root 的每次启动时（即每个宿主机部署）记录一条 WARNING，评审模型的设置必须在 Phase 3 执行任何决定之前得到保护。

**同意（`projects`）**：同意把 digest 发送给评审模型，即打开项目开关：每个打开开关的项目都可以被评审。`projects` 只能缩小这个范围：给出列表时，修复代理只评审列表中那些打开了开关的项目。列表放在 `dispatch.yaml` 中，是因为主线上 `PUT /projects/{project_id}/repair` 是开放的：任何能访问服务端的东西（包括使用主机网络的容器 Worker）都能打开某个项目的开关，从而把该项目的 digest 发送给评审模型；放在任何 HTTP 请求都改不了的文件中的列表，把同意固定到具名的项目上（local 模式下 Worker 仍能编辑该文件）。Dispatcher 自己不使用 `projects`：它照常登记并复制所有打开开关项目的运行，只把列表传下去。

### 副本删除

副本遵循 Cairn 自己的数据处理：被删除项目的数据随项目一起删除（服务端的修复表行由外键级联删除），完成项目的容器或工作目录只在 `completed_action: remove` 下才删除。副本没有按时间的保留期。housekeeping 线程（只在 `dispatch.yaml` 有 `repair:` 时存在）在每一轮（启动时一次，之后每 60 s）检查两种情况，删除项目的副本目录。每一轮先列出根目录：若没有任何条目是名称通过会话 id 规则、不是符号链接、且含有条目的目录（即项目目录；空目录不算，见下文「只含副本」），就不发送请求，也不删除任何东西。两种情况，以及每次删除都遵守的规则：

- **项目已被删除**：该轮列出项目（`list_projects()`）。对根目录下名称通过 id 规则、项目不在列表中、且只含副本（见下）的每个目录，询问 `project_gone(project_id)`：只有带服务端精确的 “Project not found” 响应体的 404 才删除该目录，原因为 `deleted`。其他任何回答（其他 404 响应体，例如没有该路由的服务端返回的 `{"detail":"Not Found"}`；401；5xx）、没有回答，或没有列表，都保留所有目录，留待之后的某一轮。因此被删除项目的副本在约一分钟内消失；Dispatcher 停机期间被删除的项目，其副本在它下次启动时消失。
- **项目在 `completed_action: remove` 下完成**：列表显示项目为 `completed`，且所用执行模式的 `completed_action`（`container.completed_action`，local 模式下为 `local.completed_action`）为 `remove` 时，删除该目录，原因为 `completed`。同样的情况下，今天的清理会删除项目的容器或工作目录，副本在一轮之内跟随。那些清理路径没有改动：修复侧从它自己取得的列表中读取项目状态。`completed_action` 为 `stop` 或 `keep`，以及项目已停止时，副本与容器或目录一样保留，直到项目被删除。在清理之前被 reopen 的项目重新变为 `active`，保留它的副本。
- **项目有运行正在报告时不删除**：只要本进程中该项目“正在报告的运行”计数大于 0，就跳过该目录，下一轮再试。检查与删除持有报告线程加入与离开计数时使用的同一把锁，因此没有报告线程会写入正在被删除的目录，被删除的目录也只会被之后开始的运行（被 reopen 的项目的运行）重新创建。调度器会取消被删除、停止或完成项目的任务，它们的副本立即停止，也没有最终副本，因此它们的报告线程很快就会结束。
- **只含副本**：只有当目录至少含有一个运行目录、且除副本外什么也没有时才删除：每个条目（以 `os.lstat(name, dir_fd=<目录描述符>)` 检查）都是不是符号链接的目录，名称是本 Dispatcher 生成的运行 id（`uuid4().hex`，`re.fullmatch(r"[0-9a-f]{32}", name)`），且每个运行目录中的每个条目都是名为 `transcript.jsonl`、`meta.json`、`transcript.jsonl.tmp` 或 `meta.json.tmp` 的普通文件。这一检查在询问 `project_gone` 之前进行，`deleted` 与 `completed` 两种情况都一样。删除时只 unlink 检查过的那些文件（`os.unlink(name, dir_fd=…)`），再以 `os.rmdir(name, dir_fd=…)` 依次删除每个运行目录和项目目录，从不使用 `shutil.rmtree`，因此期间新增的任何条目都会让 `rmdir` 失败，目录得以保留。含有其他任何东西的目录被保留，每个目录每个进程记录一次 `repair shadow copies not removed project=%s reason=%s error=not_copies`（`reason` 为所检查的情况），也不为它发送请求。没有条目的目录不含副本：既不询问也不删除，也不记录日志，在列出根目录之后才被清空的目录也是如此——空目录中没有任何东西能证明它是本 Dispatcher 创建的（家目录的 `Templates`，或 `transcript_root` 指向 `local.workspace_root` 时被删除项目的空工作目录）。代价是：Dispatcher 自己留下的空项目目录（复制在创建它之后、创建运行目录之前失败）会一直保留，直到你手动删除。无法打开或列出的目录不当作空目录：它算作项目目录，因此该轮会发送 `GET /projects`，在删除或完成的情况下它被保留，并记录一次 `repair shadow copies not removed … error=<异常类名>`。所以你放在副本根目录中的目录（例如评估集），以及指向含有其他数据的目录（你的家目录、仓库、`datas/`，或 `local.workspace_root`——今天的代码会保留其中被删除项目的工作目录）的 `transcript_root`，都不会丢失任何东西。
- **安全**：根目录以 `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` 打开。只有 `os.lstat(name, dir_fd=…)` 显示为目录、且不是符号链接的条目才可能被删除，并且按上一条的方式、经由以 `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` 打开的描述符删除，不跟随任何符号链接。名字像项目的符号链接或文件、名称不通过 id 规则的条目，以及 `agent-settings.json` 及其临时文件，从不被删除。每次删除记录 `repair shadow copies removed project=%s reason=%s count=%s`（INFO；`count` 为其中运行目录的数量）。失败时记录 `repair shadow copies not removed project=%s reason=%s error=%s`（WARNING，异常类名，每个项目每个进程一次），下一轮重试。
- 副本属于 `dispatch.yaml` 的 `server` 所指服务端的项目。若把它指向另一个服务端，Dispatcher 会删除那个服务端没有的所有项目的副本。
- `dispatch.yaml` 没有 `repair:` 时什么也不删除；此时根目录中留下的副本会一直保留，直到你手动删除。

### 运行看到的内容，以及其他差异

开关打开时保持相同的：argv、env、timeout、进程调用序列（`start`、`communicate(N + 15)`，以及来自心跳租约或取消的 `kill`/`cancel`）、图快照、运行自己的服务端请求、结果（因此调度器的回收与冷却也相同），以及所有已有的日志行。

修复路径的运行增加了：

- 每个运行一个守护线程；
- 每个运行至多六个发往服务端的小请求，每个都是一次简短的 SQLite 写入；
- container 模式下：在 Worker 的容器中执行一次容器 inspect（claudecode），或一两次 `find` exec（codex、pi），容器内的进程列表可能看到它；1800 s 的预算至多 14 次归档 stat 与复制，外加最终的一次；
- container 模式下，每次复制流进行期间 Docker 持有该容器的锁（见「会话记录副本」）。所有项目（包括开关关闭的项目）的派发、回收与取消，可能因此延迟至多一次复制的时间，在已验证的 1 s 界限之内。被取消任务的复制会立即关闭流，任务也没有最终副本，因此停止一个任务正在运行的项目，只需等待正在进行的那次数据块读取。最终副本在任务返回之后制作，而 `_reap_futures` 会在 `_cancel_inactive_tasks` 之前把已返回的任务移出待取消的集合，因此没有东西会取消最终副本；最终副本进行期间发生的停止或完成，会让清理 inspect 等待该副本结束，至多 `COPY_TOTAL_SECONDS`，且在同一 1 s 界限之内；
- 每次复制至多 64 MiB 的宿主机磁盘写入，以及文件未变的每个 tick 一次 `meta.json` 重写；
- 新的日志行。

`dispatch.yaml` 中的 `repair:` 部分，无论开关如何，增加了：

- 启动时导入 `cairn.dispatcher.repair` 包，以及一个在 Dispatcher 的整个生命周期内存在的守护线程 `repair-housekeeping`；
- 启动时：读取一次令牌文件、共用 key 检查、缺失时创建副本根目录、写修复代理的设置文件；
- 副本根目录中有项目目录（名称通过 id 规则且含有条目的目录；空目录不算）时，每 60 s 一次 `GET /projects`；对项目已离开该列表、且只含副本的每个这样的目录，一次 `GET /projects/{project_id}`。两者都不带修复令牌，各在 uvicorn 的访问日志中多出一行，与调度器每 `runtime.interval` 一次的轮询并列。`GET /projects` 在回答之前会执行超时清理，与调度器自己的轮询以及界面的轮询相同，因此超时的 claim 可能在 housekeeping 的这次调用时就被清除，至多比调度器的下一次轮询早 `runtime.interval` 秒；
- 删除被删除项目的副本目录，以及 `completed_action: remove` 下完成的项目的副本目录。

没有 `repair:` 时以上都不会发生。

### 版本兼容

| Dispatcher / 服务端 | 结果 |
| --- | --- |
| 新 Dispatcher，旧服务端 | 缺少 `repair_enabled`，即关闭：今天的任务 |
| 旧 Dispatcher，新服务端 | 该字段被忽略（pydantic 默认的 `extra="ignore"`），因此不登记、不评审；运行手册的检查能发现这种情况 |
| 新 Dispatcher，服务端没有配置令牌 | `503 token_not_configured`：每个进程一条 WARNING；运行不受影响 |
| 带 `repair:` 的新 Dispatcher，旧服务端 | 缺少 `repair_enabled`，因此所有项目都关闭，不登记任何运行。设置文件照常写入，housekeeping 照常运行：它只使用 `GET /projects` 与 `GET /projects/{project_id}`，旧服务端对被删除项目的 404 响应体相同 |
| 旧 Dispatcher（修复层之前），`dispatch.yaml` 含 `repair:` | 加载时被拒绝，值为空的 `repair:` 键也一样：`DispatchConfig` 是 `extra="forbid"`。错误针对 `repair` 这个未知键，打印该部分输入的开头 25 个与末尾 24 个字符：`REVIEW_API_KEY` 按「`repair:` 配置」写在 `REVIEW_MODEL` 与 `REVIEW_BASE_URL` 之间时其中没有 key，写在 `env` 最后时其中有最后一个评审模型 key 的末尾。回滚 Dispatcher 之前删除这一部分，包括 `repair:` 这一行 |

### 日志

logger 为 `cairn.dispatcher.repair.*`，`DispatcherLogFormatter` 显示为 `repair.*`。除调度器的那一行与共用 key 的那一行以 `repair ` 开头外，每条消息都以 `repair shadow ` 开头。这是完整的列表：修复模块不记录其他任何行，且没有一行匹配 Phase 0 日志解析器（`tools/repair-phase0/baseline_from_logs.py --check-formats`）的模式。这些日志行中从不出现令牌、key、会话 id 或会话记录的文本。`dispatch.yaml` 无法加载时 Dispatcher 打印的 pydantic 或 YAML 错误不是修复模块的日志行，其中可能有输入的片段：`REVIEW_API_KEY` 写在 `REVIEW_MODEL` 与 `REVIEW_BASE_URL` 之间、`repair:` 放在 `workers:` 之前时，pydantic 的错误中没有评审模型的 key（见「`repair:` 配置」中的「加载错误会打印什么」）。

```text
INFO    repair shadow configured transcript_root=%s reviewers=%s projects=%s                              每个进程一次；projects 为列表中的数量或 all
WARNING repair shadow not configured, runs are not reported reason=%s                                     每个进程一次；reason 见「令牌」
WARNING repair shadow token file readable by others token_file=%s mode=%s                                 每个进程一次；mode 为八进制
WARNING repair reviewer shares a worker key reviewer=%s worker=%s                                         启动时，每个与 Worker 共用 key 的评审模型一次；只有名称
INFO    repair shadow agent settings written path=%s reviewers=%s                                         启动时
WARNING repair shadow agent settings not written path=%s error=%s                                         启动时；error 为异常类名、unsafe_path 或 reviewer_key_is_token
WARNING repair shadow housekeeping failed step=%s error=%s                                                每个进程每一步一次；error 为异常类名
INFO    repair shadow run registered project=%s intent=%s worker=%s run=%s cli=%s budget=%ss session=%s   session=yes|no
INFO    repair shadow session registered project=%s intent=%s worker=%s run=%s source=%s after_s=%s      source=argv|output|final_output
WARNING repair shadow session rejected project=%s intent=%s worker=%s run=%s len=%s sha256=%s
WARNING repair shadow session probe failed project=%s intent=%s worker=%s run=%s error=%s               每个运行一次；error 为异常类名
INFO    repair shadow run ended project=%s intent=%s worker=%s run=%s code=%s timed_out=%s cancelled=%s execute_s=%s
INFO    repair shadow outcome reported project=%s intent=%s run=%s outcome=%s
INFO    repair shadow registration refused project=%s intent=%s worker=%s run=%s call=%s status=%s code=%s
WARNING repair shadow registration failed project=%s intent=%s worker=%s run=%s call=%s status=%s attempts=%s   每个运行与调用一次：最后一次尝试之后，422 则立即（status=422 attempts=1），登记与 session 调用也一样
WARNING repair shadow server not configured status=%s code=%s                                             每个进程一次
INFO    repair shadow transcript copied project=%s intent=%s run=%s bytes=%s redactions=%s copy=%s        第一次复制；之后的复制为 DEBUG
WARNING repair shadow transcript copy failed project=%s intent=%s run=%s reason=%s                        每个运行每个原因一次；包括 unsafe_path 与 bad_project_id
WARNING repair shadow transcript copy group not set root_gid=%s                                           每个进程一次；修复代理的设置文件也一样
WARNING repair shadow credential file too large, skipped file=%s bytes=%s                                 每个进程每个文件一次；file 为文件的标签
INFO    repair shadow copies removed project=%s reason=%s count=%s                                        reason = deleted|completed
WARNING repair shadow copies not removed project=%s reason=%s error=%s                                    每个项目每个进程一次；error 为异常类名或 not_copies
WARNING repair shadow hook failed project=%s intent=%s worker=%s run=%s hook=%s error=%s                  error 为异常类名
WARNING repair shadow reporter failed project=%s intent=%s worker=%s run=%s                               带 traceback
ERROR   repair module unavailable, running today's explore task project=%s                                调度器，每个进程一次；启动时导入失败则 project=-
```

### 保持不变的内容

开关关闭时（每个项目的默认状态）：

| 保持相同的 | 原因 |
| --- | --- |
| 调度器为 explore 提交的函数及其 8 个参数 | 接入点 (a) 只在 `repair_enabled is True` 时才返回别的任务；`ProjectMeta` 只把 JSON 的 `true` 读作打开，该字段的任何值都不会让项目读取失败 |
| Dispatcher 加载的模块 | 没有 `repair:` 部分时，派发打开开关的项目之前不导入 `cairn.dispatcher.repair` 下的任何东西；有该部分时在启动时导入，并增加上文所列的内容 |
| Worker 的命令、env、timeout、进程调用、运行的服务端请求、结果与日志行 | 除接入点 (a) 的两处外，没有编辑任何现有的 Dispatcher 函数；`DispatchConfig` 只增加一个可选字段 `repair` |
| 现有的 `dispatch.yaml` 文件 | `DispatchConfig` 保持 `extra="forbid"`，只增加默认为 None 的 `repair`：不含 `repair:` 的文件解析结果、每个已有的键、校验与错误信息都与今天相同，Dispatcher 不为修复读写任何东西。读取 `repair:` 部分不发送网络请求，也不改变调度器、Worker 检查或任务路径 |

开关打开时（影子模式），运行同样按今天的方式开始、执行和结束（argv、env、timeout、进程调用序列、运行自己的服务端请求、结果、已有日志行）：透传层原样转发每个调用，所有修复 I/O 都在单独的守护线程上；任何结论都不会被执行，Dispatcher 忽略修复接口的响应体。修复层的日志行中从不出现令牌、key、会话 id、digest、模型原始输出或评审文本（`dispatch.yaml` 加载失败时打印的错误见「`repair:` 配置」中的「加载错误会打印什么」）。

---

## 配置字段速查

### `dispatch.yaml`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `server` | 是 | Cairn Server 的 base URL |

### `runtime.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `runtime.max_workers` | 是 | Dispatcher 同时运行中的任务总数上限 |
| `runtime.max_running_projects` | 是 | 当前 dispatcher 运行期内已接手且仍为 `active` 的项目上限，不受历史遗留容器影响 |
| `runtime.max_project_workers` | 是 | 单个项目内同时运行的任务上限，统一计入 `bootstrap`、`reason` 和 `explore` |
| `runtime.interval` | 是 | 统一节拍配置；既是 Dispatcher 主循环间隔，也是带 claim 任务的 heartbeat 周期 |
| `runtime.healthcheck_timeout` | 是 | Worker 健康检查的统一外层 watchdog 超时 |
| `runtime.worker_healthcheck` | 否 | Worker 健康检查模式：`startup_and_task`、`startup_only` 或 `disabled`；默认 `startup_only` |
| `runtime.execution` | 否 | 执行后端：`container`（默认）或 `local`；`local` 时 worker 在 dispatcher 宿主机上以子进程运行，复用本机 CLI，启动时校验各 CLI 是否已安装可用 |
| `runtime.prompt_group` | 是 | 当前使用的 prompt 组目录名 |

### `container.*`

仅 `runtime.execution: container`（默认）时必填。

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `container.image` | 是 | 项目容器镜像 |
| `container.network_mode` | 是 | 项目容器网络模式 |
| `container.completed_action` | 是 | 项目 completed 后对容器的处理方式 |

`container.completed_action` 可选值：

- `remove`：项目 completed 后删除容器
- `stop`：项目 completed 后只停止容器，保留现场

实现约定：

- completed project 的容器 cleanup 可以异步并行进行，不要求阻塞主调度循环
- 如果项目已从 Server 删除，Dispatcher 会把找不到对应项目的 `cairn-dispatch-*` 容器视为 orphan，并执行 stop 清理

### `local.*`

仅 `runtime.execution: local` 时生效，`container` 模式下忽略。此时无需 `container.*`，worker 也不需要任何 LLM 环境变量；启动时 Dispatcher 会对每个已配置 worker 的 CLI 执行 `--help` 探测，全部缺失则报错退出。

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `local.workspace_root` | 否 | 每项目工作目录的根；不填则取 dispatcher 启动时的当前目录，每项目分到隔离子目录 `<root>/<project_id>/` 作为 worker 进程的工作目录 |
| `local.completed_action` | 否 | 项目 completed 后对工作目录的处理：`keep`（默认，保留现场）或 `remove` |

### `tasks.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `tasks.bootstrap.timeout` | 是 | `bootstrap` 第一阶段超时 |
| `tasks.bootstrap.conclude_timeout` | 是 | `bootstrap` 双阶段收尾超时 |
| `tasks.reason.timeout` | 是 | `reason` 的超时 |
| `tasks.explore.timeout` | 是 | `explore` 第一阶段超时 |
| `tasks.explore.conclude_timeout` | 是 | `explore` 双阶段收尾超时 |

### `workers.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `name` | 是 | Worker 静态标识；协议写回时使用这个值作为 `creator` 或 `worker` |
| `type` | 是 | Worker driver 名；支持 `claudecode`、`codex`、`mock` |
| `task_types` | 是 | 该 Worker 支持的任务类型列表 |
| `max_running` | 是 | 该 Worker 自身的并发上限 |
| `priority` | 是 | 当前任务类型的候选 Worker 中，数字越小优先级越高 |
| `env` | 是 | 该 Worker 的变量表；具体必需 key 由对应 driver 决定并在启动时校验 |

补充：

- Worker 选择顺序是：先过滤任务类型、`max_running` 和处于本地 `retry_after` 窗口内的 Worker，再按 `priority`，同优先级优先选当前运行数更少的，最后随机；`bootstrap` 和 `explore` 都会先 claim，再启动任务；当 `runtime.worker_healthcheck=startup_and_task` 时，真正启动前会做一次健康检查，失败的 Worker 会进入短暂不可选窗口；进入 `bootstrap_conclude` / `explore_conclude` fallback 时不再重复健康检查
- 健康检查、执行命令、session 提取、二阶段 `conclude` 都由对应 driver 代码负责
- prompt 内容从代码工程里的 markdown 资源加载

### `repair.*`

可选；整个部分不写时，Dispatcher 不为修复读写或启动任何东西。说明见「修复层（影子模式）」›「`repair:` 配置」。

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `repair.token_file` | 是 | dispatcher 令牌文件；展开 `~` 后必须是绝对路径；内容去掉首尾的 ASCII 空白后至少 32 个可打印 ASCII 字符 |
| `repair.transcript_root` | 否 | 副本根目录，默认 `~/.local/share/cairn/repair/transcripts`；展开 `~` 后必须是绝对路径；也保存修复代理的设置文件 `agent-settings.json`；应是专用目录 |
| `repair.projects` | 否 | null（默认，所有打开开关的项目）或项目 id 列表，只能缩小可评审的项目 |
| `repair.reviewers` | 是 | 评审模型列表，至少一个；在线修复代理使用第一个，门禁回放逐个运行 |
| `repair.reviewers[].name` | 是 | 评审模型名，整串匹配 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`，评审模型之间唯一 |
| `repair.reviewers[].env.REVIEW_MODEL` | 是 | 发送给端点的模型名 |
| `repair.reviewers[].env.REVIEW_BASE_URL` | 是 | 以 `http://` 或 `https://` 开头的 base URL；`openai_chat` 追加 `/chat/completions`，`anthropic_messages` 追加 `/v1/messages` |
| `repair.reviewers[].env.REVIEW_API_KEY` | 否 | 评审模型自己的 key（`anthropic_messages` 必填）；去掉首尾的 ASCII 空白，可打印 ASCII |
| `repair.reviewers[].api_format` | 否 | `openai_chat`（默认）或 `anthropic_messages` |
| `repair.reviewers[].structured_output` | 否 | 布尔值，修复代理默认 true；只用于 `openai_chat` |
| `repair.reviewers[].max_tokens` | 否 | ≥ 1 的整数，修复代理默认 4096 |
| `repair.reviewers[].context_window` | 否 | ≥ 1 的整数，默认 null；服务端给该评审模型的上下文（token），决定发给它的 digest 预算与 room（每个 digest 的 UTF-8 字节上限）。本地模型服务（例如 Ollama，会悄悄截断超过其上下文的 prompt）必须设置为服务端真正的上下文，绝不能更大（见 `docs/specs/repair-agent.md`「digest 预算」）；默认 null 只适合不会截断 prompt 的服务端 |
| `repair.reviewers[].timeout_seconds` | 否 | ≥ 1 的整数，默认 180；每次尝试的时限 |
| `repair.reviewers[].max_attempts` | 否 | ≥ 1 的整数，默认 2 |
| `repair.reviewers[].ca_bundle` | 否 | 非空字符串：https `base_url` 的 CA 文件，按修复代理看到的路径 |
| `repair.reviewers[].proxy` | 否 | 非空字符串：模型客户端唯一使用的代理 |
| `repair.reviewers[].price` | 否 | 恰好含 `input_per_mtok` 与 `output_per_mtok`，各为 ≥ 0 的有限数；只供回放与门禁报告计算成本 |

补充：

- 评审模型不是 Worker：不参与调度、Worker 选择与健康检查；`common_env` 从不合并进评审模型条目
- 每个评审模型都用自己的 key，只在这里填写一次；`dispatch.yaml` 因此也保存评审模型的 key，应保持 0600 权限
- 在每个评审模型的 `env` 中把 `REVIEW_API_KEY` 写在 `REVIEW_MODEL` 与 `REVIEW_BASE_URL` 之间，并把 `repair:` 放在 `workers:` 之前，不要放在文件末尾：拼错的 `repair:` 键、缩进错位而落进 Worker 条目（或其 `env`、`common_env`）的 `repair:`，以及修复层之前的 Dispatcher，会打印该部分输入的开头 25 个与末尾 24 个字符，关于整个文件的加载错误会打印整个文件输入的开头与末尾；`REVIEW_API_KEY` 写在 `env` 最后时，其中就有最后一个评审模型 key 的末尾。YAML 语法错误会打印出错位置附近的行，可能含有 key 的一部分，不要把加载失败时的输出原样贴到别处
- 修改 `repair:` 之后先重启 Dispatcher，再重启修复代理

---

## 附录：示例配置与 Prompt 内容

### `dispatch.example.yaml`

```yaml
server: "http://127.0.0.1:8000"

runtime:
  max_workers: 5  # total running tasks
  max_running_projects: 3  # total active projects admitted by this dispatcher runtime
  max_project_workers: 2  # per-project running tasks, including bootstrap + reason + explore
  interval: 3  # intentional shared cadence: scheduler loop interval + claim-task heartbeat interval, in seconds
  healthcheck_timeout: 15  # shared watchdog for all worker healthchecks, in seconds
  worker_healthcheck: "startup_only"  # startup_and_task | startup_only | disabled
  prompt_group: "default"  # selects prompts/<group>/

tasks:
  bootstrap:
    timeout: 120
    conclude_timeout: 30
  reason:
    timeout: 45
  explore:
    timeout: 600
    conclude_timeout: 120

container:
  image: "tmp:latest"
  network_mode: "host"
  completed_action: "stop"  # options: "remove" | "stop"

common_env:
  TSEC_BASE_URL: "http://<SERVER_HOST>/api"
  TSEC_AGENT_TOKEN: "..."

workers:
  # 同一模型拆成多个 Worker 的原因，应是它们使用不同的 API key，
  # 从而拥有彼此独立的并发配额。
  - name: "claude-sonnet-thinker"
    type: "claudecode"
    task_types: [bootstrap, reason]
    max_running: 1
    priority: 0  # lower number wins; ties prefer fewer running tasks, then choose randomly
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-a"

  - name: "claude-sonnet-doer"
    type: "claudecode"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 1
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-b"

  - name: "codex-gpt54"
    type: "codex"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 3
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-c"

  - name: "codex-gpt54-alt"
    type: "codex"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 4
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-d"

  - name: "mock-observer"
    type: "mock"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 9
    env:
      MOCK_HEALTHCHECK: '{"delay":[0.05,0.15],"outcomes":{"ok":0.9,"fail":0.1}}'
      MOCK_BOOTSTRAP: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_BOOTSTRAP_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_REASON: '{"delay":[0.1,2.2],"outcomes":{"complete":0.1,"intent":0.3,"noop":0.1,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.2}}'
      MOCK_EXPLORE_EXECUTE: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_EXPLORE_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
```

补充：

- 这里只是用示例值表达配置结构
- `common_env` 会先并到每个 worker 的环境变量里，然后再被 `worker.env` 覆盖；即 `common_env < worker.env`
- Worker 按独立的 key / model 并发配额单元建模，不应让多个 Worker 共享同一个 key

下面 5 份 markdown 内容对应代码工程里的 prompt 文件。

### `bootstrap.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你需要直接解决这个问题，目标是完成 Goal。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```
## 上下文
### Origin
{origin}
### Goal
{goal}
### Hints JSON 数组
{hints}
````

### `reason.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前只做 `reason`。
你要同时判断两件事：
1. 现有 facts 是否已经满足 goal。
2. 如果还未满足，当前是否需要提出一个新的 intent。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "..."}
```
已满足 goal 时返回：
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```
未满足 goal，但需要提出新 intent 时返回：
```json
{"accepted": true, "data": {"intent": {"from": ["f001"], "description": "..."}}}
```
未满足 goal，且当前不需要提出新 intent 时返回：
```json
{"accepted": true, "data": {}}
```
## 规则
- 如果下面的 `open_intents` 为空，说明当前图里没有任何进行中的探索；此时若不返回 `data.complete`，则必须返回 `intent`。
- `intent.from` 只能从下面的合法 fact id 中选择。
## 上下文
### 图快照
{graph_yaml}
### 当前合法的 fact id JSON 数组
{fact_ids}
### 当前所有未结论的 intent JSON 数组
{open_intents}
````

### `explore.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前只做 `explore`。
你只处理当前这一条 intent，执行探索并给出最终事实结论。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```
正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```
## 规则
- `description` 必须是客观探索结论，不要输出解释性废话。
## 上下文
### 图快照
{graph_yaml}
### 当前 intent id
{intent_id}
### 当前 intent 描述
{intent_description}
````

### `explore_conclude.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前正在对同一个 `explore` 做收尾总结。
- 不要继续探索。
- 只总结截至目前已经完成的探索和结论。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```
正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```
## 规则
- `description` 必须是客观探索结论，不要输出解释性废话。
## 上下文
### 图快照
{graph_yaml}
### 当前 intent id
{intent_id}
### 当前 intent 描述
{intent_description}
````

### `bootstrap_conclude.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
- 不要继续推进。
- 不要等待未完成的任务。
- 只总结截至目前已经确认、且对达到 goal 最有帮助的关键事实。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```
## 上下文
### Origin
{origin}
### Goal
{goal}
### Hints JSON 数组
{hints}
````
