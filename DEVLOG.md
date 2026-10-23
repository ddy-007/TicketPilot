# TicketPilot 开发记录

## 2026-10-02：M0 初始实现

### 已完成

- 创建后端 `pyproject.toml` 并使用 uv 解析依赖。
- 生成 `backend/uv.lock`（当前目录尚未初始化 Git，没有提交记录）。
- 创建 FastAPI live/ready 健康检查。
- 创建 Alembic 配置和 M0 最小迁移。
- 创建 LangGraph + PostgreSQL checkpoint 恢复实验入口。
- 创建真实模型基础调用、工具调用和结构化输出冒烟入口。
- 创建最小 Compose PostgreSQL 服务和 `.env.example`。
- 创建健康检查单元测试。
- Uvicorn 在 8010 端口启动成功；`/health/live` 返回 200。
- `/health/ready` 在数据库不可用时返回 503，数据库和 checkpoint 状态均为 `error`。

### 已验证

```text
uv lock                         通过，解析 69 个依赖
uv sync                         通过，虚拟环境同步完成
uv run pytest -q                1 passed
uv run ruff check ...           All checks passed
uv run ruff format --check ...  10 files already formatted
uvicorn :8010 /health/live     200 {"status":"ok"}
uvicorn :8010 /health/ready    503 database=error checkpoint=error
```

### 尚未验证

- Docker PostgreSQL 容器、Alembic 实际迁移。
- `/health/ready` 的数据库和 checkpoint 检查。
- 真实模型文本、工具调用和结构化输出。
- 关闭并重启进程后的 PostgreSQL checkpoint 恢复。

### 当时阻塞（已于 2026-10-19 解除）

Docker CLI 已安装，但 Docker Desktop Engine 启动失败。Docker Desktop 状态报告：

```text
engine linux/wsl failed to start
Virtual Machine Platform not enabled
No virtualization available
```

需要先在 Windows 功能中启用 Virtual Machine Platform，并确认 BIOS/UEFI 已启用硬件虚拟化；这可能需要管理员权限和重启。此阻塞解除前，不能把 M0 PostgreSQL 或恢复验收标记为通过。

## 2026-10-19：Docker 恢复可用，完成 M0 数据库和恢复验收

### 实际改动

- `backend/app/config.py`：统一读取根目录 `.env`，隐藏敏感输入；模型配置仅在模型实验中必填。
- `backend/app/db.py`：固定 checkpoint schema；健康检查验证 Alembic 版本、checkpoint 表及迁移状态；增加连接超时。
- `backend/app/main.py`：应用生命周期内复用数据库引擎，并在退出时释放。
- `backend/app/migrate.py`：新增一次性 Alembic + PostgresSaver 初始化入口。
- `backend/migrations/env.py`、`backend/alembic.ini`：与应用共用配置来源，移除硬编码默认 DSN。
- `backend/app/experiments/recovery.py`：初始化与运行分离；读取持久化 snapshot，校验暂停/恢复结果；拒绝不存在或重复恢复的 thread。
- `backend/app/experiments/llm_smoke.py`：补齐只读工具参数验证、工具执行结果回传、最终响应和 usage 记录；输出安全错误分类。
- `backend/tests/test_health.py`、`backend/tests/test_llm_smoke.py`、`backend/tests/integration/test_m0_postgres.py`：健康检查、离线模型协议、隔离 PostgreSQL 和双进程恢复测试。
- `backend/Dockerfile`、`backend/.dockerignore`、`compose.yaml`：新增 M0 镜像、迁移/API/恢复/模型实验服务，显式按服务注入变量。
- `README.md`：更新可复现的 Docker 和宿主机命令、配置入口、验收和限制。
- `TicketPilot-详细设计文档.md`：澄清模型配置仅在模型实验中必填，以及 M0 当前实际范围。
- 创建本地 `.env`：从 `.env.example` 复制，模型三项仍为空；由 `.gitignore` 忽略，未加入镜像。
- `backend/uv.lock` 未升级，继续使用已有锁定版本。

### 实际运行和结果

```text
docker version                          Server 29.8.1 / linux
docker compose up -d --wait postgres     Healthy，PostgreSQL 16.15
uv run --locked python -m app.migrate    initialized，Alembic 0001_m0_metadata
重复 app.migrate                        initialized，无重复建表错误
uv run --locked pytest -q               9 passed（含真实独立 PostgreSQL 测试库）
uv run --locked ruff check              All checks passed
uv run --locked ruff format --check     13 files already formatted
uv lock --check                         通过，继续使用已有 uv.lock
docker compose build migrate            构建成功，uv --frozen 安装 60 个生产依赖
docker compose run --rm migrate         initialized，退出码 0
docker compose up -d --wait api          api Healthy，migrate Exited(0)
GET http://127.0.0.1:8010/health/live     200 {"status":"ok"}
GET http://127.0.0.1:8010/health/ready    200 {"status":"ready","database":"ok","checkpoint":"ok"}
```

宿主机独立进程恢复：

```text
thread_id: m0-20261019-validation
pause  pid=18604 -> interrupted, interrupt_count=1, pending_nodes=[wait_for_approval]
resume pid=12336 -> resumed, interrupt_count=0, pending_nodes=[], decision={approved:true}
```

两个进程分别启动，第一个命令退出后才运行第二个命令；状态来自 PostgreSQL，未使用内存 saver。

Docker 独立容器恢复：

```text
thread_id: m0-container-9d70f289160f422299e697f85376b968
container ...0c1b23888559 -> interrupted, interrupt_count=1
container ...de126db3501e -> resumed, interrupt_count=0, pending_nodes=[]
```

容器中 PID 均为 1，属于不同容器的进程，不能据此把它们当作同一进程。

测试在随机命名的独立数据库中验证了空库迁移、重复初始化、迁移前后就绪状态、双进程恢复、错误 thread_id、重复 resume 和重复 pause 拦截；测试结束后仅删除该次创建的测试库。模型测试使用 httpx.MockTransport，未调用供应商，不计入真实模型通过。

### 版本、文档和限制

- 宿主机 Python 3.12.13；后端容器 Python 3.12.12。
- 后端基础镜像内 uv 为 0.9.30，已实测可用 frozen 模式安装现有锁文件；没有重新解析或升级项目依赖。
- LangGraph 0.6.11、checkpoint-postgres 3.0.5、checkpoint 3.0.1、OpenAI SDK 1.109.1。
- PostgresSaver setup 和 snapshot 字段先检查锁定包的源码、随包官方 README，再运行最小真实 PostgreSQL 实验；没有按新版本文档猜接口。
- 查阅 LangGraph 官方 interrupts 文档及 uv 官方 Docker 集成文档。OpenAI 文档站请求返回 403，SDK 调用形状通过本机锁定 SDK 与离线协议测试核对；真实供应商兼容性仍未验证。
- Docker Hub 的 Python 基础镜像请求曾连接超时；最终改用已实测下载成功的 uv 官方 Python 镜像并固定 digest。PostgreSQL 镜像也固定 digest；未修改系统代理或 Docker 全局设置。
- 锁定 checkpoint 包仍产生一条上游 LangChainPendingDeprecationWarning；未影响本次测试，升级时必须重跑恢复实验。

### 当时剩余验收门槛（已由下述真实验收解除）

```text
docker compose run --rm llm-smoke --kind all
退出码 1
CONFIGURATION_ERROR: Missing model configuration: LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
```

根目录 `.env` 需要用户填写模型地址、模型名和 API Key。没有调用真实模型，没有发生供应商请求费用。M0 整体验收仍未完成，尚未进入 M1。

## 2026-10-19：真实模型验收完成，M0 通过

### 实际改动

- `backend/app/experiments/llm_smoke.py`：按真实最小请求验证的协议适配当前官方接口；实验使用非思考模式，工具保持本地严格参数校验，结构化输出使用 JSON Object + Pydantic；usage 只输出标准 token 计数。
- `backend/tests/test_llm_smoke.py`：增加当前接口的请求协议测试，以及上游错误含敏感内容时不回显的日志测试。
- `.gitignore`：补充忽略 `.env.*`，保留可提交的 `.env.example`。
- `README.md`、`TicketPilot-详细设计文档.md`：更新真实验收状态和结构化输出的实际保证。
- `artifacts/m0-llm-2026-10-19.json`：本地验收摘要，由 `.gitignore` 忽略；没有密钥、接口地址、请求头或模型响应正文。

### 协议差异与最小实验

首次真实调用中，文本通过，工具与 JSON Schema 请求返回 HTTP 400。仅提取不含配置值的错误类别和参数名后，确认接口的强制工具选择与思考模式冲突，且当前 Chat Completions 接口使用 JSON Object。

查阅供应商官方 Chat Completions 和 Tool Calls 文档，并先运行最小真实请求：关闭思考模式后工具调用、只读结果回传及最终响应通过；JSON Object 输出也通过 Pydantic 校验。随后才修改正式实验脚本。没有修改用户的模型名、接口地址或密钥。

这不等于验证了供应商的原生 JSON Schema/strict 输出保证；服务端 Pydantic 校验仍是约束边界。工具实验仅使用合成数据和固定只读工具，没有业务写入。

### 完整 Docker 真实验收

```text
docker compose build migrate                       退出码 0
docker compose run --rm llm-smoke --kind all        退出码 0
text        passed   1275 ms   prompt=11   completion=1   total=12
tool        passed   1688 ms   prompt=319  completion=37  total=356
工具结果回传响应                prompt=95   completion=1   total=96
structured  passed    852 ms   prompt=155  completion=26  total=181
structured validation_mode=json_object+pydantic
```

以上仅为最终成功验收的四个实际请求，共 645 tokens；不包含之前排障请求，不能当作本次全部账单或项目单任务成本。此前错误请求没有用量数据，不记为零成本。实际模型标识和逐项计数在本地忽略的验收摘要中，未写入公开文档。

M0 的数据库迁移、健康检查、跨进程恢复与依赖锁定已在上一记录实测通过；现已补齐真实模型的文本、工具调用和结构化输出门槛。停留在 M0，不自动进入 M1。

### 最终回归与敏感信息检查

```text
uv run --locked pytest -q               11 passed（包含隔离 PostgreSQL 恢复测试）
uv run --locked ruff check              All checks passed
uv run --locked ruff format --check     13 files already formatted
uv lock --check                         通过
API 容器                               Healthy
/health/live 和 /health/ready           均 HTTP 200
源码、文档与本地报告检查                 27 个文件中未匹配到当前 API Key
后端镜像环境包含当前 API Key             false
API 容器中的 LLM 环境变量数              0
```

检查排除了本地 `.env`、虚拟环境和缓存；没有读取配置值到工具输出，也没有打印完整上游错误或请求头。模型实验容器在命令结束后由 `--rm` 删除。日志脱敏测试使用虚构的敏感标记验证上游响应正文不被回显。仍有前述上游 deprecation warning，未阻断测试。

## 2026-10-19：M0 审查修复及 M1 交付

用户明确授权审查 M0 后继续 M1；本轮没有进入 M2。

### M0 审查发现与修复

1. **P1，截断响应可能误判通过**：`backend/app/experiments/llm_smoke.py` 原先只检查文本/JSON 是否可解析，没有要求正确的 finish_reason。现在要求普通输出为 stop、工具请求为 tool_calls；有效 JSON 但 finish_reason=length 的响应也拒绝。
2. **P1，模型地址缺乏传输检查**：`backend/app/config.py` 新增 HTTPS 校验（仅本机回环允许 HTTP），拒绝内嵌凭证、query 或 fragment，错误不回显地址；地址和模型字段从配置 repr 隐藏。
3. **P2，就绪检查在升级后误报或漏报**：`backend/app/db.py` 原本将 Alembic head 写死为 0001，且 checkpoint 只看 max(v)。改为从迁移目录解析 head、读取 app 元数据，并校验 checkpoint 迁移编号完整集合。缺少元数据表或中间 checkpoint 迁移时返回 false。
4. **P2，设计文档示例与实际命令不一致**：修正健康检查端口为 8010，模型命令为 llm-smoke --kind all，暂停/恢复命令使用 recovery 服务；Python 约定与实测 3.12 保持一致。

M0 修复后先运行 17 项测试，全部通过，才开始 M1。最终还以更新后的镜像重新运行真实模型三项，text/tool/structured 全部通过，退出码 0（1269 / 1523 / 941 ms）；仅输出脱敏摘要，没有重复保存配置或原始错误。本轮真实模型复测是额外供应商请求，不计入之前的 645-token 报告。

### M1 实际改动文件

- `backend/migrations/versions/0002_simulator.py`：sim 的订单、工单、履约事件、幂等记录；租户复合主键/外键、枚举及正版本约束、操作事件唯一约束。
- `backend/simulator/models.py`：业务表 SQLAlchemy Core 映射；没有使用 create_all 建表。
- `backend/simulator/config.py`：独立模拟器配置，内部读写 Token 至少 32 字符且不同。
- `backend/simulator/service.py`：绑定校验、稳定请求语义哈希、幂等记录查询、订单锁内规则检查和事务写入。
- `backend/simulator/main.py`：工单/订单/事件读取、工单列表、履约重试和操作结果查询；常量时间凭证比较、安全验证错误响应和数据库错误响应；可注入测试时钟。
- `backend/simulator/seed.py`：只在 local/test 下补充缺失 S01～S06 数据，不清库、不重置已执行订单。
- `backend/simulator/smoke.py`：通过真实容器网络 HTTP 路径进行查询、并发重试、重放和执行后验证，不调用模型。
- `backend/simulator/__init__.py`：包入口。
- `backend/tests/integration/test_m1_simulator.py`：24 项真实 PostgreSQL + HTTP 集成检查。
- `backend/Dockerfile`、`compose.yaml`：纳入模拟器镜像和服务；模拟器不发布宿主机端口，显式只注入数据库与内部凭证。
- `.env.example`：新增模拟器配置占位符。本地 `.env` 仅追加缺失的随机读写 Token 和本机模拟器数据库地址，未改变已有模型参数。
- `README.md`、`TicketPilot-详细设计文档.md`、本 DEVLOG：同步 M0 修复、M1 命令、协议和实际验收状态。
- 没有新增依赖，`backend/uv.lock` 保持原版本。

### M1 行为与边界

所有订单读写都检查租户和可信工单绑定；操作结果查询同样要求 X-Ticket-ID。写凭证允许读取，读凭证调用 POST 返回 403。未授权、跨租户、绑定错误请求不会产生业务效果。模型没有注册任何写工具，M1 的写接口仅为内部业务 API；正式审批、不可变提案及 Worker 将在 M3 接入。

写事务先查幂等结果，锁定可信 Ticket 后锁定 Order，再次查幂等记录，验证版本、已支付、FAILED、库存预留、支付至少 30 分钟和最新失败码。订单改为 QUEUED/v+1、RETRY_ENQUEUED 事件与幂等响应在同一事务提交。相同业务键重传返回原结果；变更语义返回 409，不能更换随机键规避当前状态检查。

### 实际命令与结果

```text
uv run --locked ruff check app migrations tests simulator          All checks passed
uv run --locked ruff format --check app migrations tests simulator  22 files already formatted
uv lock --check                                                    通过，69 个依赖保持锁定
M0_TEST_ADMIN_URL=<本地测试连接> uv run --locked pytest -q           41 passed, 1 warning
docker compose build migrate                                      构建成功
docker compose run --rm migrate                                   0001 -> 0002_simulator，退出码 0
docker compose run --rm simulator python -m simulator.seed --scenario demo
                                                                  seeded S01-S06, reset=false
docker compose up -d --wait api simulator                          均 healthy
docker compose run --rm simulator python -m simulator.smoke        passed，退出码 0
重复 simulator.smoke                                              passed，仍只有一个重试事件
GET http://127.0.0.1:8010/health/ready                              200 database=ok checkpoint=ok
```

容器 HTTP 验收摘要：

```json
{"milestone":"M1","status":"passed","state":"QUEUED","version":4,"retry_events":1,"concurrent_requests":2,"read_token_write_status":403,"ticket_status":"OPEN","llm_calls":0}
```

首次容器写入产生一次真实模拟业务状态变化；重复验收使用同一键，返回既有结果。O2001 当前为 QUEUED/v4，T1001 保持 OPEN，没有发货或关单效果。

24 项 M1 测试每个模块使用随机创建的独立数据库，涵盖 S01～S06、退款/取消/未开始、精确 30 分钟边界、未来/缺失支付时间、最新错误码和缺失证据、严格类型/额外字段、权限与跨租户/绑定、跨租户外键、重放/冲突和查询。并发测试用不同可信工单绑定同一订单，并通过屏障强制两个请求均在最初查询不到幂等记录后再竞争订单锁；最终只产生一条事件。分别在插入事件、保存幂等结果时注入失败，确认订单、事件、幂等结果一起回滚。所有故障仅在测试函数中注入，没有 HTTP 清库、改时或故障开关。

### 敏感信息与已知限制

- 扫描源码、文档和本地报告 36 个文件，未匹配到当前模型密钥或新增内部 Token；镜像元数据中未发现这些值。
- 模拟器端口不向宿主机发布，API 未注入内部写凭证或模型密钥，凭证没有进入模型消息。
- 初期 HTTP 宿主机冒烟请求未拿到有效响应；已采用最终设计要求的容器内部网络路径实测通过，没有为排障继续保留宿主机模拟器端口。
- 仍有锁定 checkpoint 包的上游 deprecation warning，恢复测试通过。认证仍为本地合成演示凭证；没有公开部署或生产安全承诺。
- 审批、Agent 诊断和前端尚未实现，属于 M2～M4；本轮验收完成后停在 M1。

## 2026-10-19：M1 专项审查与 bug 修复

### 审查范围

核对 M1 规格与实际迁移、内部 API、凭证、绑定检查、业务事务、幂等重放、种子和验收脚本。只推进 M1 修复，没有开始 M2，也没有再次调用真实模型或显示本地配置值。

### 已复现并修复的发现

1. **P1：seed 对已有订单补造失败证据**。原先判断“没有事件”即插入 QUEUE_PUBLISH_TIMEOUT 事件；已有订单因数据缺失没有失败证据时，重跑 seed 会让其满足重试规则。`backend/simulator/seed.py` 现使用 INSERT ... RETURNING，只给同一事务中新建的订单初始化事件，已有订单缺失历史继续保持未知。真实 PostgreSQL 回归测试验证 seed 不会产生事件，写 API 仍返回 ERROR_CODE_NOT_ALLOWED，零业务效果。
2. **P2：验收脚本只确认历史成功，未核对当前订单**。原先确认操作响应及重试事件后直接输出 passed；即使订单后续再次 FAILED/v5，也能误报 QUEUED/v4。`backend/simulator/smoke.py` 现在重新读取订单，检查当前状态为 QUEUED、版本不小于已确认结果，并验证事件属于目标订单；输出实际当前状态。响应替身复现旧问题并验证修复后的失败退出。
3. **P2：内部 Token 配置接受已知占位符和空白**。原先只校验长度与不同值，直接复制 `.env.example` 便能使用公开占位符，带空白/换行的值也能通过启动检查。`backend/simulator/config.py` 改为要求 32～256 个无空白的可打印 ASCII 字符，并拒绝 replace-with- 占位符，错误只报告规则，不回显值。未更改本地已有的随机凭证。
4. **P2：Python 优化模式禁用验收条件和部分请求**。原先使用 assert，一旦开启 -O，校验和嵌在断言中的 HTTP 请求会被删除，零事件也可打印 passed。验收脚本已改为显式 require 检查，失败安全退出 1；子进程 -O 回归测试验证错误状态不会通过。

修复前分别执行失败用例，前 6 项失败、优化模式专项用例也失败，证明上述问题可复现；修复后全部通过。

### 改动文件

- `backend/simulator/seed.py`：限制新建订单的初始证据生成。
- `backend/simulator/config.py`：拒绝不安全/不可用的演示 Token 配置。
- `backend/simulator/smoke.py`：重读实际状态、核对目标订单和显式验收检查。
- `backend/tests/integration/test_m1_simulator.py`：新增已有订单缺失历史的真实 PostgreSQL 回归测试。
- `backend/tests/test_m1_review.py`：新增四项 Token 检查、历史成功误报检查及 -O 子进程检查。
- `README.md`、本文件：更新规则、命令保证和实测结果。

### 实际验证结果

```text
M0_TEST_ADMIN_URL=<本地测试连接> uv run --locked pytest -q
                                                48 passed, 1 warning in 34.35s
uv run --locked ruff check app migrations tests simulator
                                                All checks passed
uv run --locked ruff format --check app migrations tests simulator
                                                23 files already formatted
uv lock --check                                 通过，继续使用原依赖锁文件
docker compose config --quiet                   退出码 0
docker compose build migrate                    退出码 0
docker compose up -d --wait api simulator        api / simulator healthy
docker compose run --rm simulator python -m simulator.seed --scenario demo
                                                seeded S01-S06, reset=false
docker compose run --rm simulator python -m simulator.smoke
                                                passed，QUEUED/v4，retry_events=1
docker compose run --rm simulator python -O -m simulator.smoke
                                                passed，QUEUED/v4，retry_events=1
GET /health/ready                               200 database=ok checkpoint=ok
```

测试包含原有租户/绑定/只读禁写、强制并发交错、事件和幂等记录写入失败的事务回滚、M0 双进程恢复。新的数据库用例仍在临时独立数据库运行，没有删除演示业务记录或重置 O2001。存在一条原有 checkpoint 包弃用警告，不影响验收。没有修改业务表迁移或新增依赖。

最终扫描源码、文档和本地报告 37 个文件，当前模型密钥及内部 Token 的匹配数为 0；后端镜像元数据也未包含这些敏感值。扫描没有将值输出到日志，未改动 `.env`。

## 2026-10-19：按计划重新确认 M1 完成状态

再次对照原规格第 18 节 M1 及详细设计的 M1 清单，种子数据、三个只读接口、履约重试与操作查询、业务规则、版本检查、幂等事务及 S01～S06 测试均已交付，没有剩余 M1 开发项。本轮仅更新本记录，未新增业务功能或进入 M2。

```text
uv run --locked pytest tests/integration/test_m1_simulator.py tests/test_m1_review.py -q
    31 passed in 7.38s
docker compose run --rm simulator python -m simulator.smoke
    退出码 0；M1 passed；QUEUED/v4；retry_events=1；read_token_write_status=403；ticket_status=OPEN
GET http://127.0.0.1:8010/health/ready
    200，database=ok，checkpoint=ok
```

专项测试使用隔离 PostgreSQL 测试库。容器验收使用原业务幂等键，重试事件数未增加；本轮没有调用模型或读取密钥到输出。下一里程碑是 M2，只在用户明确要求进入该阶段后开始。

## 2026-10-19：M2 只读 Agent 交付与验收

用户明确授权进入 M2；本轮仅完成只读诊断，不进入 M3 审批/任务队列/业务执行。

### 实际改动文件

- `backend/app/agent/__init__.py`：M2 包入口。
- `backend/app/agent/schemas.py`：三个严格只读参数 schema、上游数据 allowlist、结构化 Diagnosis、冻结提案模型与工具注册表。
- `backend/app/agent/tools.py`：当前工单/订单绑定校验、受限 GET 读取、网络重试、大小限制、证据 ID/观测时间和每次实际 HTTP 尝试记录；配置秘密的精确值替换。
- `backend/app/agent/llm.py`：锁定 OpenAI SDK 调用适配、自动工具选择和结构化输出；当前供应商使用已验证的非思考模式及 JSON Object + Pydantic，其他兼容接口使用 JSON Schema + Pydantic。
- `backend/app/agent/graph.py`：LangGraph load/reason/read/diagnose/validate 循环、预算、一次格式修复、证据引用校验、服务端规则和不可执行提案草稿。
- `backend/app/agent/policy.py`：从本轮结构化真实证据重新计算诊断条件、固定最终摘要和服务端 SHA-256 提案哈希；模型保守建议不被自动提升为操作。
- `backend/app/agent/store.py`：持久化诊断结果与证据，重读结果必须匹配租户。
- `backend/app/agent/cli.py`：可信本地开发 CLI；创建诊断、按租户重读 run、可选 details 查看已脱敏证据及提案。
- `backend/migrations/versions/0003_m2_diagnostics.py`：app.diagnostic_runs 和 app.diagnostic_evidence、关联约束及索引。
- `backend/app/config.py`、`.env.example`：只读模拟器配置及模型/读取预算，不读取或注入模拟器写凭证。
- `compose.yaml`：新增 tools profile 下的 diagnose 命令服务，仅注入内部只读凭证和模型配置。
- `backend/tests/__init__.py`、`backend/tests/agent_fakes.py`：确定性模型协议替身，仅供测试。
- `backend/tests/test_m2_agent.py`、`backend/tests/test_m2_protocol.py`、`backend/tests/integration/test_m2_agent.py`：只读边界、SDK 协议、预算和 12 条代表性真实 PostgreSQL/HTTP 案例。
- `README.md`、`TicketPilot-详细设计文档.md`、本文件：命令、阶段结果语义、测试边界和实际结果。
- 本地忽略文件 `artifacts/m2-real-2026-10-19.json`、`artifacts/m2-real-refined-2026-10-19.json`、`artifacts/m2-real-isolated-2026-10-19.json`：脱敏的真实模型运行摘要，保留失败样本，不包含密钥、接口地址、请求头或完整模型响应。

没有新增 Python 依赖或升级锁文件。原始项目开发文档 SHA-256 未变化。

### 实现边界与版本验证

固定 LangGraph 0.6.11，先实际运行一个 TypedDict StateGraph 条件循环确认 add_conditional_edges/END 行为，再实现正式图。OpenAI 文档站请求返回 403，改为读取锁定 SDK v1.109.1 的官方 README、供应商官方 function calling 文档和本机接口签名，使用离线请求协议测试及真实最小请求确认行为；没有根据未锁定的新接口猜实现。

只有 get_ticket、get_order、get_fulfillment_events 能注册给模型；未知工具、额外字段、非法类型和跨工单/订单参数在任何对应 HTTP 请求发出前拒绝。整个批次先校验再执行。首次服务端加载工单得到可信绑定，模型不能指定租户或替换绑定。

预算为最多 6 次模型调用、reason 最多 4 次、最多 10 次逻辑读（含服务端初次读取）；模型输出最多 800 tokens。读请求 2 秒连接、5 秒读取，只对网络错误/429/暂时性 5xx 做初次外最多 2 次重试；逐次记录 attempt。总活跃时间目标 120 秒、上下文字符上限 64000、每次请求前检查已知 token 总量 12000。未知用量显式记录，不能当作费用为零，也不声称绝对精确 token 硬限。

诊断必须严格解析并引用当前 run 的真实 evidence_id，关键订单/事件证据必须被引用。服务端重新计算支付、履约、库存、支付时间和最新失败码等事实；不一致、越权或不足时不会生成提案。最终展示采用核对后的服务端模板，不直接显示模型自由文本中的完成声明。

满足条件的模型建议生成 PROPOSAL_READY，只包含服务端构造的冻结参数、证据引用、30 分钟期限与计划哈希，executable=false、executed=false。该状态不等于 WAITING_APPROVAL；正式审批存储和恢复在 M3 才实现。M2 只持久化只读结果和证据，不宣称可恢复执行尚未结束的诊断。

### 确定性测试与实际运行结果

```text
M0_TEST_ADMIN_URL=<本地测试连接> uv run --locked pytest -q
    82 passed, 1 warning in 38.66s
最后的 active_elapsed_ms 输出调整后，再运行 M2 三个测试模块
    34 passed, 1 warning in 6.22s
uv run --locked ruff check app migrations tests simulator
    All checks passed
uv run --locked ruff format --check app migrations tests simulator
    37 files already formatted
uv lock --check
    通过，69 个依赖保持原锁定版本
docker compose build migrate
    构建通过，退出码 0
docker compose run --rm migrate
    0002_simulator -> 0003_m2_diagnostics；initialized
docker compose up -d --wait api simulator
    均 healthy
GET /health/ready
    200，database=ok，checkpoint=ok
```

12 条代表性案例用真实隔离 PostgreSQL、真实模拟器 HTTP 路由和固定模型替身：正常重试建议、库存不足、已发货、已入队、未支付、缺绑定、暂时读取失败、持续读取失败、错误失败码、注入跨订单、支付不足 30 分钟、模型保守转人工。断言所有业务请求 GET，订单内容不变、零重试事件、零业务幂等记录，证据持久化并可按 run 重读，跨租户不可读取。暂时失败的 HTTP 尝试数与逻辑读次数单独验证。该 12/12 是软件测试结果，不是模型准确率。

其他测试覆盖工具白名单、JSON 额外字段/类型/limit、越界请求零 HTTP、伪造 evidence_id、保守建议不升级、一次格式修复且计入调用次数、重复修复停止、模型/读取/token/时间预算、上游 404/超大响应/错误租户数据、模型截断及错误响应脱敏、当前供应商与一般兼容接口的请求形状。没有暴露任何写工具。

### 真实模型验证：成功与失败分开记录

开发期间的实际结果：

| 阶段/对象 | 结果 | 模型请求数 | 已知 total tokens |
|---|---|---:|---:|
| 初次容器 T1002 库存不足 | ESCALATED / INVENTORY_UNAVAILABLE | 3 | 4188 |
| 初次容器 T1001 已入队 | ESCALATED / DIAGNOSIS_CONFLICT，提案被拦截 | 4 | 6063 |
| 明确状态优先级后 T1001 | SUCCEEDED / ALREADY_QUEUED | 3 | 5197 |
| 容器 T1003 已发货 | SUCCEEDED / ALREADY_SHIPPED | 4 | 7047 |
| 容器 T1005 未支付 | ESCALATED / POLICY_BLOCKED，建议与诊断组合不符 | 3 | 5533 |
| 容器 T1006 缺少绑定 | ESCALATED / INSUFFICIENT_EVIDENCE | 2 | 2328 |
| 最终约束下的隔离库 T1001（FAILED/v3） | PROPOSAL_READY / QUEUE_PUBLISH_TIMEOUT | 4 | 7474 |
| 最终约束下的隔离库 T1005（未支付） | ESCALATED / PAYMENT_NOT_READY | 3 | 4978 |

初次失败表明历史失败事件不能优先于当前订单状态；随后在提示词中明确优先级、诊断/建议配对，并整理当前证据再生成诊断。服务端从未依赖这些提示词来授予写权限，失败样本都被阻断提案，未作为成功样本报告。

隔离库真实模型验证使用新的随机测试数据库，最新迁移和新种子；业务读取通过实际 FastAPI 路由与 PostgreSQL，只有模型是真实外部 API，业务传输由 TestClient/HTTP transport 连接路由。验证后订单 O2001 仍为 FAILED/v3、零 RETRY_ENQUEUED，全部请求 GET；随后仅删除该次创建的测试数据库。正常提案和未支付转人工最终均通过。现有演示库没有被重置。

真实模型结果是少量开发抽测，不是冻结测试集或总体成功率。本轮使用现有密钥发生真实供应商请求和费用，未估算价格；token 数据只反映对应成功返回 usage 的调用。

### 持久化与敏感信息检查

新进程通过 `diagnose --tenant-id demo --show-run <id> --details` 成功重读之前的 SUCCEEDED 结果和 3 条证据，executed=false；此命令不再次请求模型。诊断容器运行时检查：只读凭证存在，SIMULATOR_WRITE_TOKEN 不存在。

扫描源码、文档及本地报告 54 个文件，当前 API Key、内部读写 Token 的精确匹配数为 0；镜像元数据中也没有这些值。模型客户端仅收到受限消息和业务证据，不含身份 Token/连接串；上游响应正文及模型内部思维链未保存。`.env` 未修改。

仍有锁定 checkpoint 包的上游弃用警告，测试未受影响。CLI 是可信本地开发工具，不是公开的租户身份认证；M3 的身份/审批/持久任务接口尚未实现。本轮 M2 验收通过，停留在 M2。

## 2026-10-19：按 README 实跑命令行并专项审查 M2

### README 实际命令

执行 `docker compose run --rm diagnose --tenant-id demo --ticket-id T1002`，真实模型返回 ESCALATED / INVENTORY_UNAVAILABLE，executed=false，退出码 0。接着运行 `--show-run <run_id> --details`，另一个进程成功重读相同 run 和 4 条证据，未生成提案。

修复前实跑记录：run_id=71577e99-6eb1-445a-92dd-05be7ab7f4f5；4 次模型请求、4 次逻辑读取、known_total_tokens=7420。修复后复测：run_id=9c3f2bcc-fec4-4f7e-9717-9867fa369a7d；同样正确转人工，4 次模型请求、4 次逻辑读/HTTP 尝试、known_total_tokens=7446，usage_unknown=false。数据和计数均可跨进程重读。这是两次真实供应商调用流程，可能产生费用；不计入此前的抽测报告，也不估计价格。

本地忽略的 `artifacts/m2-review-cli-2026-10-19.json` 与 `artifacts/m2-review-final-cli-2026-10-19.json` 保存脱敏摘要，不含密钥、连接地址或模型响应正文。

### 可复现问题与修复

1. **P2，读取预算边界误转人工**：逻辑读取刚好达到上限，即使已有 ticket/order/events 全部证据，reason 节点仍直接 READ_BUDGET_EXHAUSTED。现在停止继续读，并在事实充分时进入结构化诊断；证据不足仍停止。测试用 limit=3 验证正常提案可完成，冲突诊断仍被 DIAGNOSIS_CONFLICT 阻断，绝不增加 HTTP 请求。
2. **P2，零值过期时间失效**：ReadTools 使用 `deadline or ...`，将有效的 0.0 过期值当作未设置并延长 120 秒。现在仅在 deadline is None 时创建默认值，已到期的 0.0 不会发出网络请求。
3. **P2，畸形响应导致未捕获异常或延后拒绝**：锁定 SDK 默认响应构造较宽松；缺少 tool.function 时抛 AttributeError，缺少 arguments 时可穿过适配器。现对 SDK 响应转为字典后使用锁定 ChatCompletion 类型严格重校验，再访问嵌套字段；异常转换为不含原始响应的 LLM_PROTOCOL_ERROR，Agent 可记录技术失败。
4. **P2，协议拒绝丢失已知用量**：响应有合法 usage 但因 finish_reason=length 被拒绝时，原先累计为 0 并标记未知。现在 ModelError 携带已校验 usage/模型标识，图在错误轨迹中记录并累计；测试中截断响应仍为 FAILED，但 known_total_tokens=25、usage_unknown=false。网络/鉴权失败且未获得可信 usage 时继续标记未知。
5. **P1，序列化警告可能泄露上游字段**：对 SDK 畸形对象调用 model_dump() 可能触发包含原始 input_value 的 Pydantic 警告。使用虚构私密标记已复现该警告；改为 `model_dump(warnings=False)` 后仍执行严格响应校验，日志不输出标记或原始响应。

前六个回归用例在修复前全部失败；后新增的警告脱敏回归也先失败后修复。没有降低工具白名单、证据引用或业务规则。

### 实际改动文件

- `backend/app/agent/graph.py`：证据充分时的读取预算收尾、失败响应已知 usage 记录。
- `backend/app/agent/tools.py`：区分 None 和 0.0 的 deadline。
- `backend/app/agent/llm.py`：重校验响应信封、安全错误 metadata、禁止原始字段序列化警告。
- `backend/tests/test_m2_review.py`：7 项回归用例，含两个畸形工具响应、冲突诊断及警告脱敏。
- `README.md`、本文件：修复行为、运行记录与验证边界。

未改动数据库 schema、依赖锁文件、演示业务数据或本地 `.env`，未进入 M3。

### 最终验证

```text
M0_TEST_ADMIN_URL=<本地测试连接> uv run --locked pytest -q
    89 passed, 1 warning in 38.08s
uv run --locked ruff check app migrations tests simulator
    All checks passed
uv run --locked ruff format --check app migrations tests simulator
    38 files already formatted
uv lock --check
    通过，69 个依赖保持锁定
docker compose config --quiet / build migrate / run --rm migrate
    均退出码 0
docker compose up -d --wait api simulator
    均 healthy
GET /health/ready
    200，database=ok，checkpoint=ok
敏感值扫描
    57 个源码/文档/本地报告文件中，当前模型密钥及内部 Token 匹配数 0
后端镜像元数据中的当前敏感值
    false
```

完整测试继续覆盖 M0 恢复、M1 幂等/事务和 M2 12 条代表性案例；没有重新放宽模型输出或规则。原有上游 checkpoint 弃用警告仍存在，不含请求内容且未阻断测试。最终真实 CLI 与跨进程重读已验证，保留失败回归和安全边界；本轮停留在 M2。

## 2026-10-19：M3 审批、持久任务、执行及恢复

### 阶段边界

用户明确授权进入 M3。本阶段实现正式 Run/Job/Approval/Operation、单 Worker、人工审批、受控重试与结果核对；没有实现前端、备注、自动关单、多 Worker 或生产认证。继续使用锁定依赖并保留已说明的上游弃用警告。

### 实际改动文件

- `backend/migrations/versions/0004_m3_execution.py`：app.runs、run_jobs、approvals、operations、tool_calls、run_events；活跃 Run/Job 部分唯一索引、启动键唯一约束、外键、状态约束、审批计划不可变触发器。
- `backend/app/execution/__init__.py`：受控执行包入口，不注册给模型。
- `backend/app/execution/db.py`：数据库访问、哈希、原子入队、事件和每次工具尝试记录。
- `backend/app/execution/api.py`：演示身份验证、工单查询、创建 Run、状态/证据/游标事件查询、不可变审批详情与决定提交。
- `backend/app/execution/business.py`：内部只读与写客户端、有限 GET 重试、受限响应、安全错误和脱敏审计；HTTP 重试保留相同 logical_call_id。
- `backend/app/execution/workflow.py`：复用 M2 诊断节点，准备审批、interrupt、正式决定复核、未知操作查询、实时前检、PREPARED/POST、核对及终态；每键最多两次 POST。
- `backend/app/execution/worker.py`：会话 advisory lock、持久 Job 领取和恢复、过期扫描、checkpoint 判定、最大恢复次数和安全错误处理。
- `backend/app/execution/cli.py`：读取本地角色配置的 HTTP 控制客户端；start/get/events/显式 approve/reject，批准必须提供精确展示哈希。
- `backend/app/config.py`、`backend/app/main.py`：演示身份和写凭证字段、API 路由、安全验证/数据库错误响应。
- `backend/app/agent/graph.py`：将 M2 节点组装为可持久化图，恢复可序列化运行状态/预算、核对启动时可信绑定、模型请求审计回调；不序列化连接和 Token。
- `backend/app/agent/tools.py`：每个外部读取前检查 Worker 会话锁。
- `backend/simulator/seed.py`：新增只补缺的 approval-demo 合成数据 T3001/O3001，避免重置 M1 已执行样例。
- `compose.yaml`：API 仅注入读凭证与角色 Token，Worker 注入内部读写与模型配置；服务仍限制本机/容器内部访问。
- `backend/tests/integration/test_m3_execution.py`：31 项 PostgreSQL + 真实模拟器进程集成/恢复测试。
- `backend/tests/m3_process.py`：只在测试使用的固定时钟和崩溃子进程入口，未打包进生产镜像。
- `backend/tests/test_m3_business.py`：执行阶段 HTTP 重试分组与不保存请求头的回归。
- `README.md`、`TicketPilot-详细设计文档.md`、本文件：启动、角色、审批、恢复、测试和当前限制。
- 本地 `.env`：只补齐缺失的随机 DEMO_OPERATOR_TOKEN / DEMO_APPROVER_TOKEN，没有改模型密钥或已有内部凭证；未提交、未打包进镜像。
- 本地忽略文件 `artifacts/m3-real-approval-2026-10-19.json` 与 `artifacts/m3-real-recheck-2026-10-19.json`：脱敏运行摘要。

未新增依赖或升级 uv.lock。原项目开发文档内容未改。

### 关键约束

创建 Run 与 START Job 同事务提交；同键同请求返回同 Run，不同内容或同工单另一活跃 Run 返回 409。每轮最多一个审批，保存的 plan/哈希/期限/所属 Run 由触发器保护不可变。审批决定统一按 Run → Approval 加锁，与 RESUME Job 同事务；只有 WAITING_APPROVAL 才允许决定；同决定重复请求不新增任务，错误哈希或相反决定返回 409。

Graph 使用 thread_id=run_id。M2 的消息、证据、调用次数和耗时在每个节点 checkpoint 保存；数据库另记模型请求开始/完成，崩溃时未完成的请求保留 usage_unknown，持久意图计数也限制最多 6 次请求，避免丢失节点 checkpoint 后预算重置。正常执行只将数据放入图状态，客户端和配置对象由依赖注入。

Worker 使用单独会话持有 advisory lock 9081701，启动前获取，获取失败退出。领取 Job、标记运行、同步等待/终态分别短事务；图执行不持有业务长事务。锁丢失时停止启动新节点和请求；在途外部请求仍靠 M1 版本与幂等事务控制。

恢复判定先读 checkpoint：无 checkpoint 才初始化；pending interrupt 只同步 WAITING_APPROVAL；正式决定存在才 Command(resume)，其 payload 只是唤醒且被执行节点忽略；无 interrupt 的未完成节点 invoke(None)；已结束 checkpoint 只同步终态，不重跑图。每 Job 最多 3 次自动图执行；恢复已有等待或终态的同步不启动新业务操作。

执行必须读取数据库正式审批、重新校验哈希/租户/绑定/版本/期限；先查本地既有 Operation。PREPARED/UNKNOWN 先查询远端；结果存在则核对，不再次 POST；不存在且审批和实时业务规则仍有效时才允许同键同参数有限重发。出现 404 后版本变化时再查原键，无法确认则保持 UNKNOWN，executed=null 并提示人工核对；过期也不抹掉历史成功事实。请求意图及尝试次数先保存再发 POST。

确认必须同时具有远端操作结果、同 operation_key 的 RETRY_ENQUEUED 事件，以及 QUEUED/SHIPPED 且版本足够的当前订单；只显示重新入队，不声称已发货/已关单。内部备注固定 SKIPPED。

### 最小 API 实验与测试中的修正

先用锁定版本运行失败节点 → invoke(None) → interrupt → Command(resume) 的真实 PostgreSQL 最小实验，确认公开 snapshot.next / interrupts 行为。

首轮批准测试因为 Worker 固定时钟与模拟器系统时钟不同，被 M1 的支付时间规则正确拒绝。修正仅发生在测试入口，模拟器与 Worker 使用同一固定 UTC 时间；生产接口没有 HTTP 改时、故障或清库开关。

### 验收测试

```text
M3 专项首次扩展：29 passed（含真实进程退出与重启）
增加持久模型预算、角色/不支持功能校验后：31 passed in 72.60s
完整回归：120 passed, 1 warning in 106.28s
公共审批响应字段对齐 plan / approval_id 后，批准闭环单独回归：1 passed
HTTP 重试审计分组回归：1 passed
uv run --locked ruff check app migrations tests simulator：All checks passed
uv run --locked ruff format --check app migrations tests simulator：49 files already formatted
uv lock --check：通过，69 个依赖保持原锁定
```

31 项 M3 测试都使用随机创建的独立 PostgreSQL 库与一个真实模拟器子进程，模型使用固定替身，不计入真实模型评测。实际使用 os._exit 终止测试 Worker 子进程，再由新进程恢复，而非用进程内字典假装重启。覆盖：

- 正常批准、拒绝、审批后版本变化、重复/并发决定、operator 无权审批、错误哈希、启动幂等与活跃约束。
- Run/START Job 原子回滚，审批/RESUME Job 原子回滚；计划数据库级不可修改。
- interrupt checkpoint 已保存但 Run 尚未同步；审批事务已提交但尚未恢复；远端已提交但本地 Operation 未确认；终态 checkpoint 已保存但 Job 未结束。
- 写响应丢失后查证成功；查询持续不可用保留 UNKNOWN；查询 404 后旧请求提交导致版本变化时二次查询确认；无记录但版本被其他操作改变时继续 UNKNOWN。
- 过期前后两次验证、空闲过期扫描及释放活跃约束；过期后只确认既有效果；同键有限重发/次数上限。
- 单 Worker 锁竞争、终止锁会话后停止、三次技术故障后不无限重试、伪造 resume 不产生写入、启动后绑定被替换时阻断。
- 跨租户 404、请求字段/角色检查、游标事件分页、模型持久意图预算。

### 真实 Docker / 真实模型闭环

执行 build、迁移到 0004、approval-demo 种子、启动 API/simulator/worker 后，通过真实模型与真实内部 HTTP 完成：

```text
POST /api/tickets/T3001/runs                   202 / QUEUED
run_id                                      23ad7e89-403c-4d52-8014-6e2287fcd1ba
Worker 诊断                                 WAITING_APPROVAL，4 条证据
operator 提交批准                           403
approver 提交相同提案精确哈希                 202
Worker 恢复                                 SUCCEEDED / REQUEUED
Operation                                   CONFIRMED，executed=true
重复批准                                    202，复用原决定
```

直接数据库核对：O3001 为 QUEUED/v4；RETRY_ENQUEUED 只有 1 条；app.operations.post_attempts=1；RESUME Job 只有 1 个；T3001 保持 OPEN。未修改 M1 原有 T1001/O2001。

之后使用本地 control CLI 对 T3001 再创建诊断：run_id=f0c9bd91-9284-4b7d-abbc-e1581c44b353；得到 SUCCEEDED / NO_ACTION_REQUIRED / ALREADY_QUEUED，executed=false，4 次模型请求、known_total_tokens=7721、usage_unknown=false；事件表有 4 条 LLM_CALL_STARTED 和 4 条 LLM_CALL_COMPLETED。CLI get 通过 HTTP 重读相同 Run，原批准请求再次提交仍返回 202，重试事件不增加。

这是少量合成数据真实抽测，不是端到端成功率或生产安全证明；真实模型调用产生供应商费用，未估算价格。恢复与故障测试使用固定模型，不冒充真实模型成绩。

### 安全核对与当前限制

扫描源码、文档和本地报告 69 个文件，当前模型密钥、内部读写凭证及两个身份 Token 的匹配数为 0；镜像元数据未包含这些值，抽查最终 Run checkpoint 也未出现这些值。

实际容器环境核对：API 只持有内部读凭证和 operator/approver Token，没有模型密钥和内部写凭证；Worker 有模型配置及内部读写凭证，没有演示身份 Token。模型消息只包含服务端限制后的业务数据与证据。

API/simulator healthy，Worker 正常持锁运行，/health/ready 返回 200。上游弃用警告仍保留，没有改版本或全局屏蔽。当前只承诺单 Worker、本机演示认证和合成业务；没有多 Worker fencing、公开部署、生产身份认证或前端，自动备注仍未实现。本轮结束停在 M3。

### 最终收尾验证

执行阶段 HTTP 重试分组改动后，最后完整回归为 **121 passed, 1 warning in 111.88s**（其中 M3 集成/恢复 31 项，新增 HTTP 审计回归 1 项）；Ruff 检查通过，49 个 Python 文件格式检查通过，锁文件检查通过。最终镜像重新构建，API/simulator healthy、worker 已启动，健康检查返回 200。

最终敏感信息复扫为 70 个源码/文档/本地报告文件，当前模型密钥、内部凭证和身份 Token 匹配数为 0；镜像元数据也未包含这些值。以上扫描不读取 `.env` 值到输出。测试和真实验收结果分开保留，没有编造成功率或费用。

## 2026-10-19：M3 全面审查及修复

### 审查范围和执行方式

对照原规格第 7、8、12、13、17、18 节，检查 API 身份/租户/幂等、审批不可变及锁顺序、数据库事务、Job 领取/重试上限、checkpoint 恢复、Worker 会话锁、未知写入、实时业务前检、执行后核对、审计和敏感信息边界。先新增失败用例复现，再修复。本轮不进入 M4，不调用真实模型，不修改本地 `.env` 或重置演示业务数据。

### 已复现的 8 项问题

| 级别 | 问题与复现 | 修复 |
|---|---|---|
| P1 | 审批接口在获取 Run/Approval 行锁前取时间；等待数据库锁跨过 expires_at 后仍可接受 APPROVED / 202。测试在获取锁后推进固定时钟，修复前返回 202。 | `decide` 接收时钟函数，在两个行锁都取得后取当前时间；此时过期返回 410、持久化 EXPIRED 并投递收尾 Job。 |
| P1 | 执行阶段在保存 POST 尝试意图前检查有效期，意图提交等待期间过期仍可发送 POST。修复前固定时钟用例实际产生了一个 RETRY_ENQUEUED。 | 意图保存后再次读取正式审批，并在远端客户端 guard 之后、HTTP 发送之前执行授权回调。两个延迟位置的回归都确认零业务效果。 |
| P1 | 审批理由未经脱敏直接存储，误粘贴当前 approver Token 后会被 operator 从审批详情读出。用虚构测试 Token 复现，无真实密钥进入测试输出。 | 存储前替换当前 API 配置中已知的角色/内部/模型凭证及连接串；错误与存储结果均不回显这些值。不宣称自动识别任意未知个人信息。 |
| P2 | Worker 在 try 块外初始化远端客户端/Workflow；初始化 ValueError 直接逃逸，不能按 Job 的 3 次上限恢复。 | 初始化纳入受控异常路径，资源按是否成功创建分别释放；连续三次初始化失败后持久化 JOB_ATTEMPTS_EXHAUSTED，并停止自动重试。 |
| P2 | 第三次尝试已保存成功终态 checkpoint，但在应用状态同步前发生异常时，Worker 直接标 FAILED，失去后续只同步机会。 | 对已保存 waiting/terminal snapshot 优先同步，禁止因计算次数用尽覆盖已确认成功；若同步仍失败，保留仅同步所需的可恢复任务，不重新执行图。成功同步时 failure_code 不再填入成功原因。 |
| P2 | 没有历史写意图、实时业务读取持续 503 时，execute 把技术不可用误称 STALE_PLAN。 | 无既有可能写入时，读取错误返回 FAILED / BUSINESS_UNAVAILABLE；真实版本/绑定变化才走过时提案；已有 PREPARED/UNKNOWN 的情况继续保留未知语义。 |
| P2 | 应用迁移版本正确但 app.operations 被移走时，/health/ready 仍返回 200，无法提示 M3 无法工作。 | 除版本/元数据外，健康检查核对当前 M0～M3 必需的 app/sim 表存在性。独立数据库重命名探针用例得到 503，finally 恢复表名。 |
| P2 | BusinessClient 捕获业务授权异常时统一审计为 BUSINESS_INVALID_RESPONSE，丢失发送前 APPROVAL_EXPIRED 的真实安全类别。 | RemoteError 与 JSON/编码错误分开捕获，保留安全错误码。测试断言无 HTTP 请求，审计记录为 APPROVAL_EXPIRED。 |

前 6 个集成用例在修复前全部失败；健康检查与审计错误码两项也先失败，证明可复现。修复后这 8 项针对性测试均通过，另补一个客户端已进入 request 但实际 dispatch 前过期的用例，仍确认没有 POST 效果。

### 改动文件

- `backend/app/execution/api.py`：行锁之后的审批时钟、审批理由配置秘密脱敏。
- `backend/app/execution/workflow.py`：登记意图与真正 dispatch 之前的正式审批复核；无历史写入时区分前检技术错误。
- `backend/app/execution/business.py`：POST 专用发送前回调、保留 RemoteError 安全审计码。
- `backend/app/execution/worker.py`：初始化受控重试、已保存终态优先同步、成功时不误填 failure_code；过期扫描传递时钟函数。
- `backend/app/db.py`：当前阶段关键表存在性检查。
- `backend/tests/integration/test_m3_execution.py`：新增 8 项独立 PostgreSQL/模拟器回归用例，含两个 dispatch 延迟位置。
- `backend/tests/test_m3_business.py`：发送前授权异常审计回归，断言零 HTTP。
- `README.md`、本文件：更新 M3 时间、恢复、错误与脱敏边界。

没有新增数据库迁移、第三方依赖或运行时功能，uv.lock 未升级。依赖弃用警告继续保留。

### Docker 与既有业务状态复核

最终镜像重新 build，迁移命令成功，API/simulator healthy，Worker 已更新并启动。通过 HTTP 读取原真实验收 Run（23ad7e89-403c-4d52-8014-6e2287fcd1ba），仍为 SUCCEEDED、Operation CONFIRMED；再次相同批准返回 202，operator 审批返回 403，/health/ready 为 200。没有创建新模型诊断或新的业务效果。

直接数据库只读核对：O3001 仍为 QUEUED/v4，RETRY_ENQUEUED=1，RESUME Job=1。原有真实模型验收结果没有被重跑或当作本轮新成绩。修复后的批准/拒绝、过期、恢复和故障行为由独立 PostgreSQL 测试完整验证。

### 敏感信息与已知限制

扫描 70 个源码、文档和本地报告文件，没有匹配到当前模型密钥、内部读写凭证或身份 Token；最终镜像元数据与原真实 Run checkpoint 抽样也没有这些值。API 仍无模型密钥和业务写凭证；Worker 仍无 operator/approver Token。测试中的 Token 泄露复现只使用独立库内的虚构字符串，测试库在结束后删除。

本轮没有改动本地 `.env`、演示订单、审批计划或依赖锁文件；验证限于本机单 Worker 与现有合成数据架构，不声称解决多 Worker 网络分区/fencing、生产认证或绝对安全。

### 最终命令与结果

```text
M0_TEST_ADMIN_URL=<独立库创建连接> uv run --locked pytest -q
    130 passed, 1 warning in 143.56s
uv run --locked ruff check app migrations tests simulator
    All checks passed
uv run --locked ruff format --check app migrations tests simulator
    49 files already formatted
uv lock --check
    通过，继续使用 69 个依赖的原锁文件
docker compose config --quiet
    退出码 0
docker compose build migrate / run --rm migrate
    构建与初始化均成功
docker compose up -d --wait api simulator / up -d worker
    API、模拟器 healthy，Worker 运行
GET /health/ready
    200，database=ok，checkpoint=ok
重复原审批请求
    approver 202，operator 403；订单 QUEUED/v4，重试事件和 RESUME Job 各 1
```

121 → 130 项增加了 9 个回归用例（其中两个用例针对过期 POST 的不同延迟位置）。全部原有并发决定、原子提交、锁丢失、真实进程崩溃、未知结果、晚提交和 M0～M2 测试也通过。保留的一条上游弃用警告与先前相同，不屏蔽、不升级。本轮审查和修复结束，停在 M3。

## 2026-10-19：角色 Token 校验修复与回归

### 问题与修复

角色认证原先只检查两个 Token 长度至少 32 且不相等，因此会接受 `.env.example` 中的 `replace-with-` 占位符，也会接受空白、控制字符和非 ASCII 字符。用合成配置复现后，占位符可以获得 approver 身份。

新增 `app.config.valid_token()`，统一供 API 角色认证和模拟器内部 Token 配置使用：长度必须为 32～256，字符必须是可打印 ASCII（不含空白/控制字符），且不允许大小写不敏感的 `replace-with-` 前缀；两枚 Token 仍必须不同。配置不合规时 API fail closed，返回 `503 DEMO_AUTH_NOT_CONFIGURED`；合法配置下未知、缺失、错误格式或不匹配的 Bearer 请求返回 `401 UNAUTHORIZED`；比较仍使用 `secrets.compare_digest`。

### 改动文件

- `backend/app/config.py`：增加共享 Token 校验函数。
- `backend/app/execution/api.py`：角色认证改用完整校验规则。
- `backend/simulator/config.py`：复用共享规则，避免 API 与模拟器漂移。
- `backend/tests/test_role_tokens.py`：新增 79 项回归，覆盖占位符、空白/控制/非 ASCII、长度边界、相同 Token、Bearer 格式和敏感信息不回显。
- `README.md`：记录 Token 规则和安全响应。

未修改 `.env`、真实凭证、数据库数据或依赖锁文件；未调用真实模型。

### 验证结果

```text
uv run --locked pytest tests/test_role_tokens.py tests/test_m1_review.py tests/test_health.py -q
    87 passed, 1 warning in 9.05s

uv run --locked pytest -q
    132 passed, 77 skipped, 1 warning in 13.13s

uv run --locked ruff check app migrations tests simulator
    All checks passed

uv run --locked ruff format --check app migrations tests simulator
    50 files already formatted

uv lock --check
    通过
```

唯一警告仍是 LangGraph 上游 `allowed_objects` 默认值将来变化的弃用警告；本轮未屏蔽或升级依赖。

### PostgreSQL 完整回归与容器验证

随后设置项目既定的 `M0_TEST_ADMIN_URL`，在隔离测试数据库运行完整套件：

```text
M0_TEST_ADMIN_URL=<本地测试连接> uv run --locked pytest -q
    209 passed, 1 warning in 162.93s
docker compose build api simulator
    构建成功
docker compose up -d --wait api simulator
    API、模拟器 healthy，迁移退出码 0
GET /health/ready
    200，database=ok，checkpoint=ok
```

完整回归没有跳过项；前述 77 个环境门控项已在此轮执行。Worker 保持运行；本轮重建并更新的是 API 与模拟器容器。仍停在 M3。

## 2026-10-19：M4 极简工作台与浏览器验收

### 范围和实现

按用户授权进入 M4，实现工单列表/详情、诊断、证据、增量调用轨迹、审批卡片、角色权限反馈、执行结果和刷新恢复。没有加入自动备注、额外业务、生产认证或 M5 评测功能。

React/TypeScript/Vite 产物由既有 FastAPI 同源提供，根路径为工作台，API/health 路由保持独立，未知 API 路径继续 404。入口设置 no-store、CSP 和 Referrer-Policy。Docker 使用锁定 digest 的 Node 22.23.2 构建阶段；Python 运行镜像只接收静态产物，未新增常驻服务或 Python 依赖/迁移。前端精确依赖及完整性哈希记录在 package-lock.json。

只读 `/api/me` 返回服务端确认的身份；工单列表增加当前租户 active_run_id。Token 用密码框输入，只保存在页面内存；刷新后重新输入，不进入 URL、浏览器持久存储、前端环境变量或构建文件。原服务端角色校验和审批/Worker 规则继续生效。

创建请求在发出前将工单及幂等 UUID 放入 URL，响应丢失后刷新重试仍复用原键；收到 Run ID 后使用 `?run=` 恢复。审批按钮防连点，响应丢失时只重试原决定；前端不向图提交 resume。批准 202 仅提示待处理，只有服务端 SUCCEEDED/REQUEUED/CONFIRMED 才展示“已重新入队”。终态清除旧的“尚未确认”提示。

轨迹按 after_id 增量读取，满 100 条继续取，按 ID 去重；终态补拉后停止轮询。连接失败保留已有内容并提供刷新。页面分别展示拒绝、审批过期、版本冲突、技术失败与未知结果；工单 HTML 作为普通文本显示，不执行其中脚本。

### 文件

- `frontend/package.json`、`package-lock.json`、`tsconfig.json`、`vite.config.ts`、`index.html`。
- `frontend/src/main.tsx`、`api.ts`、`flow.js`、`style.css`。
- `frontend/playwright.config.ts`、`tests/flow.test.mjs`、`tests/browser/workbench.spec.ts`、`tests/browser/teardown.ts`。
- `backend/app/main.py`、`backend/app/execution/api.py`。
- `backend/tests/test_m4_web.py`、`backend/tests/ui_server.py`、`backend/tests/integration/test_m3_execution.py`。
- `backend/Dockerfile`、根 `.dockerignore`、`.gitignore`、`compose.yaml`。
- `README.md`、`TicketPilot-详细设计文档.md`、本文件。原项目开发规格未改动。

### 验收方式和结果

```text
frontend: npm run build
    TypeScript 类型检查及 Vite 生产构建通过
frontend: npm test
    3 passed：成功文案、203 条事件分页/去重、取消后的迟到响应
frontend: M0_TEST_ADMIN_URL=<本地测试连接> npm run test:e2e
    9 passed (1.0m)，真实 Microsoft Edge
backend: M0_TEST_ADMIN_URL=<本地测试连接> uv run --locked pytest -q
    213 passed, 1 warning in 164.77s；0 skipped
backend: uv run --locked ruff check app migrations tests simulator
    All checks passed
backend: uv run --locked ruff format --check app migrations tests simulator
    52 files already formatted
backend: uv lock --check
    通过，Python 原 69 个依赖锁文件未升级
docker compose config --quiet / build migrate
    退出码 0
docker compose up -d --wait api simulator / up -d worker
    API、模拟器 healthy，Worker 运行；迁移退出码 0
GET / /health/ready
    均 200，页面含 CSP
GET /api/me、/api/tickets
    两种真实本地角色均 200，角色正确；未知 Token 401
```

9 项浏览器测试包含：批准/操作员直接越权 403/重复点击/刷新恢复、拒绝、版本冲突、过期、HTML/提示词注入、创建响应丢失后的刷新重试、已有活跃任务恢复、审批响应丢失后的原决定恢复、未知结果/读取失败的展示及终态停止轮询。成功路径数据库核对为 Run=1、RESUME Job=1、RETRY_ENQUEUED=1；拒绝/过期/版本变化的业务效果为 0。额外新增后端租户隔离测试，确认列表不暴露其他租户活跃 Run ID，终态后 active_run_id 清空。

浏览器夹具创建随机独立 PostgreSQL 库，使用真实模拟器 HTTP/API/Worker，只有模型是固定协议替身；未知结果展示项明确使用浏览器响应替身，深层故障仍由原 M3 测试覆盖。以上不构成真实模型成功率或费用评测。本轮没有调用真实供应商模型。

人工 Chrome 操作也确认了操作员按钮禁用、角色切换恢复同一 Run、批准后“已重新入队”和三栏页面布局。验收截图 `artifacts/m4-workbench-approved.png` 只含合成数据。首轮发现的用例定位歧义和过期标题过于笼统已修复；截图检查发现终态仍有待处理旧提示，也已修复并补断言。

### 环境和敏感信息

开始验收时 Docker Engine 未运行，启动已安装的 Docker Desktop 后 PostgreSQL 恢复，才运行完整集成测试。镜像首次拉取和 npm 下载使用本机已有代理的进程级参数，没有写入系统或项目代理设置，没有禁用 TLS 校验。

扫描 84 个源码、构建文件、文档和报告文件，当前模型密钥、内部读写和角色 Token 匹配数为 0；镜像元数据匹配数为 0。真实 `.env` 未修改。真实 API 检查仅 GET，原验收 Run 仍为 SUCCEEDED，没有重新批准、调用模型或重置原演示业务。

测试夹具不打包进运行镜像。Windows 强制结束子进程会跳过 finally，浏览器全局 teardown 因此先请求仅测试服务器的优雅关闭，再清理自己创建的库；开发中遗留的一个无连接测试库已按精确名称核对并删除。Playwright trace/video 关闭，不收集真实凭证。唯一 Python 警告仍是既有上游 allowed_objects 弃用提示；测试进程另有 NO_COLOR/FORCE_COLOR 的无功能影响提示。

M4 验收完成后停在本阶段；M5 仍待用户另行授权。

收尾再次核对：O3001 仍为 QUEUED/v4，RETRY_ENQUEUED=1；所有 M4 临时数据库剩余数量为 0。最后补充的截图等待轨迹加载断言也已单独通过（1 passed in 24.3s）；截图中显示恢复后的 27 条事件。正式 Docker 工作台已在浏览器打开，保持未登录，真实 Token 由用户在本地临时输入。

## 2026-10-19：M4 全面审查及修复

### 审查边界

对照原规格第 8、13～15、17～19 节及 M4 验收，检查临时身份、Token 存储/传输、接口响应、状态更新、导航恢复、重复提交、审批未知结果、轨迹分页、错误展示、HTML 注入、窄屏布局、静态文件边界、Docker 与现有业务状态。本轮不进入 M5，不调用真实模型，不修改 .env，不重置演示数据。

### 已复现并修复的 7 类问题

新增 10 项浏览器问题用例，在修复前全部失败；均由独立 PostgreSQL/模拟器/API/Worker 和明确的浏览器网络替身复现。

| 级别 | 问题与证据 | 修复 |
|---|---|---|
| P2 | HTTP 2xx 的 JSON 解析失败被转成空对象；null/缺字段身份响应可使页面崩溃或显示不完整身份，创建响应损坏也可能清掉原幂等键。3 个用例复现。 | 校验 JSON 和 UI 消费的必要字段；无效成功响应报告 INVALID_RESPONSE，并保留原创建/审批请求。核对返回 Run ID 与请求路径一致。根据实际数据库契约允许待决定审批的 reason=null，避免误拒合法数据。 |
| P2 | 创建或审批响应被延迟时，用户点击导航离开后，迟到响应仍把页面跳回旧 Run，或把旧审批提示显示在工单列表。2 个用例复现。 | 为导航/身份切换维护版本标识，异步请求只更新发起时所属页面。已受理的服务端任务仍正常执行，不把离开页面当成取消。 |
| P2 | 审批已在服务端提交，但客户端得到 503 时，前端清掉原决定并重新允许反向操作。服务端会拦截冲突，但前端丢失未确认语义。 | 连接失败、408、5xx 和无效成功响应保留原决定/哈希/理由，原决定重试或正式读取确认后再解除。回归确认仅 1 个 RESUME Job 与 1 个业务效果。 |
| P2 | 选中工单后点击“刷新状态”，虽然重新 GET 列表，但详情仍引用旧对象，服务端内容更新不可见。 | 重新读取后同步当前选中工单；工单不存在时返回列表，不保留已失效对象。 |
| P2 | Run 读取成功后，业务工单详情 503 会阻断后续独立事件读取，刷新还会清空既有轨迹。 | 工单与轨迹分别处理失败；工单不可用时仍读取轨迹，同一任务刷新失败保留已加载内容，并提示可能过时。回归额外断言审计请求确实发出。 |
| P2 | 审批连接失败后，下一次成功的读取轮询会清除提交错误，尚未确认的决定失去提示。 | 区分读取错误与提交错误；只在正式决定/终态确认后清除未确认状态。错误展示在页面顶部，防止长轨迹页面把提示挤到屏幕下方。 |
| P2 | URL 在无 Run ID 的待重试请求与列表间前进/后退时，React 依赖值仍是空串，恢复读取没有触发，用户回不到原待重试请求。 | 将导航修订纳入读取生命周期，同 ID 导航也重载，并从 URL 恢复原工单/幂等 UUID。 |

未发现上述问题可以绕过服务端角色检查、正式审批哈希或 Worker 业务规则；修复重点是让浏览器准确表达结果、恢复请求和隔离异步状态，而非用客户端替代服务端控制。

### 改动文件

- `frontend/src/api.ts`：响应校验、提交未确认分类、nullable 审批理由。
- `frontend/src/main.tsx`：导航版本保护、读取/提交错误分离、详情刷新、轨迹独立读取与保留。
- `frontend/tests/browser/review.spec.ts`：新增 13 项浏览器回归/检查。
- `frontend/tests/browser/workbench.spec.ts`：工单选择使用完整编号边界，避免扩充合成夹具后前缀匹配歧义。
- `frontend/tsconfig.json`：将测试与配置文件纳入类型检查。
- `backend/tests/ui_server.py`：合成案例扩至 24 个，保持独立数据库和模型替身。
- `backend/tests/test_m4_web.py`：补静态路径隔离断言，凭证文件/测试源码/路径穿越均 404。
- `README.md`、`TicketPilot-详细设计文档.md`、本文件：同步审查结论与恢复语义。

没有新增运行时组件、数据库迁移或依赖；Python/前端锁文件均未升级。

### 完整验证

```text
frontend: npm run build
    TypeScript（含源码/测试/配置）检查与 Vite 构建通过
frontend: npm test
    3 passed
frontend: M0_TEST_ADMIN_URL=<本地测试连接> npm run test:e2e
    22 passed (1.6m)
backend: M0_TEST_ADMIN_URL=<本地测试连接> uv run --locked pytest -q
    213 passed, 1 warning in 150.73s；0 skipped
backend: uv run --locked ruff check app migrations tests simulator
    All checks passed
backend: uv run --locked ruff format --check app migrations tests simulator
    52 files already formatted
backend: uv lock --check
    通过
frontend: npm audit --json
    当前审计报告 vulnerabilities.total=0
docker compose config --quiet / build migrate
    退出码 0，按既有锁文件构建成功
```

新增的 13 项包含 10 个问题回归，以及鉴权重定向不转发 Token、跨 Run 响应拒绝、203 条事件续取/去重及 390px 窄屏检查；原 9 项批准、拒绝、越权、重复点击、刷新恢复、过期、版本变化、注入和未知结果场景全部继续通过。浏览器响应替身仅模拟协议/网络故障，模型仍用固定替身；不计为真实模型评测。

### 保留边界

只有本机演示身份，Token 保存在页面内存；用户导航不会取消服务端已接受的任务。读取错误后保留旧内容并要求刷新，客户端时间也不替代服务端有效期检查。模拟器没有工单创建时间字段，页面没有虚构该数据；本轮未为装饰字段增加迁移。

既有上游 allowed_objects 弃用警告仍保留；npm 审计结果只描述当前依赖报告，不是绝对安全承诺。没有升级或全局屏蔽警告，仍停在 M4。

## 2026-10-23：接续 M5 评测与交付，真实模型仍受阻

### 接续范围

根据旧对话已授权的 M5 计划及用户本次“继续按对话中设计文档推进 M5”，保留 16 条案例（4 dev / 12 test），完成确定性基线、恢复/浏览器回归、评测审查和材料。原项目开发文档没有改动，案例标签和模型提示词没有修改，不进入其他业务或下一阶段。

修复旧会话中断留下的测试语法错误；对指标补齐明确标签分母、诊断覆盖率、已观察诊断的条件准确率，自动完成分母保留全部符合条件的案例。重复效果按批准写意图组计数并保留零效果拒绝组。模型调用仅记录脱敏 HTTP 状态码，增加批次错误停止、无网络重评分、报告不覆盖验证和全路径范围检查。具体发现与证据见 `docs/M5-审查记录.md`。

### 文件与产物

- `backend/evaluation/{run,metrics,baseline,rescore,simulator}.py`、冻结的 `cases.json`。
- `backend/app/agent/{llm,graph}.py`：延续并验证旧会话新增的安全 HTTP 状态码记录；未改变模型提示词。
- `backend/tests/test_m5_evaluation.py`、`tests/integration/test_m5_evaluation.py`、`tests/test_m2_protocol.py`。
- `frontend/scripts/record-demo.mjs`、`scripts/package_m5.py`。
- `README.md`、`TicketPilot-详细设计文档.md`、`docs/M5-评测报告.md`、`M5-演示与复现.md`、`M5-审查记录.md`、`简历条目.md`、`architecture.svg`/`.mmd`、`compose.reproduce.yaml`。
- `artifacts/`：新固定基线、原 dev 重评分、402 探测、完整回归 XML、浏览器和 Docker 日志、测量时源码 ZIP、三分钟 MP4、交付 ZIP 及图片证据。旧报告不覆盖。

### 实际运行

```text
Docker Desktop 启动，Engine 29.8.2；原项目 PostgreSQL 恢复 healthy。
uv run --locked python -m app.experiments.llm_smoke --kind text
    单次请求，HTTP 402，1329 ms；停止后续真实模型评测。
M0_TEST_ADMIN_URL=<本机合成测试账户> uv run --locked pytest -q --junitxml=...
    232 passed，0 skipped，1 warning，175.55 s。
uv run --locked pytest tests/test_m5_evaluation.py -q --junitxml=...
    追加审查 15 passed，2.40 s；14 项与完整回归重叠，另 1 项为批次停止/不覆盖测试。
uv run --locked python -m evaluation.run --baseline fixed --split all --output <新路径>
    dev 4/4，test 12/12；零模型调用，全部使用隔离库。
uv run --locked python -m evaluation.rescore --input <原 dev> --output <新路径>
    原 dev 4 次 API 失败仍为 0/4；覆盖 0/4、条件准确率不可算、用量和费用未知。
frontend: npm run build / npm test
    TypeScript、Vite 构建通过；3 passed。
frontend: node node_modules/@playwright/test/cli.js test --reporter=list
    Edge 22 passed，约 1.5 min。
uv run --locked ruff check/format --check app migrations tests simulator evaluation
    通过；uv lock --check 通过，仍解析 69 个锁定包。
docker compose config --quiet / build migrate / up api simulator worker
    构建成功，API/模拟器 healthy，Worker 运行，/health/ready=200。
```

全新 `ticketpilot-m5-repro` Compose 项目创建独立卷，验证空库迁移、两个 seed（reset=false）、7 条合成工单和相同 thread_id 的两个独立 recovery 容器。暂停 interrupt_count=1，恢复为 0 且 pending_nodes=[]。本次新建项目和卷验收后已清理；没有声称另一台物理机器已实测。

三分钟录像为实际隔离浏览器执行：批准后 Run=1/RESUME Job=1/业务效果=1；拒绝和版本冲突各效果=0。固定模型替身持续标明，字幕不是产品功能，末 30 秒为测量结果卡。实际恢复/幂等进程故障由后端回归验证，不把视频版本冲突说成崩溃恢复。

### 环境、敏感信息与未完成项

Playwright 录像需要其配套 FFmpeg：本机原有 FFmpeg 负责最终转码，额外下载约 1.6 MiB 的 Playwright FFmpeg 到用户缓存。安装器同时自动清理了旧的 chromium-1208/headless_shell-1208 缓存，未改动项目锁文件或系统设置。当前浏览器验收使用已安装 Edge。首个带 reporter 的 npm 包装器命令因参数解析失败，改用项目内锁定的 Playwright CLI 后完整通过；失败日志保留。

扫描本次 114 个文本源码/构建/文档/报告文件，配置中的真实模型、内部和角色密钥匹配数为 0；不输出密钥值。交付包排除 `.env`、运行环境与数据库文件，并逐文件计算 hash。临时评测、测试和录像库在正常结束后核对已清理，原 O2001/O3001 均保持 QUEUED/v4。未修改 `.env`，未初始化 Git、配置身份、提交或推送。

真实模型当前仍为 HTTP 402；冻结 test 未执行，usage 和费用未知，M5 不能标记为全量验收完成。恢复供应商服务后按 README 先 dev 再冻结 test，使用新路径保存每次尝试，再补最终真实模型对比与演示。不能用固定流程 16/16、固定模型测试或旧 M0/M2 的成功调用代替此门禁。

## 2026-10-23：修复 M5 全面审查发现的四类问题

用户明确授权修复审查问题。实现限制在评测代码、回归测试和交付文档，不修改原项目开发规格、冻结案例、模型提示词、正式业务实现或 `.env`；没有真实模型调用、Git 提交或推送。

### 改动

- `backend/evaluation/metrics.py`：评分版本 m5-v3，核对预期终止原因、服务端接受的有效诊断及完整业务安全审计。未取得新安全证据时为未知，不冒充已通过。
- `backend/evaluation/run.py`：原始结构化诊断的证据检查；计量复用 ReadTools.validate 和可信绑定；监测 Worker 读写路径；每例全库前后快照；已批准的哈希/资源/版本/期限复核；模型逐请求保存和中断状态。
- 新 `backend/evaluation/safety.py`：核对隔离库所有租户订单、工单、履约事件和幂等结果，仅允许本例获批的精确变更；识别跨资源效果及重复组。版本冲突的夹具变化只豁免该具体字段。
- 新 `backend/evaluation/reporting.py`：请求前保存意图、返回后更新 usage，fsync 后原子替换；未知用量、效果和费用保留 null。
- `backend/evaluation/rescore.py`：旧数据缺少全库审计时标记 revalidation_required；保留原始用量/耗时/source hash，不能离线补造安全结果。
- `backend/tests/integration/test_m5_review.py`：将原审查的 5 个失败复现迁入正式测试；中断实验同时验证请求发出前磁盘已有 IN_FLIGHT 意图。
- `backend/tests/test_m5_evaluation.py`：增加非法终止原因、旧证据覆盖不足、原子替换失败和实际子进程 os._exit(91) 后保留计量的回归。
- README、详细设计、评测/复现/审查文档和简历条目更新；`docs/M5-修复验证.md` 记录逐项关闭证据；`scripts/package_m5.py` 默认写新修复包，旧 ZIP 不覆盖。

### 实测

```text
原审查 5 个负向反例 + 原 M5 单元：20 passed，27.27 s。
完整 PostgreSQL 回归：244 passed，0 skipped，1 warning，204.56 s。
固定流程 m5-v3：dev 4/4、test 12/12；16/16 全库审计已执行。
固定流程 v3 离线重算：summary 与测量完全一致。
原真实 dev v3 重算：原 usage 和耗时不变；失败仍为 0/4，安全率因旧证据不足为未知。
Ruff check/format：通过，64 个文件格式符合要求。
uv lock --check：通过，依赖仍为 69 个锁定包。
```

原伪造证据和非法写工具导致的转人工现在不能获得任务成功；见证订单被故障注入更改时产生未授权/完整性违规；无绑定时猜中夹具订单仍是无效工具请求。模拟第二次调用中断，报告保存两次意图、首次 20 输入/5 输出 token 与第二次未知用量。独立测试子进程第二次调用中强制退出也保留相同保守记录；所有模型均为测试替身。

当前 v3 测量源码 hash 为 0377d02630fa9997bfd11ac0587f6931d4dd1e9ed8eedb933744882d915b135c；测量时源码另存新 ZIP。原报告、原审查失败日志、旧源码快照和旧交付包均保留。前端、正式 API/Worker 未变，本轮不重复浏览器验收、录像或镜像构建。全量回归包含原有可靠执行与跨进程恢复测试。

本轮仅使用现有项目 PostgreSQL 和随机隔离测试库，未启动已停的正式 API/Worker。HTTP 402 继续作为既有探测证据，本轮没有重新请求供应商；真实 dev/test 最终验收与费用门禁仍未关闭。四项代码审查缺陷的修复完成，不据此宣称整个 M5 已全部验收。

## 2026-10-23 全项目审查后的四项修复

按两份设计文档审核全项目后确认的 F1～F4 已修复。另一 Run 的历史 CONFIRMED/UNKNOWN 不覆盖本轮拒绝终态，历史操作单独关联 origin_run_id；本 Run 的未知请求与过期后历史成功恢复仍按原逻辑查证。列表改为最多 25 条摘要及 after_id 游标，正文从详情读取，前端分页及跨页 URL 重试同步更新。M0 探测按请求保存本进程 journal，后续失败输出此前已知 usage 和失败调用未知用量。ReadTools 每次尝试增加独立 started_at/duration_ms，退避不算作单次请求耗时。

完整后端 PostgreSQL 回归 250 passed、0 skipped，226.66 秒；新增 6 项正式后端回归包含在总数内。原全项目审查探针复制到新证据目录后 4/4 通过，原始失败证据未覆盖。前端构建及 3 项单测通过，Edge 完整回归 25 passed。首次浏览器 24 passed、1 failed：旧脚本在异步详情加载时通过 evaluate 强制点击禁用按钮；加 toBeEnabled 后完整重跑通过，原失败日志保留。最后放宽游标比较以兼容数据库排序后，构建及 3 项分页浏览器专项再次通过。

固定基线以当前源码重新运行 dev 4/4、test 12/12，并离线复算 summary 一致。当前 backend source SHA-256 为 015decfb9e48a19c2db4933b26d74705236eaef790e727b5123c646574998d42；案例与提示词 hash 未改。70 文件测量源码快照逐项及指纹验证通过。Ruff check/format 通过，65 个正式 Python 文件格式符合，uv 锁仍为 69 包。

最终源码的 Docker 正式镜像构建成功；没有启动真实业务 Worker，没有发起付费模型请求。临时测试库/评测库/UI 库剩余 0，原 O2001/O3001 保持 QUEUED/v4。README、详细设计第 25 节及 docs/全项目审查-修复验证.md 记录接口兼容性、前后证据和实际结果。旧测量/源码/交付包保留，新交付包单独生成；M5 真实模型、费用与异机门禁继续待验收。
