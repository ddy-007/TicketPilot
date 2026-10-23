# TicketPilot 详细设计文档

版本：v1.0  
状态：实现基线  
对应规格：`TicketPilot-项目开发文档.md`

## 1. 文档定位

TicketPilot 是一个面向售后工单的可审批、可恢复履约重试 Agent。本文件把原项目规格收敛为一个适合简历展示的小型、可验证实现，补充实现时必须遵守的接口、状态、测试和验收约束。

原项目开发文档继续作为业务范围、安全边界和 M0～M5 顺序的基线。本文件不扩大业务范围；两份文档发生冲突时，优先遵守用户明确要求和原规格中的安全约束。

所有“已验证”结论必须来自实际命令输出、测试报告或提交记录。设计目标、待验证假设和实际结果必须分开记录。

## 2. 项目目标

唯一业务场景：用户已支付，但订单履约任务因 `QUEUE_PUBLISH_TIMEOUT` 失败；系统查询证据，生成重试提案，等待人工审批，获批后以幂等方式重新提交履约任务，并验证订单进入 `QUEUED`。

项目必须证明：

- 模型只能调用只读工具，不能直接写业务数据。
- 高影响操作必须经过人工审批。
- 审批中断可以通过 PostgreSQL checkpoint 跨进程恢复。
- 审批后订单版本变化时，旧提案不能执行。
- 重复请求和图节点重放不会产生重复业务效果。
- “重新入队”不会被描述为“已经发货”。

## 3. 范围

### 3.1 保留功能

- FastAPI 后端。
- PostgreSQL 数据库。
- 一个 LangGraph Agent。
- 三个只读工具：`get_ticket`、`get_order`、`get_fulfillment_events`。
- 一个高影响写操作：`retry_fulfillment`。
- 人工审批、`interrupt` 和恢复。
- 单 Worker 持久任务队列。
- 订单版本校验、业务幂等和执行后验证。
- 极简网页工作台。
- 6～12 个代表性测试场景。

### 3.2 明确不做

- 多 Agent、Redis、Celery、向量数据库。
- 退款、改地址、自动关单。
- 真实工单、支付或物流系统。
- 内部备注回写。
- 多 Worker 横向扩展。
- SSE、复杂 UI 和大规模评测平台。
- 生产级身份认证和公开部署。

## 4. 里程碑与门禁

每个里程碑完成后必须先执行验收并停止；没有明确“继续下一个里程碑”的指令，不进入下一阶段。

### M0：最小能力验证

交付：FastAPI 健康检查、PostgreSQL 连接、Alembic 迁移、LangGraph checkpoint 初始化、真实模型基础调用、真实模型工具调用、真实模型结构化输出、最小 `interrupt + Command(resume=...)` 恢复实验和 `uv.lock`。

验收：进程 A 使用固定 `thread_id` 进入中断并退出；进程 B 使用同一 `thread_id` 从 PostgreSQL 恢复并得到终态。

M0 未通过前，不实现业务 Agent、Worker 或前端。

### M1：最小模拟业务服务

交付业务表、固定演示数据、三个只读接口、重试接口、操作查询接口、规则校验、订单版本递增和业务幂等。

验收：合法订单从 `FAILED` 变为 `QUEUED`；重复请求不重复插入事件；不满足条件的请求被拒绝。

### M2：只读 Agent

交付严格工具 schema、绑定范围校验、调用预算、证据编号、结构化诊断和不可变重试提案。

验收：正常案例生成提案；不满足条件时转人工；模型不能调用写工具；跨订单参数在 HTTP 请求前被拦截；注入文本不能改变规则。

### M3：审批、执行和恢复

交付 Run/Job/Approval/Operation 持久化、单 Worker、审批 API、审批哈希、审批中断、拒绝和过期处理、执行前复核、幂等执行和执行后验证。

验收：正常批准、拒绝、版本冲突、重复批准和 Worker 重启五条闭环通过。

### M4：极简工作台和最终测试

交付工单列表、工单详情、诊断按钮、证据和调用轨迹、审批卡片、执行结果、刷新恢复和代表性测试。

验收：浏览器完成批准和拒绝流程；无审批权限时不能批准；刷新不会创建新 Run；页面只显示“已重新入队”，不显示“已发货”。

### M5：评测与求职材料

按原规格第 17、18、20 节及旧对话已确认的小规模实施范围，冻结 16 条合成案例（4 dev / 12 test），对比固定查询规则与现有只读 Agent，两者共用审批、幂等、持久恢复和写后校验。交付逐例结果、分母明确的指标、真实 usage/费用或未知原因、恢复测试、README、架构图、三分钟录像和仅使用实测数字的简历条目。

2026-10-23 早先 M5 验收：固定流程 16/16、后端完整回归 232、浏览器 22 项通过。真实模型最小请求 HTTP 402，真实 test 未运行，M5 不标记为全部验收完成。M5 专项契约见第 24 节，后续全项目修复及最新结果见第 25 节。

## 5. 总体架构

```text
Browser
  -> FastAPI API
       -> PostgreSQL app/sim data
       -> persistent jobs
       -> simulator read/write API
Worker
  -> PostgreSQL job queue
  -> LangGraph + PostgreSQL checkpoint
  -> LLM provider
  -> simulator API
```

逻辑上分为三类状态：

1. LangGraph checkpoint：记录图执行位置和中断状态。
2. 应用数据库：记录 Run、Job、Approval、Operation、证据和事件。
3. 模拟业务数据库：记录订单实际状态、履约事件和远端幂等结果。

三类状态不假设为一个原子事务，恢复逻辑必须显式协调它们。

## 6. 技术栈和环境

- Python 3.11 / 3.12（已验证宿主机 3.12.13、容器 3.12.12）。
- FastAPI、Uvicorn、Pydantic v2、pydantic-settings。
- SQLAlchemy 2、Alembic、psycopg 3、PostgreSQL。
- LangGraph、`langgraph-checkpoint-postgres`。
- OpenAI SDK 兼容接口、httpx。
- pytest、ruff。
- React、TypeScript、Vite。
- Docker Compose。
- uv 和 `uv.lock`。

具体依赖版本必须通过实际解析和 M0 实验确定，不能根据规格中的类别说明猜测版本。

Docker Compose 是标准验收环境。没有 Docker 时允许连接本机 PostgreSQL，但必须单独记录环境；SQLite 只能用于纯单元测试，不能用于 M0 PostgreSQL 恢复验收。

## 7. 配置和密钥

必需环境变量：

```dotenv
APP_ENV=local
APP_DATABASE_URL=postgresql+psycopg://...
CHECKPOINT_DATABASE_URL=postgresql://...
LLM_BASE_URL=...
LLM_MODEL=...
LLM_API_KEY=...
DEMO_OPERATOR_TOKEN=...
DEMO_APPROVER_TOKEN=...
```

规则：

- 数据库必需变量缺失时启动失败，只显示变量名，不显示值；模型三项配置仅在执行模型实验时必填，不阻断健康检查、迁移和 PostgreSQL 恢复实验。
- API Key、Token 和完整连接串只能存在于本地 `.env` 或进程环境。
- `.env.example` 只使用占位符。
- 日志和异常不得输出 API Key、密码、Authorization Header 或完整 DSN。
- 配置摘要只记录环境、主机、数据库名、模型名和敏感值是否存在。
- `CHECKPOINT_DATABASE_URL` 必须使用锁定版本 checkpoint 包支持的格式，不能未经验证复用 SQLAlchemy URL。

## 8. 数据库和初始化顺序

### 8.1 业务表

最小业务表：

- `tickets(id, tenant_id, subject, body, bound_order_id, status)`。
- `orders(id, tenant_id, payment_status, fulfillment_status, inventory_reserved, paid_at, version)`。
- `fulfillment_events(id, tenant_id, order_id, error_code, created_at, operation_key)`。
- `idempotency_records(tenant_id, idempotency_key, request_hash, operation_type, response_json)`。

### 8.2 Agent 表

最小 Agent 表：

- `runs`：一次处理过程和最终状态。
- `run_jobs`：持久任务及领取状态。
- `approvals`：不可变提案、哈希和决定。
- `operations`：应用侧操作状态和幂等键。
- `tool_calls`：工具调用轨迹和证据。
- `run_events`：面向工作台的事件流。

### 8.3 初始化顺序

1. PostgreSQL 健康检查通过。
2. `alembic upgrade head`。
3. 使用锁定版本官方接口初始化 checkpoint 表。
4. 执行数据库连接验证。
5. 启动 API、Worker 或实验进程。

禁止在应用导入阶段迁移数据库，禁止多个服务并发猜测创建 checkpoint 表，禁止用 `Base.metadata.create_all()` 替代 Alembic。

## 9. 健康检查 API

```http
GET /health/live
```

返回：

```json
{"status":"ok"}
```

该接口只检查进程存活，不访问数据库。

```http
GET /health/ready
```

就绪时返回：

```json
{"status":"ready","database":"ok","checkpoint":"ok"}
```

数据库或 checkpoint 不可用时返回 HTTP 503，并只返回安全错误分类。健康检查不调用模型。

## 10. 业务规则和安全边界

只有以下条件全部满足时，才允许生成和执行 `retry_fulfillment`：

1. 工单属于当前租户。
2. 订单是工单可信绑定的订单。
3. `payment_status == PAID`。
4. `fulfillment_status == FAILED`。
5. `inventory_reserved == true`。
6. 最新错误码为 `QUEUE_PUBLISH_TIMEOUT`。
7. 支付时间已超过 30 分钟。
8. 订单版本等于提案中的 `expected_version`。
9. 审批已批准、哈希一致且未过期。
10. 同一幂等键没有已确认的业务效果。

模型只能输出建议，不能决定租户、权限、审批、提案期限、版本校验或最终写入。写工具不注册给模型，也不使用 `eval`、`getattr` 或动态导入执行模型文本。

## 11. API 契约

### 11.1 外部 API

```text
GET  /api/tickets
GET  /api/tickets/{ticket_id}
POST /api/tickets/{ticket_id}/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/events?after_id=0&limit=100
GET  /api/approvals/{approval_id}
POST /api/approvals/{approval_id}/decision
```

创建 Run 使用 `Idempotency-Key`。相同键和相同请求返回原 Run；相同键但请求不同返回 409；同工单存在活跃 Run 时返回 409。

审批请求必须由服务端验证 approver Token。请求体中的 `role`、`approved` 或任意恢复命令不具有授权意义。决定与 `RESUME` Job 必须在同一数据库事务中提交。

### 11.2 模拟器内部 API

```text
GET  /internal/tickets/{id}
GET  /internal/orders/{id}
GET  /internal/orders/{id}/fulfillment-events
POST /internal/orders/{id}/retry-fulfillment
GET  /internal/operations/{idempotency_key}
```

内部 API 使用独立读写凭证。跨租户、错误工单绑定和越权资源统一返回 404；只读凭证调用写接口返回 403。

## 12. Agent 和审批流程

```text
load_context
  -> reason
  -> read_tools -> reason
  -> build_diagnosis
  -> validate_diagnosis_and_policy
  -> prepare_approval
  -> await_approval(interrupt)
  -> execute_action
  -> verify_result
  -> finalize
```

`GraphState` 只保存可序列化状态：Run 上下文、消息、预算、证据、诊断、审批 ID/哈希、操作键、结果和终态。不得保存数据库连接、HTTP 客户端、Token、函数对象或无限增长的原始日志。

模型调用最多 6 次，逻辑只读工具调用最多 10 次；达到预算必须进入明确终态，不能无限重试。

## 13. LangGraph 恢复实验

实验图固定为：

```text
START -> await_approval(interrupt) -> END
```

实验命令：

```text
python -m app.experiments.recovery --phase pause --thread-id <id>
python -m app.experiments.recovery --phase resume --thread-id <id>
```

`pause` 阶段使用固定 `thread_id` 进入 interrupt、确认 checkpoint 已保存并退出。`resume` 阶段不重新提交初始状态，读取同一 thread 的 checkpoint，使用 `Command(resume=...)` 恢复并确认没有未解决 interrupt。

中断节点前不得发送业务写请求。恢复时中断节点可能从头重放，因此审批创建必须按 `(run_id)` 幂等，业务写入必须使用稳定的业务幂等键。

## 14. 幂等和结果未知

履约重试幂等键固定为：

```text
fulfillment-retry:{tenant_id}:{order_id}:v{expected_version}
```

请求语义哈希只包含操作类型、租户、订单、预期版本和预期错误码，不包含 Run ID、Approval ID 或当前时间。

业务服务必须在一个事务中：

1. 检查幂等记录。
2. 锁定订单。
3. 再次检查幂等记录。
4. 校验租户、绑定关系、支付、履约、库存、时间、错误码和版本。
5. 更新 `FAILED -> QUEUED` 并递增版本。
6. 插入 `RETRY_ENQUEUED` 事件。
7. 保存幂等结果并提交。

写请求超时表示结果未知。恢复时先用同一幂等键查询远端结果；不能生成新键盲目重试。确认结果至少需要远端操作记录、对应事件和订单状态证据。

## 15. 单 Worker

MVP 只允许一个 Worker，通过 PostgreSQL session advisory lock 防止并行消费。Worker 启动时：

1. 获取 advisory lock，失败则退出。
2. 将遗留 `RUNNING` Job 重新入队。
3. 短事务领取一个 `QUEUED` Job 并标记 `RUNNING`。
4. 图执行期间不持有长事务。
5. 图结束或中断后，以短事务同步 Run 和 Job。

锁连接断开后停止领取新任务。多 Worker、网络分区和 fencing 不属于本项目能力，不在文档或简历中声称已解决。

## 16. 模型验证

真实模型调用是 M0 硬门槛。验证分为：

1. 基础文本调用成功。
2. 一个严格 schema 的只读工具调用成功。
3. 一个 Pydantic 结构化诊断成功。

Mock 模型仅用于离线单元测试、协议测试和错误分支测试，不计入 M0 真实模型验收。真实模型失败时必须记录为未完成，不能用 Mock 结果替代。

## 17. 测试和验收证据

必须覆盖：

- 空 PostgreSQL 数据库迁移。
- 重复迁移和 checkpoint 初始化。
- live/ready 健康检查差异。
- 缺少配置和敏感信息脱敏。
- LangGraph 跨进程恢复。
- 非法工具名和参数。
- 结构化输出额外字段。
- 非绑定订单拦截。
- 未支付、库存不足、错误失败码、版本冲突。
- 拒绝审批不产生写入。
- 重复批准只有一个决定。
- 同一幂等键只产生一个履约事件。
- 写请求结果未知时不更换幂等键。
- 提示词注入不产生越权操作。

M0 标准命令由 README 记录，至少包括：

```bash
docker compose up -d postgres
docker compose run --rm migrate
docker compose up -d api
curl http://127.0.0.1:8010/health/live
curl http://127.0.0.1:8010/health/ready
docker compose run --rm llm-smoke --kind all
docker compose run --rm recovery --phase pause --thread-id <id>
docker compose run --rm recovery --phase resume --thread-id <id>
```

验收报告必须包含命令、退出码、关键输出和环境说明。缺少数据库或 API Key 时必须标记未完成，不得虚构成功。

## 18. 交付结构

实现完成后，仓库只保留支撑范围所需的结构：

```text
backend/
  app/
    main.py
    config.py
    db.py
    models.py
    graph.py
    tools.py
    policy.py
    api.py
    worker.py
  migrations/
  tests/
frontend/
  src/
compose.yaml
.env.example
README.md
```

允许合并小模块，但不得为了目录完整提前创建空壳文件或未使用的抽象层。

## 19. 简历边界

项目完成后只使用实测数字，推荐表达：

> 基于 FastAPI、LangGraph 与 PostgreSQL 实现可审批、可恢复的售后履约重试 Agent；通过只读工具隔离、人工审批、订单版本校验和业务端幂等，阻止模型越权并避免恢复和重复请求造成重复业务效果。

不得声称生产级认证、高并发、多 Worker、绝对安全或未经测试的成功率。

## 20. 当前限制

- 单一业务场景。
- 单 Worker。
- 合成业务数据。
- 本地演示 Token。
- 真实模型依赖外部 API 配置。
- 当前环境若没有 Docker、PostgreSQL 或 API Key，只能完成代码和离线测试，不能完成 M0 最终验收。

M0 的 Compose 只实现 PostgreSQL、一次性 migrate、API 和两个命令行能力实验；未实现后续业务服务。2026-10-19 已通过数据库迁移、独立进程/容器恢复和真实模型三项验收。M1 已增加独立 simulator 服务及 0002 迁移；其默认只在容器网络内开放。当前供应商的结构化输出采用已实测的 JSON Object + Pydantic，不宣称原生 JSON Schema 约束。具体实测命令以 README 和 DEVLOG 为准。


M1 实现约定：模拟器内部写凭证可读取及重试，读凭证仅可读取；操作查询同样要求 X-Ticket-ID 并校验绑定。种子只补缺、不清库。时间测试通过构造函数注入固定 UTC 时钟，未提供 HTTP 改时或重置接口。当前通过真实 PostgreSQL 的 S01～S06、幂等重放、并发和事务回滚验证；审批与模型工具循环仍属于后续里程碑。

## 21. M2 实现落地说明

M2 已实现只读 LangGraph、三个严格 schema 工具、绑定范围拦截、预算、证据存储、诊断校验及不可执行的哈希提案草稿。命令入口为 `python -m app.agent.cli`（Compose 服务 diagnose），仅使用内部只读凭证。临时诊断状态 PROPOSAL_READY 不等于 WAITING_APPROVAL，也不接入写工具。

本阶段用 app.diagnostic_runs/diagnostic_evidence 保存只读结果；M3 再接入正式审批、持久任务和受控执行。模型建议不能覆盖规则，模型文本不能作为执行成功声明。12 条代表性确定性案例与真实模型抽测分开记录；实际命令和已知限制以 README/DEVLOG 为准。

## 22. M3 实现落地说明

新增正式 runs/jobs/approvals/operations/tool_calls/run_events。Run 创建与 Job、审批决定与恢复 Job 各自原子提交；审批计划由数据库触发器保护不可变。M2 诊断节点的可序列化运行状态接入同一持久化图，thread_id=run_id，等待节点忽略 resume 中的批准声明，执行必须读数据库正式决定。

单 Worker 会话锁，丢锁即停止启动新节点；恢复先查 checkpoint，再选择初始启动、普通 invoke(None)、审批 Command(resume) 或终态同步。执行先查询既有操作，再校验实时规则/版本及审批，保存 PREPARED 后才 POST；同业务键最多两次 POST、每 Job 最多三次自动执行。UNKNOWN 不被 404、过期或版本冲突误改为确定未执行。

M3 提供本机 API 和显式审批 CLI；API 没有业务写凭证，Worker 有写凭证但不注册给模型。正式身份仅为本地合成演示 Token；不包含生产认证或多 Worker fencing。额外 approval-demo 种子只追加 T3001/O3001，不清库、不重置。自动关单和备注仍未实现。M4 前端落地见第 23 节；具体命令、实测结果与限制见 README/DEVLOG。

## 23. M4 实现落地说明

采用 React、TypeScript、Vite 构建极简工作台，产物由既有 FastAPI 同源提供；Docker 使用锁定 Node 镜像的构建阶段，运行阶段仍为既有 Python 服务，没有新增部署组件。前端依赖通过 package-lock.json 锁定。

新增只读 `GET /api/me`；身份完全来自角色 Token 的服务端校验。工单列表附带当前租户活跃 Run ID。原审批接口、持久任务、业务前检和 Worker 写入边界继续复用 M3，不新增数据库迁移或新的模型工具。

Token 通过密码框临时输入，只留在页面内存。Run ID 放入 URL 以便刷新恢复；创建请求的不敏感幂等键/工单 ID 在发送前暂存到 URL，响应丢失后的刷新重试仍使用原键。审批卡片显示完整服务端计划、证据、有效期、影响与哈希；响应丢失后只重试同一决定。最终成功须由服务端确认重新入队，前端不把 202 批准响应显示为执行成功。

轮询按事件 ID 增量拉取，满页继续取、去重并补拉终态事件后停止；失败提示手动刷新。合成工单文本通过 React 文本节点展示，不执行其 HTML。页面区分拒绝、过期、提案变化、失败和未知结果；自动备注继续暂缓。

验收使用独立 PostgreSQL、真实模拟器 HTTP/API/Worker、固定模型替身，以及真实 Edge/Chrome 浏览器。固定模型结果不记为真实模型评测；M5 进展见第 24 节。

## 24. M5 评测实现与验收状态

全面审查后的评分版本为 m5-v3。任务通过还需匹配预期终止原因和服务端接受的有效诊断，伪造引用或非法工具导致的转人工不算业务成功。每例对隔离库全部订单、工单、履约事件和幂等记录做前后核验，只允许正式获批的本例精确变更；Worker 读写出站请求均检查资源范围。模型工具有效性复用 ReadTools.validate，绑定来自可信服务端上下文。请求前保存调用意图，返回后保存 usage，临时文件 fsync 后原子替换报告；中断和强制退出保留部分计量，未知字段不填零。旧数据缺少新增安全证据时必须标为需重跑，不能离线补造。修复后完整回归 244 passed，固定流程 16/16；详见 docs/M5-修复验证.md。

`backend/evaluation/cases.json` 为冻结标签集，保留原 10 月 20 日 SHA-256。`evaluation.run` 创建随机独立 PostgreSQL 库与模拟器进程，固定夹具时钟；固定流程按规则查询并复用现有 Agent 校验和 M3 执行控制，真实 Agent 使用同一环境。故障只在查询边界注入，不关闭业务安全检查。

`metrics.py` 对状态、业务结论、数据库状态、效果次数、证据和范围一起评分。诊断主指标使用全部明确标签，另报诊断覆盖率和有结果的条件准确率；没有模型结果不能悄悄缩小自动完成率分母。成本在 usage/计价口径不明确时必须为 null。`evaluation.rescore` 在案例 hash 一致时对历史结果另存重算，不改原测量、不访问模型、不覆盖旧报告。

真实调用需显式 `--real-model`，单例与整批均有预算，供应商 API/连接/超时错误中止整批并保存部分结果。HTTP 状态码仅以整数形式记录，不保存供应商私密响应正文或认证 Header。程序完成执行与评分通过是两个独立概念。

固定规则基线已完成 dev 4/4、test 12/12；旧真实 dev 4 次 API 失败和本次 402 探测全部保留。未执行真实 test，费用未知，未修改提示词以迎合冻结 test。确定性恢复回归、浏览器验收和全新 Compose 卷的本机启动实验已通过。录像使用固定模型替身并持续标注，不替代真实模型门禁。

完整证据、分母定义、已知限制和待关闭项见 `docs/M5-评测报告.md`；复现命令和录像脚本见 `docs/M5-演示与复现.md`。当前停在 M5，尚未通过全部最终验收。

M4 全面审查后，HTTP 适配层验证 JSON 和 UI 所消费的响应字段，并核对 Run ID 与请求路径一致；未作决定时审批理由允许 null，依据现有数据库/API 契约处理。请求连接失败、408、5xx 或成功响应损坏均保留提交未确认语义，审批仅重试原决定。此客户端检查不替代服务端授权、计划哈希和业务规则。

导航使用本地版本标识隔离旧响应；同一 Run ID 或空 Run ID 下的浏览器前进/后退仍触发重新读取。读取错误与提交错误独立保存，只有正式状态确认后才清除提交未确认提示；工单读取失败不阻断轨迹读取。源码、浏览器测试及前端配置共同接受 TypeScript 检查。具体复现与修复记录见 DEVLOG 的 M4 全面审查章节。

## 25. 全项目审查修复（2026-10-23 后续）

其他 Run 共享的 Operation 不覆盖本轮正式拒绝决定；拒绝保持 REJECTED/DECLINED、executed=false，历史记录通过 historical_operation 和 warnings 单独展示并关联 origin_run_id。同一 Run 的 PREPARED/UNKNOWN 恢复仍先查证；旧操作在有效期内提交、恢复时审批过期的成功结果继续如实确认。

工单列表使用 after_id 游标，limit 默认/最大 25，返回摘要和 next_after_id，不返回 body；详情接口返回完整正文和当前租户 active_run_id。前端通过“加载更多工单”追加摘要，选中后读取详情；待重试 URL 直接按工单 ID 取详情，不依赖该工单位于第一页。内部客户端保留 64 KiB 响应上限。

M0 探测请求前登记调用记录，每次响应后立即保留 usage，后续网络或格式错误不丢弃此前已知用量；未知请求继续标记 usage_unknown。ReadTools 为每次 HTTP 尝试记录 started_at 和 duration_ms，单次耗时不包含后续退避等待，逻辑调用总耗时另记。

最新完整后端回归 250 passed、0 skipped，浏览器 25 passed；原审查四个契约探针 4/4 通过，修复后固定基线重跑 16/16，源码 SHA 与测量报告对应。真实模型、费用和异机验收门禁仍未关闭；证据与兼容说明见 docs/全项目审查-修复验证.md。
