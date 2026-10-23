# TicketPilot

单场景、可审批、可恢复的售后履约重试 Agent。**M0～M4 已完成；M5 已完成确定性评测与交付材料，真实模型最终验收被 HTTP 402 阻塞**。只有供应商恢复、真实 dev/test 结果可复核后才把 M5 标为完成。

## 当前状态（2026-10-23）

- Docker Linux Engine 和 PostgreSQL 16.15 已启动。
- Alembic 已迁移到 `0004_m3_execution`，checkpoint 独立初始化及重复初始化通过。
- M1 提供三个只读接口、内部工单列表、履约重试与操作结果查询，S01～S06 已测试。
- 同一 thread_id 的两个独立进程已完成暂停与恢复，恢复后没有未解决中断。
- 真实模型的文本、只读工具调用闭环及结构化输出已在独立 Docker 容器中通过。
- 锁定版本：LangGraph 0.6.11、langgraph-checkpoint-postgres 3.0.5、OpenAI SDK 1.109.1；其余见 backend/uv.lock。宿主机实际测试 Python 3.12.13。
- 工作台由 API 同源提供，打开 `http://127.0.0.1:8010/`；前端依赖见 `frontend/package-lock.json`。
- 最新后端完整回归 250 passed、0 skipped；前端构建和 3 项单测通过，浏览器完整回归 25 passed。全项目审查新增的 4 项问题已修复，原复现探针 4/4 通过；同一 Run 的过期历史结果查证继续通过。
- M5 冻结 16 条案例（4 dev / 12 test），固定流程 16/16 符合预期。真实 Agent 旧 dev 4 条均遇到供应商 API 错误；本次最小探测 HTTP 402，真实 test 尚未运行，费用未知。固定模型替身不计为真实模型评测。
- 交付入口：[全项目审查修复验证](docs/全项目审查-修复验证.md)、[M5 修复验证](docs/M5-修复验证.md)、[M5 评测报告](docs/M5-评测报告.md)、[演示与复现](docs/M5-演示与复现.md)、[架构图](docs/architecture.svg)、三分钟录像（本地：`artifacts/m5-demo-delivery-2026-10-23.mp4`）、[简历条目](docs/简历条目.md)、[M5 审查记录](docs/M5-审查记录.md)。

![TicketPilot 架构](docs/architecture.svg)

## M5 评测与复算

在项目根目录运行；下列数据库连接是本机 Docker 的公开合成测试账户。评测自动创建和清理自己的随机库，不重置日常演示订单。输出文件不得已存在。

```powershell
$env:M0_TEST_ADMIN_URL = 'postgresql://ticketpilot:local-dev-only@127.0.0.1:5432/postgres'
uv run --directory backend --locked python -m evaluation.run --baseline fixed --split all --output ../artifacts/m5-fixed-rerun.json
# 只重算已有测量，无需访问模型或数据库。
uv run --directory backend --locked python -m evaluation.rescore --input ../artifacts/m5-fixed-v3-2026-10-23.json --output ../artifacts/m5-fixed-recomputed.json
```

真实模型需要显式 `--real-model` 和总调用预算，先 dev、再冻结 test；当前供应商 HTTP 402，已停止后续调用。指标分子、分母、用量未知值、全部失败尝试及恢复说明见评测报告；不能将固定流程 12/12 写为真实 Agent 成功率。首次 Git 提交之前的历史评测使用 SHA-256 与测量时源码快照追踪版本；后续变更使用 Git 记录。

当前使用 m5-v3：异常转人工不能计为成功，全库副作用与全部 Worker 读写路径均受核验，调用意图和 usage 逐次原子落盘。旧报告缺少全库审计时标为需重跑，不能通过重评分补造安全结论。四项审查缺陷已修复；本轮没有重试真实模型，HTTP 402 是既有探测记录。

全项目后续修复还覆盖：另一 Run 的共享操作不覆盖本轮拒绝终态；工单列表每页最多 25 条摘要，正文通过详情接口读取；M0 工具探测失败保留先前已知用量；只读 HTTP 每次尝试记录独立开始时间和耗时。最新固定基线已按修复后源码重跑 16/16，原始数据为 `artifacts/m5-fixed-project-fixes-2026-10-23.json`，旧报告继续保留。

## 本地配置

项目根目录已创建未提交的 `.env`。新机器先复制 `.env.example` 为 `.env`，不要覆盖已填写的配置。模拟器需要内部读写 Token；API 只持有读凭证，Worker 持有读写凭证，浏览器不接收内部凭证。浏览器临时输入两个演示角色 Token 之一。

你只需填写：

```dotenv
LLM_BASE_URL=供应商提供的完整兼容接口根地址
LLM_MODEL=供应商提供的模型名
LLM_API_KEY=本地密钥
```

配置统一读取项目根目录 `.env`，进程环境变量优先。模型变量在真实模型实验、诊断 CLI 和 Worker 中使用；健康检查、迁移和 PostgreSQL 恢复实验不依赖模型密钥。缺少数据库 URL 会阻止后端启动。API 没有模型密钥和业务写凭证，迁移容器只注入数据库配置。

`.env` 已列入 `.gitignore`，不会进入 Docker 构建上下文。项目已初始化 Git；本地凭证、运行日志、截图/录像和压缩包保留在本地，不进入提交。仓库随附[验证摘要与隐私说明](docs/验证摘要.md)。

## Docker 启动（在项目根目录运行）

```powershell
docker compose up -d --wait postgres
docker compose build migrate
docker compose run --rm migrate
docker compose run --rm simulator python -m simulator.seed --scenario demo
docker compose run --rm simulator python -m simulator.seed --scenario approval-demo
docker compose up -d --wait api simulator
docker compose up -d worker
Invoke-RestMethod http://127.0.0.1:8010/health/live
Invoke-RestMethod http://127.0.0.1:8010/health/ready
```

API 和工作台的宿主机端口是 **8010**，容器内部端口是 8000。PostgreSQL 仅发布到本机 127.0.0.1:5432，供宿主机开发和测试使用。基础镜像均固定 digest；后端通过 `uv sync --frozen --no-dev`、前端通过 `npm ci` 使用锁文件安装依赖。两个种子命令只补充缺失数据，不重置已执行的订单。

`migrate` 顺序执行 Alembic 迁移和 PostgresSaver.setup()；API 启动不执行迁移。checkpoint 固定使用 `checkpoint` schema，应用最小表在 `app` schema，Alembic 版本表在 `public` schema。

验证跨进程恢复，必须使用一个从未用过的 thread_id：

```powershell
$thread = "m0-" + [guid]::NewGuid().ToString('N')
docker compose run --rm recovery --phase pause --thread-id $thread
# 等待第一个命令完全退出，再启动第二个容器。
docker compose run --rm recovery --phase resume --thread-id $thread
```

预期先得到 `status=interrupted, interrupt_count=1`，后得到 `status=resumed, interrupt_count=0, pending_nodes=[]`。这是无业务写入的能力实验，resume 信号不能作为未来正式业务审批的授权。

模型配置完成后执行（会调用供应商 API）：

```powershell
docker compose run --rm llm-smoke --kind all
```

应分别返回 text/tool/structured 的 `status=passed`。工具实验会验证工具名和严格参数、执行固定只读函数、回传工具结果并取得最终响应。一般接口使用 JSON Schema + Pydantic；已验证的 DeepSeek 官方接口使用非思考模式和 JSON Object + Pydantic，后者不等于服务端原生 JSON Schema 约束。所有输出仍须经过本地严格校验，不用替身替代真实验收。脚本不自动重试；每项实际调用记录耗时和提供商 usage。

2026-10-19 最终真实验收的三项均通过，退出码 0；本地 `artifacts/m0-llm-2026-10-19.json` 保存不含密钥、接口地址或响应正文的验收摘要，记录提供商返回的模型标识及 token 用量。该目录由 `.gitignore` 忽略；排障产生的额外请求不计入此最终成功运行的用量摘要。

停止本项目容器而保留数据：

```powershell
docker compose down
```

## 宿主机开发（数据库仍使用 Docker）

```powershell
cd backend
uv sync --locked
uv run --locked python -m app.migrate
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8010
```

运行前先停止容器 API 或选用另一个端口，避免占用 8010。模型和恢复命令：

```powershell
uv run --locked python -m app.experiments.llm_smoke --kind all
$thread = "m0-" + [guid]::NewGuid().ToString('N')
uv run --locked python -m app.experiments.recovery --phase pause --thread-id $thread
uv run --locked python -m app.experiments.recovery --phase resume --thread-id $thread
```

## M1 模拟器启动与验收

本次已在本地 `.env` 追加随机、不同的 `SIMULATOR_READ_TOKEN` 和 `SIMULATOR_WRITE_TOKEN`，没有修改已有模型配置。新机器可用 Python 的 `secrets.token_urlsafe(36)` 分别生成两份并写入本地文件；启动时会拒绝文档占位符、空白/控制字符以及长度不在 32～256 范围的 Token。宿主机运行模拟器还需填写 `SIMULATOR_DATABASE_URL`，Compose 会显式注入容器数据库地址。

在根目录运行：

```powershell
docker compose build migrate
docker compose run --rm migrate
docker compose run --rm simulator python -m simulator.seed --scenario demo
docker compose up -d --wait api simulator
docker compose run --rm simulator python -m simulator.smoke
```

最后一条命令不调用模型，发送内部 HTTP 请求：读取工单、订单和事件，验证只读凭证 POST 返回 403，用两条并发写请求完成同一重试，再读取操作记录和事件确认效果。预期摘要：

```json
{"milestone":"M1","status":"passed","state":"QUEUED","version":4,"retry_events":1,"concurrent_requests":2,"read_token_write_status":403,"ticket_status":"OPEN","llm_calls":0}
```

重复 seed 仅补充缺失合成数据，不删除记录、不重置已执行订单；失败事件只与本次新建订单一起创建，不会给已有订单补造缺失证据。重复 smoke 通过原幂等记录返回结果，并重新读取当前订单；若历史重试成功但订单当前再次失败，验收必须失败。验收后演示订单 O2001 保持 QUEUED/v4。如需从 FAILED/v3 重做完整首次写入，用隔离测试库运行测试，不要清空演示库。

验收使用显式条件检查，在 `python -O -m simulator.smoke` 下同样有效。M1 审查后完整测试为 48 项通过（包含 M0 回归和真实 PostgreSQL 测试），发现与修复记录见 DEVLOG。

模拟器默认**不发布宿主机端口**，仅在 Compose 网络的 `http://simulator:8001` 访问；API 仍为本机 8010。业务数据在 `sim` schema；Agent 后续只能经内部 API 访问业务数据。读凭证可调用 GET；写凭证可读写；跨租户或不符合工单绑定统一返回 404。

内部协议：

| 方法与路径 | 必需上下文 |
|---|---|
| GET `/internal/tickets`、`/internal/tickets/{id}` | Bearer、X-Tenant-ID |
| GET `/internal/orders/{id}`、`/internal/orders/{id}/fulfillment-events` | 再加 X-Ticket-ID；事件 limit 1～20 |
| POST `/internal/orders/{id}/retry-fulfillment` | 写 Bearer、X-Tenant-ID、X-Ticket-ID、Idempotency-Key |
| GET `/internal/operations/{key}` | Bearer、X-Tenant-ID、X-Ticket-ID |

POST JSON 仅接受 `expected_version`（严格正整数）和 `expected_error_code`。键固定为 `fulfillment-retry:{tenant}:{order}:v{expected_version}`。相同键相同语义返回原结果，不同语义返回 409；新键不能绕过订单当前状态与版本检查。版本、支付、库存、支付满 30 分钟及最新失败码在订单锁内重查，订单更新、事件和幂等结果同事务提交。

M1 集成测试通过 `M0_TEST_ADMIN_URL` 创建随机数据库，再执行当前全部迁移。测试清理仅发生在该次创建的库；覆盖 S01～S06、精确时间边界、错误码、类型/额外字段、403/404、跨租户复合外键、重放、强制并发交错，以及事件/幂等记录插入失败的回滚。业务时钟在测试构造函数中注入，无法通过普通 HTTP 参数调整。

## M2 只读诊断 Agent

M2 使用 LangGraph 的 load → reason → read → diagnose → validate 流程。可调用工具严格只有 `get_ticket`、`get_order`、`get_fulfillment_events`，模型自动选择查询；写凭证没有注入诊断容器，也没有注册写工具。初始工单读取由程序执行以取得可信绑定，后续模型参数必须严格匹配本轮工单/订单。

根目录下运行：

```powershell
docker compose build migrate
docker compose run --rm migrate
docker compose up -d --wait api simulator
docker compose run --rm diagnose --tenant-id demo --ticket-id T1002
```

以上诊断命令会调用真实模型，T1002 的预期为库存未预留、转人工。O2001 已在 M1 验收中进入 QUEUED，诊断 T1001 应报告无需重复提交；不要为了重做演示清空该订单。新建隔离数据库的 M2 集成测试保留了正常 FAILED/v3 提案路径。

正常输出包括 run_id、status、reason_code、executed=false、模型/读取/HTTP 尝试计数和 token 摘要。查看存储结果不会再次调用模型：

```powershell
docker compose run --rm diagnose --tenant-id demo --show-run <run_id> --details
```

`--details` 显示已校验、脱敏的证据、诊断、模型调用摘要和提案草稿。`--tenant-id` 是可信本地开发入口参数，不是浏览器认证；不存在或不同租户的 run 不返回结果。模型自由文本不直接用作最终完成声明，展示摘要由服务端根据核验结果生成；不存储模型内部思维链。

M2 结果：

- `PROPOSAL_READY`：模型建议和服务端规则均通过，生成不可执行提案草稿；它不表示正在等待正式审批或已经执行。
- `SUCCEEDED`：只读确认已在队列/已发货，无需重试。
- `ESCALATED`：业务需要人工处理、越界工具、证据引用错误或模型与事实冲突。
- `FAILED`：模型/上游技术故障或调用预算无法支持可靠完成。

提案参数由程序从当前真实证据构造，包含固定工具名、订单、预期版本、证据、30 分钟有效期和服务端 SHA-256；`executable=false`。诊断引用必须属于本轮，不能只凭模型建议生成提案；模型保守选择人工处理也不会被规则静默升级为操作。

本阶段新增 `app.diagnostic_runs` 和 `app.diagnostic_evidence`，保存每轮结果、受限工具证据、观测时间和每次 HTTP 尝试；模型摘要保存阶段、模型名、提示词版本、耗时、usage。临时图状态中不保存连接或 Token。M2 没有审批中断/任务队列，未承诺诊断过程崩溃恢复；正式 Run/Approval/Job 协调使用下述 M3 入口；独立 diagnose 命令仍只执行 M2。

预算：最多 6 次模型请求，reason 最多 4 次，为诊断和一次 JSON 修复预留额度；最多 10 次逻辑读取（含可信初始读取），每次 HTTP 初次失败最多再试 2 次，仅重试网络错误/429/暂时性 5xx。单次连接 2 秒、读取 5 秒，模型默认 30 秒，活跃时间目标 120 秒；超出预算停止。已知 token 用量上限为 12000，每次请求前检查、输出最多 800 tokens；这不是供应商计费或未知用量的绝对硬限。

读取额度刚好用满但已有充分证据时，停止读取并进入诊断；额度不足且证据不完整才转人工。响应被截断或协议不合法时不当作成功；对已完整解析的响应，已返回的 usage 仍计入用量。畸形 SDK 响应在访问嵌套工具字段前重新验证，错误与序列化警告不回显原始响应内容。

### M2 验证边界

```powershell
cd backend
$env:M0_TEST_ADMIN_URL="postgresql://ticketpilot:local-dev-only@127.0.0.1:5432/ticketpilot"
uv run --locked pytest tests/test_m2_agent.py tests/test_m2_protocol.py tests/integration/test_m2_agent.py -q
```

12 条代表性案例使用真实隔离 PostgreSQL、模拟器 HTTP 和固定模型替身，验证正常提案、库存不足、已发货、已入队、未支付、缺绑定、暂时/持续读取失败、非白名单失败码、注入跨订单、支付时间不足、模型保守建议。断言业务订单与事件不变、工具仅 GET、跨订单请求不会发出、证据可重读且跨租户不可查询。这些确定性结果不等于真实模型成功率。

另外实际调用已配置模型：正常提案（新建隔离库）、库存不足、已发货、已入队、未支付、缺绑定路径已验证；开发中出现过 DIAGNOSIS_CONFLICT/POLICY_BLOCKED，服务端均阻断提案，记录保留在 DEVLOG，不作为成功样本。真实调用增加供应商费用，未计算价格或声称总体成功率。本地 artifacts/m2-real-*.json 只保存脱敏摘要，由 Git 忽略。

## M3 审批、持久任务与恢复

M3 API 绑定本机 8010；模拟器不发布宿主机端口。API 仅注入模拟器读凭证和两个演示身份 Token；Worker 注入内部读写凭证和模型配置，模型消息中不包含这些凭证。当前本地 `.env` 已补齐缺失的 `DEMO_OPERATOR_TOKEN` / `DEMO_APPROVER_TOKEN` 随机值，未修改已有密钥。新机器需分别生成不同的 32～256 字符可打印 ASCII Token（不含空白），不使用文档占位符；角色 Token 配置不合规时 API 返回 `503 DEMO_AUTH_NOT_CONFIGURED`，请求凭证不匹配时返回 `401 UNAUTHORIZED`，响应不会回显 Token。

### 启动

根目录执行：

```powershell
docker compose build migrate
docker compose run --rm migrate
docker compose run --rm simulator python -m simulator.seed --scenario approval-demo
docker compose up -d --wait api simulator
docker compose up -d worker
```

`approval-demo` 只补充 T3001/O3001 合成工单与 FAILED/v3 订单，不重置原 M1 的 T1001/O2001，也不重置已执行的 T3001。当前实际验收已批准 T3001，其订单为 QUEUED/v4，重复诊断应无需动作。首次完整重试路径可在独立测试数据库复现，禁止通过清空演示库重做。

### 人工控制命令

以下本地客户端从根目录 `.env` 读取身份 Token，通过 HTTP 调用 API，不打印 Token；无需把凭证复制到聊天、命令参数或日志。进入 backend 目录：

```powershell
uv run --locked python -m app.execution.cli start --ticket-id T3001
uv run --locked python -m app.execution.cli get --run-id <返回的run_id>
uv run --locked python -m app.execution.cli events --run-id <run_id>
```

创建返回 202 / QUEUED，模型在 Worker 中运行。只有状态进入 WAITING_APPROVAL 才允许决定。查看 `approval.plan` 的订单、版本、影响、证据和有效期后，显式输入审批 ID 和展示的哈希：

```powershell
uv run --locked python -m app.execution.cli approve --approval-id <approval_id> --plan-hash <plan_hash>
# 或拒绝同一份提案：
uv run --locked python -m app.execution.cli reject --approval-id <approval_id> --plan-hash <plan_hash>
uv run --locked python -m app.execution.cli get --run-id <run_id>
```

客户端的 approve/reject 使用 approver 身份，start 使用 operator 身份；服务端独立验证角色，operator 直接调审批接口返回 403。批准只表示决定已持久化和恢复任务已入队，不表示业务已经执行。最终 SUCCEEDED + REQUEUED 才表示操作结果、关联事件和当前订单状态均已核对；“重新入队”不表示已经发货。此演示认证不等同于真实组织的双人职责分离。

### 持久化与恢复约定

- `app.runs/run_jobs`：创建 Run 与 START Job 原子提交；同键同请求返回原 Run，键冲突或同工单已有活跃 Run 返回 409。部分唯一索引保证一个活跃 Run 和一个活跃 Job。
- `app.approvals`：每 Run 最多一个不可变提案；数据库触发器拒绝修改计划、哈希、有效期和所属 Run。审批接口统一 Run → Approval 加锁，决定与 RESUME Job 原子提交；同决定重复请求不新增任务，反向决定或错误哈希返回 409。
- `app.operations`：稳定业务幂等键和请求语义哈希，状态区分 PREPARED / UNKNOWN / CONFIRMED / FAILED。POST 意图先提交数据库；超时先查原键，查不到不宣称未执行。恢复时满足审批及实时规则才允许使用同键同参数有限重发，单键最多发起 2 次 POST。
- `app.tool_calls/run_events`：保存每次尝试与受限证据、模型请求意图/完成/usage、审批和终态事件。进程在模型请求中断时，只有开始事件也明确保留用量未知；持久模型意图计数防止丢失检查点后重置预算。
- 单 Worker 持有 PostgreSQL 会话 advisory lock 9081701；获取失败退出。锁连接丢失后不再领取任务或启动新节点/外部请求，不对在途请求作已取消承诺。
- Worker 启动把遗留 RUNNING Job 重新入队。已有 pending interrupt 只同步等待状态；正式决定已提交才 Command(resume)；普通未完成节点 invoke(None)；已有终态只同步结果，不重跑业务图。实际恢复实验针对锁定版本验证。
- 每个 Job 最多 3 次自动图执行尝试；超过时停止，可能已有业务效果则保留人工核对提示。恢复已有终态或等待状态的同步不创建新写入。
- 到期扫描只处理 WAITING_APPROVAL 的 PENDING 审批，按相同锁顺序标记 EXPIRED 并投递收尾任务；执行前再次查过期时间。过期审批只查证已发起的历史请求，不启动新 POST。收尾后释放活跃工单约束。

M3 专项审查后，审批有效期在取得 Run/Approval 行锁后使用当前时间判定；登记 POST 尝试后、HTTP 发送前也再次读取正式审批，数据库等待不延长授权。若尚无历史写请求但前检读取服务不可用，报告技术失败 `BUSINESS_UNAVAILABLE`，不会误称订单已变化。已有 PREPARED/UNKNOWN 时仍保持“可能执行”的保守语义。

Worker 的客户端与流程初始化也纳入有限重试；已保存等待/终态 checkpoint 的应用同步故障优先重同步，不能在最后一次尝试误把已确认成功改成失败。用户填写的审批理由在存储前替换当前配置中已知的凭证/连接串，不回显这些值；这不是任意未知个人信息的自动识别承诺。健康检查同时核对当前阶段关键业务表存在性。

恢复命令：

```powershell
docker compose restart worker
```

测试用的崩溃点、固定时钟只在 tests 中注入，没有生产 HTTP 故障开关、清库或改时接口。不同服务状态不在同一事务中，不能把 checkpoint 等同于外部恰好一次执行；最终去重依赖 M1 业务事务及唯一约束。

### M3 验收

```powershell
cd backend
$env:M0_TEST_ADMIN_URL="postgresql://ticketpilot:local-dev-only@127.0.0.1:5432/ticketpilot"
uv run --locked pytest tests/integration/test_m3_execution.py -q
```

测试使用独立随机 PostgreSQL 数据库和独立模拟器进程；模型用固定协议替身，不记为真实模型成功率。实际终止/重启 Worker 子进程，覆盖正常批准、拒绝、版本变化、重复/并发审批、两类原子提交回滚、审批过期、会话锁丢失、恢复上限、未知请求及延迟提交、终态同步。另有真实 Docker 模型 → WAITING_APPROVAL → 正式批准 → CONFIRMED/REQUEUED 的实测，详见 DEVLOG。

## M4 极简工作台

根目录执行（Docker 会按 `frontend/package-lock.json` 构建前端，宿主机无需安装 Node）：

```powershell
docker compose build migrate
docker compose up -d --wait api simulator
docker compose up -d worker
```

打开 `http://127.0.0.1:8010/`。从本地配置中取 operator 或 approver Token，在密码输入框临时输入；不要把真实值贴进聊天或写进页面代码。`GET /api/me` 返回服务端确认的角色。Token 只保存在页面内存，不写入 URL、localStorage、sessionStorage、日志或 `VITE_*` 构建变量；刷新和切换身份后需要重新输入。任务网址只包含 Run ID，凭此恢复原任务，不自动创建新任务。

操作路径：选择工单 → 客服操作员“开始诊断” → 查看证据、调用轨迹、完整计划及哈希 → 切换审批员 → 批准或拒绝 → 等待正式终态。操作员的审批按钮不可用，服务端也独立返回 403。提交审批只显示待处理提示；仅 `SUCCEEDED / REQUEUED / CONFIRMED` 显示“已重新入队”。拒绝、过期、提案过时、技术失败和未知结果使用不同文案，不将批准等同于已执行或已发货。

一次诊断的客户端幂等键在请求前写入 URL 的非敏感 `ticket/request` 参数；网络响应丢失后，即使刷新并重新登录，手动重试仍使用原键。收到 Run ID 后替换为 `?run=...`。同工单已有活跃任务时打开原任务；列表显示活跃任务提示。工单列表通过 `after_id` 游标分页，每页默认/最多 25 条摘要，点击“加载更多工单”继续；选中后单独获取完整正文，URL 中待重试工单不受第一页范围限制。审批响应丢失时只允许重试原决定，或刷新读取正式状态。事件每页 100 条，以 `after_id` 续取满页，按 ID 去重；进入终态后补拉并停止定时请求。连接出错时保留已有内容并提示手动刷新，不在错误后反复自动提交。

M4 审查后，前端同时校验 JSON 和所消费的响应字段，拒绝缺字段、错误类型及返回其他 Run 的响应；提交返回损坏的 2xx、408、5xx 或连接失败时，保留原请求/审批决定以便核对。离开页面后忽略迟到响应，不跳回旧任务或显示旧提示；这不会取消服务端已受理的任务。浏览器后退/前进也能恢复同一待重试请求。读取轮询不会清除尚未确认的提交错误；工单读取失败时仍单独尝试读取轨迹，刷新失败保留已加载内容并提示可能不是最新状态。

当前既有 T3001 已执行；再次诊断可能得到“无需操作”。批准/拒绝等可重复验收使用下面的隔离夹具，不重置演示订单。前端不提供改数据库、清库、跳过审批或任意 resume 的入口；自动备注仍未实现。

### 本地前端开发

宿主机 Node 22.12+ 或 24；本次实际为 22.23.2。在 frontend 目录：

```powershell
npm ci --ignore-scripts
npm run dev
```

Vite 绑定 `127.0.0.1:5173`，仅将 `/api` 代理到本地 `8010`；Token 无需填入任何前端环境变量。正式 Docker 由 FastAPI 同源提供构建文件，没有新增常驻前端服务。镜像构建上下文为仓库根目录（Compose 已配置），根 `.dockerignore` 排除凭证、虚拟环境、node_modules、测试夹具和本地报告。

### M4 浏览器回归

在 frontend 目录执行；需本地 PostgreSQL 可连接及测试账号具备 CREATE DATABASE 权限：

```powershell
npm ci --ignore-scripts
npm run build
npm test
$env:M0_TEST_ADMIN_URL="postgresql://ticketpilot:local-dev-only@127.0.0.1:5432/ticketpilot"
npm run test:e2e
```

测试默认使用已安装的 Microsoft Edge（`channel: msedge`）。其他环境可在 `playwright.config.ts` 改用已安装浏览器或标准 Chromium，并按该版本 Playwright 安装相应浏览器。端口 8021 必须空闲；不能复用指向演示库的服务器。

`backend/tests/ui_server.py` 创建随机独立数据库，启动真实模拟器 HTTP、API 和单 Worker，只有模型使用固定协议替身；测试中审批和业务写入均针对合成数据。测试端控制接口只存在于此夹具，Docker 不包含 `tests`；结束时显式关闭服务并删除自己创建的测试库。Playwright trace/video 关闭，截图仅包含合成数据并放入被忽略的 artifacts/test-results，不记录真实凭证。

浏览器测试覆盖批准、拒绝、operator 直接越权 403、重复点击、刷新恢复、版本冲突、过期、HTML/提示词注入、创建/审批响应丢失和现有活跃任务恢复。未知结果/工具失败展示单独使用浏览器响应替身检验 UI，不把它计为新的数据库故障实验或真实模型成绩。更深层的未知写入、崩溃恢复由原 M3 PostgreSQL 测试覆盖。

新增审查回归还覆盖异常响应、导航期间的迟到写响应、审批提交后返回 503、详情刷新、工单/轨迹读取隔离、提交错误保留、浏览器后退、鉴权重定向不转发凭证、跨 Run 响应拒绝、203 条事件分页及 390px 窄屏。当前模拟器未记录工单创建时间，页面没有虚构该时间；不影响上述核心闭环验收。

## 后端测试和验收

在 backend 目录：

```powershell
uv run --locked pytest -q
uv run --locked ruff check app migrations tests simulator
uv run --locked ruff format --check app migrations tests simulator
uv lock --check
```

默认测试不调用真实模型。PostgreSQL 集成测试默认跳过；显式启用：

```powershell
$env:M0_TEST_ADMIN_URL="postgresql://ticketpilot:local-dev-only@127.0.0.1:5432/ticketpilot"
uv run --locked pytest -q
```

测试创建随机命名的独立数据库，测试后仅删除自己创建的库，不清空演示库。它验证空库迁移、重复初始化、迁移前后就绪状态、两个独立进程恢复、错误 thread_id、重复恢复，以及 M1 的业务、权限、并发和事务回滚场景。M0_TEST_ADMIN_URL 需要本地测试角色具备 CREATE DATABASE 权限。

## 健康检查语义

- `/health/live`：进程存活，返回 200，不访问数据库。
- `/health/ready`：应用迁移版本和 checkpoint 表/迁移完整时返回 200；未初始化或数据库不可用时返回 503。该接口不调用模型。

## 已知限制

模拟业务写接口仅内部 Worker 持有写凭证；外部用户只提交审批决定。模型没有写工具，也不能通过 resume payload 自行授权。锁定 checkpoint 包导入时存在上游 LangChainPendingDeprecationWarning；已运行的恢复测试未受影响，后续升级必须重跑相同实验。Docker 首次拉取或构建需要网络；Docker Desktop 需启用 Linux Engine。此项目只使用本地演示数据库密码和合成数据，不包含生产认证或生产业务系统。
