# Harness看板（HarnessKanban）实施进度与交付记录

| 项目 | 内容 |
| --- | --- |
| 记录日期 | 2026-10-15 |
| 依据 | [PLAN.md](./PLAN.md) T01–T38、[DEVELOP.md](./DEVELOP.md) D01–D21、[CONTRACT.md](./CONTRACT.md) |
| 可复用提示词 | [README.md](../README.md) §一「给 AI Agent 的任务执行提示词」 |
| 环境（实测） | Python 3.12.11、Node 26.4.0、pnpm 11.1.2、MySQL 9.7.1（127.0.0.1:3306）、Redis、nginx 1.31.6 |
| 解析依赖版本 | Django 5.2.7、DRF 3.18.3、Celery 5.6.3、FastAPI 0.143.1、redis-py 6.4.0、PyMySQL 2.2.8 |
| 前端解析版本 | Vue 3.5.44、Vite 8.3.4、TypeScript 5.9.3、Vitest 5.0.4、Playwright 1.64.1、Pinia 3.0.4、vue-router 4.6.4 |

本记录只列**实际运行过**的命令与结果。未运行或未覆盖的部分明确标注，不作为已完成证据。

A01–A30 与 R01–R13 的逐项覆盖、证据命令与主线程签收结论见 [ACCEPTANCE-MATRIX.md](./ACCEPTANCE-MATRIX.md)。

## 1 实际运行的验收命令与结果

| 命令 | 结果 |
| --- | --- |
| `python manage.py check` | 无问题（0 silenced） |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` |
| `python manage.py migrate`（SQLite 空库） | 全部应用，`tasks_claim_lock` 恰一行 `(id=1, revision=0)` |
| `DB_ENGINE=mysql python manage.py migrate`（MySQL 空库） | 全部应用，锁行恰一行（MySQL 9.7.1） |
| `python -m pytest -q -m "not mysql"`（SQLite） | **387 passed, 13 deselected, 0 failed** |
| `DB_ENGINE=mysql python -m pytest -q -m "mysql"` | **13 passed**（真实 MySQL 跨连接竞争、身份写权限竞态、部署与锁行、派发防争用） |
| `DB_ENGINE=mysql python tests/manual/t19_cross_instance_claim.py` | **全部通过**（两个真实 AgentAPI 进程 + MySQL） |
| `DB_ENGINE=mysql python tests/manual/t38_capacity.py` | **全部达标**（100 Agent / 30 项目 / 99,990 任务） |
| `DB_ENGINE=mysql python tests/manual/t36_backup_restore.py` | **全部通过**（数据库与文件成对备份/隔离恢复/一致性校验） |
| `DB_ENGINE=mysql python tests/manual/t37_e2e_acceptance.py` | **全部通过**（Web 与 Agent 两条 API 串成单一主线 14 步，逐步断言数据库） |
| `DB_ENGINE=mysql python tests/manual/t35_proxy_acceptance.py` | **全部通过**（真实 nginx 1.31.6 HTTPS 代理，含 `underscores_in_headers` A/B 对照） |
| `DB_ENGINE=mysql python tests/manual/t39_pi_agent_flow.py` | **全部通过**（真实 `pi` Agent 进程 + 一段提示词，非 mock：查任务 → 领取 → 产出 → 上传 → 提交 → 主管审核闭环；提示词取自 `README.md` 的 `AGENT-PROMPT` 段，文档与验收同源） |
| `pnpm exec playwright test tests/e2e/cross_api.spec.ts` | **通过**（浏览器操作与远程 Agent 请求在同一脚本内交替驱动同一任务） |
| `pnpm exec vitest run` | **14 files / 170 tests passed** |
| `pnpm exec playwright test` | **31 passed**（chromium：五列看板、代提交/审核/返工/回收、Agent 与项目/成员/文档页、看板总览与活动页，以及 T37 浏览器与 Agent 交替驱动同一任务的联合用例；全部连真实后端，逐例断言控制台零报错） |
| `pnpm exec vue-tsc --noEmit` / `pnpm build` | 0 类型错误 / 构建成功 |

## 2 真实环境验证的关键场景

| 场景 | 证据与结果 |
| --- | --- |
| 两个真实 API 进程 + 20 并发争抢同一任务（T19 场景 1） | 200=1、409=19、其他=0；1 个执行、1 个容量槽、执行带固定截止时刻 |
| 持锁连接未提交时的领取（T19 场景 2） | 等待 **5.7 秒**后返回 503 `CLAIM_LOCK_TIMEOUT`（driver_code 1205），持锁期间任务保持 TODO，未降级为无锁领取 |
| 持锁连接释放后的领取（T19 场景 3） | 返回 200，任务转进行中 |
| 两个真实连接同时结束同一执行（T04） | 仅一方生效，容量只减一次 |
| 单进程内 20 并发领取（T18，pytest） | 恰好 1 次成功、19 次冲突；1 执行、1 槽、1 事件 |
| 两名主管并发批准/驳回（T20–T22） | 恰好一个决定生效，另一条为版本冲突 |
| Redis 不可用时的派发（T26） | 业务事件保留、次数与 `next_retry_at` 记录、连续失败升级为 `NEEDS_ATTENTION` 且不丢弃事件 |
| 消费后失败回写（T26） | 已处理事件不被覆盖回失败状态 |

运行 T19 脚本需要真实网络栈：本地沙箱会拦截回环 TCP（表现为任意端口 connect 成功、HTTP 返回 502），因此该脚本必须在非沙箱环境执行，且默认端口为 `T19_PORTS`（默认 19121/19122）。

## 3 实施期间发现并修复的缺陷（均由真实测试暴露）

1. **锁等待被内部重试放大**：`CLAIM_LOCK_WAIT_SECONDS=5` 未接入 MySQL（服务端默认 50s），且锁等待超时被当作可重试错误在进程内重试 4 次，实测等待 22.4 秒。现按 D05 修正：仅死锁与 SQLite busy 进程内重试，锁等待超时立即返回 503；同时通过 `init_command` 把 `innodb_lock_wait_timeout` 设为配置值，实测等待降到 5.3–5.7 秒。
2. **Outbox 脱敏按精确键名匹配**：`agent_key`、`api_token`、`result_content` 等字段名不会被过滤，会把密钥写进派发事件。现改为敏感片段匹配（key/token/secret/password/credential/content/authorization）。
3. **SQLite 上缺少事务边界不被发现**：测试助手直接调用领域服务时，SQLite 静默忽略 `SELECT FOR UPDATE` 而 MySQL 正确报错；已改为所有调用统一经过最外层事务包装。
4. **Django 无法在中间件捕获视图异常**：参数校验阶段抛出的领域错误会变成通用 500。现由 `BaseAPIView.dispatch` 统一转换，响应始终为契约信封且不含堆栈、SQL 或路径。
5. **`__length` 查找在 Django 5.2 不可用**：长度约束改用 `Length` + 比较 lookup，SQLite/MySQL 均生成 `LENGTH/CHAR_LENGTH` 断言。
6. **非法 Host 绕过契约信封**：`RequestBoundaryMiddleware` 调用 `request.get_host()` 会先抛 `DisallowedHost`（非信封 400），且 `CommonMiddleware` 在其之前运行也会先抛同一异常。现改为自行校验原始 Host 头，并把边界中间件置于 `CommonMiddleware` 之前，任何非法 Host 均返回 403 `REQUEST_ORIGIN_FORBIDDEN` 信封。
7. **无请求体的 DELETE 被误拒**：边界中间件要求所有非安全方法声明 JSON 类型，导致移除成员/文档的无体 DELETE 返回误导性的 403。现仅在请求确实带体时要求类型。
8. **405 与未匹配路径不走路由信封**：方法不允许与未知路径分别返回 Django 默认响应。现由 `BaseAPIView.http_method_not_allowed` 与 `config/urls.handler404` 统一返回信封。
9. **Web 代提交必然失败**（由 T31/T32 浏览器用例暴露）：`submit_result` 在 `web_operator` 来源时把 `submitted_by_agent` 传成 `None`，而该列 NOT NULL；随后 `IntegrityError` 被统一映射为 `SUBMISSION_EXISTS`，于是任何 Web 代提交都返回误导性的 409。现提交者固定记录为执行的 Agent（代操作来源记在活动事件的 `actor_type` 上），并把提交创建包在 savepoint 中，只有在确实存在同一执行的提交时才返回 `SUBMISSION_EXISTS`。
10. **卡片附件数量把同一个文件数了两次**：提交会把产物同时挂到执行与提交两条引用上，而 `attachment_count` 统计的是引用行数，导致一个文件显示为 2 个。现按去重后的文件数统计。
11. **新建项目的初始上下文版本出现编号缺口**：`record_context_version_locked` 语义是"追加"（会自增 `content_version`），用于创建时会产出 `content_version=2` 且缺少 1 号版本。现创建时显式写入 1 号快照并复用共享的文档引用助手。
12. **并发派发器会重复发布同一事件**（由 `-m "mysql and concurrency and jobs"` 暴露）：派发预占是 `processed_at IS NULL` 的批量更新，获胜方提交后该条件仍成立，因此两个派发器都能预占并各自发布一次（实测 `[1, 1]`）。现改为逐事件条件预占并过滤租期，后到者匹配不到行。
13. **Agent 读取项目列表在 MySQL 上必然失败**（由在 MySQL 上补跑复合选择器暴露）：读取路径复用了写路径的行锁重读（`SELECT ... FOR UPDATE`），而读取路径没有事务——SQLite 静默忽略行锁，MySQL 抛 `TransactionManagementError`，`GET /api/agent/v1/projects` 与 `/projects/{id}` 会 500。现拆分为写路径 `load_actor_agent_locked` 与读路径 `load_actor_agent_for_read`（同样的 enabled 与 Key 版本校验，不取行锁）。

## 4 已实现并通过测试的能力

- **T01 契约冻结**：`docs/CONTRACT.md` + `backend/domain/contracts.py`。
- **T02 工程与运行**：`backend/`（Django + FastAPI + Celery + 启动期配置校验）、`frontend/`、`deploy/`。
- **T03 模型与迁移**：16 个模型、跨库 CheckConstraint、`TaskClaimLock.id=1` 数据迁移、两库空库迁移验证。
- **T04 事务/时钟/锁原语**：最外层事务与有界重试、数据库时钟、全局锁行、锁计划分层加锁、两个 CAS 原语。
- **T05/T06/T09**：统一信封与错误映射、Host/Origin/内容类型边界、`AGENT_TOKEN` 原名解析、持久幂等回执（含敏感回执去明文）、统一可见性与当前执行写 guard。
- **T07/T08**（子任务交付，59 个测试）：Agent 创建/Key 生命周期/基本属性、全局角色与启用变更、项目授权、最后主管保护（含真实 MySQL 双主管并发撤权）、WAIT/NOW 两种处置。
- **T13/T14/T15**（子任务交付，主线程已复核）：任务编制与状态机、指派与领取资格、`read_task` 只读列表与等级解析。
- **T23**（主线程实现并验收，9 个测试）：任务归档/恢复作为独立标记（不改五态、不删证据），双轮返工历史在 detail 与 activity 中完整可读，员工无法经历史入口读取准备中任务，归档编号不复用。
- **T24**（主线程实现并验收，14 个测试，含真实 MySQL 并发）：列内排序以服务端解析出的真实邻接关系计算中点值，过滤视图传入的非相邻锚点被拒绝，跨列锚点与状态断言被拒绝，间距耗尽时按列重编号；并发两客户端同版本排序只有一方成功。
- **T34**（主线程实现并验收，15 个测试；`-m "fault and jobs"` 21 passed、`-m "api and deployment"` 7 passed）：结构化请求链日志（`request_id`、路由模板、状态、错误码、耗时、任务/执行/项目/Agent ID，脱敏后不落 Key 与私有路径）；锁等待超时与重试耗尽在事务回滚后写入**独立短事务**的 system 失败记录，尽力而为且不掩盖原始错误；管理入口 `GET /ops/failures`、`POST /ops/outbox/{id}/retry`、`POST /ops/capacity-audit`；重派保留原事件行与尝试次数，已消费事件拒绝复活；失败清单对 Key 与私有路径做二次脱敏。公开 `live` 只返回存活；`ops` 路径仅存在于回环 WebAPI，AgentAPI 的 OpenAPI 中不存在。
- **T29/T30/T33**（子任务交付，主线程复核，91 个前端单测 + 16 个真实浏览器用例）：Agent 管理（一次性 Key 交付、重放不再交付、WAIT/NOW 处置、有效执行与占槽分列）、项目与成员/背景/文档（背景编辑独立递增 `content_version`、同名上传显式选择新版本或新文档、归档只读）、看板总览与活动页（按服务端 payload 原样展示、范围与 `as_of` 标注、逾期占槽与「等待回收」并列、例外可跳转到对应看板卡片）。
- **T31/T32**（子任务交付，主线程复核并修正，31 个前端单测 + 9 个真实浏览器用例）：五列看板（固定顺序、列计数、卡片字段区分 `due_at` 与执行截止）、筛选保留、真实拖拽调用排序端点、非法跨列拖拽被拒并回退服务端状态、代领/提交/审核/驳回返工/回收表单。业务流全部连真实 WebAPI，无业务 Mock。
- **T25**（子任务交付，主线程复核通过，14 个测试 + 变异校验）：`GET /dashboard` 两套 API 与 `GET /activity`。计数来自单条条件聚合，权限范围在聚合之前确定；员工 `PREPARING=0` 且标记 `PUBLISHED_ONLY`，空范围完成率为 `null` 而非 100%，超时未回收既进异常列表也不计为工作中但仍占槽。已与 `frontend/src/api/types.ts` 的 `DashboardDTO` 字段对齐。
- **项目编制端点（主线程补齐，12 个测试）**：`GET/POST /projects`、`GET/PATCH /projects/{id}`，背景编辑经上下文版本服务递增 `content_version` 并追加不可覆盖快照。
- **T10/T11/T12**（子任务交付，91 个测试全通过）：项目生命周期、受控文件（白名单+内容嗅探+服务端存储键+用途授权+上传与绑定双截止校验）、不可覆盖的上下文版本与领取快照。
- **T16/T17/T18**：五级固定超时、进度不延时、等于截止拒绝、释放/回收/超时扫描单次生效、单例锁内原子领取与补偿回收。
- **T20–T22**：不可覆盖提交、单次容量释放、并发审核唯一决定、驳回独占指定原执行者、两轮返工历史完整、DONE 重开。
- **T26/T27**：Outbox 派发/消费状态机与恢复、临时文件清理（仅超龄无引用）、容量巡检与有修复证据的纠正。
- **T28 前端基础**：请求协调、同键重试、乱序响应门、可见时轮询、无 localStorage 写入、键盘可用弹窗。

## 5 尚未完成或尚未验证（不得视为通过）

**无未完成的交付任务。** T01–T38 全部实现，并且每一项都有实际运行过的命令作为证据。

仍存在的**验证边界**（不是未完成的任务，而是未被证据覆盖的范围）记录如下：

| 边界 | 说明 |
| --- | --- |
| 代理验收的证书与入口 | T35 使用自签名证书与 `127.0.0.1` 入口；未验证公网 CA 证书、真实 DNS 与外部网络路径 |
| 备份演练的环境范围 | T36 覆盖单实例 MySQL 与本地文件目录；未覆盖跨机共享存储、增量备份与更长保留期 |
| 并发压测客户端 | T38 的延迟为顺序采样（含全局锁串行路径），未使用多机并发客户端产生排队压力 |

## 6 未运行检查及原因

- **真实 HTTPS 代理链路**：已在真实 nginx（1.31.6，`brew install nginx`）上完成验收，覆盖 `underscores_in_headers on` 的透传与其关闭时的失败对照。未覆盖的部分：使用自签名证书与 `127.0.0.1` 入口，未验证公网 CA 与真实 DNS 解析。
- **性能目标（P95 < 500ms / 看板 < 1s）**：已在目标规模实测并通过（`tests/manual/t38_capacity.py`，nearest-rank P95，warm-up 后采样，原始样本不落库但脚本可重跑）：

| 路径 | 类别 | n | median | P95 | 目标 |
| --- | --- | --- | --- | --- | --- |
| `GET /tasks`（page_size=100） | 查询 | 100 | 21.2ms | 25.2ms | < 500ms |
| `GET /dashboard` | 查询 | 100 | 29.9ms | 36.2ms | < 1s |
| `GET /tasks?project_id` | 查询 | 100 | 13.4ms | 14.7ms | < 500ms |
| `POST /get_task/`（全局锁串行） | 业务写入 | 100 | 21.2ms | 24.0ms | < 500ms |
| `POST /submit` | 业务写入 | 30 | 7.0ms | 8.4ms | < 500ms |
| `POST /review` | 业务写入 | 30 | 5.4ms | 5.7ms | < 500ms |

数据规模 100,000 任务中写入 99,990 行（30 项目 × 3,333，采样受整除以项目取整）；MySQL 9.7.1 单实例、无并发压测客户端（延迟为顺序采样）。压力下复核通过：五态合计等于任务总数、无「进行中没有当前执行/执行者」、Agent 占槽数与真实未结束执行数一致、无同一执行的多份提交、无同一提交的多次审核。首次未预热运行时同一查询 P95 为 410ms（冷缓冲池），脚本现已显式预热并把预热结果排除在样本之外。
- **备份恢复演练**：已完成（`tests/manual/t36_backup_restore.py`）：mysqldump + 私有文件目录成对备份，恢复到隔离数据库与文件目录后校验任务/提交/审核关系、文件校验和一致，并确认恢复没有重新计时固定截止时刻。演练只覆盖单实例 MySQL 与本地文件目录，未覆盖跨机共享存储与增量备份。
- **故障注入的其余分支**：持锁进程退出、Redis 不可用、超时重复回收分别由 T19 场景 2/3、T26 派发失败用例、T16/T17 超时用例覆盖（均为真实进程或真实连接）；未单独演练"停机后重启并扫描全部未处理事件"的整机流程。

## 7 需要主线程继续处理的事项

1. **`PATCH /projects/{id}` 已补齐**（主线程实现并测试）：背景编辑走 `update_project_background` 递增 `content_version` 并追加上下文版本；标题/简介走 `update_project_metadata`；两者不可在同一次请求混用，以保证单次回执。
2. **尚未实现的端点**：T13–T25 范围内已全部注册（含 `GET /dashboard` 两套 API、`POST /projects/{id}/task-order`）。剩余缺口只在前端业务页面（T29/T30/T33/T34）与部署/故障/性能门禁（T35–T38）。
3. **活动事件命名不统一**：`task_service`/`claim_service` 记录 `archived`、`claimed`，而 `submission_service`/`review_service` 记录 `task.submitted`、`task.rejected`。历史查询与后续前端（T33）需要按名字映射文案，建议在 T33 前一并统一并在契约中固定。
4. `webapi/routes/agents.py` 的 `EnvelopedAPIView` 是 `BaseAPIView` 的兼容包装（DRF Response 终结化），在当前信封返回 `JsonResponse` 后已是无效的兜底分支，可在后续清理时删除。

## 8 子任务报告的契约判定（主线程已接受）

- 项目生命周期为 Web 专属操作（product 2.3）；非 Web 来源返回 403 `ROLE_FORBIDDEN`。
- 文档与附件变更同时递增 `project.version`（不只是 `content_version`），使并发的背景 PATCH 以过期版本拿到 409。
- AgentAPI 侧 multipart 由受控解析器处理（`python-multipart` 未安装也未在 `pyproject.toml` 声明）；若后续希望使用 FastAPI 原生解析，只需增加该依赖，无需改代码。
- 幂等重放上传会保留一份未绑定的暂存 `FileAsset`（不产生假附件），由 T27 清理收集。
- 文本上传若包含 HTML/SVG/script 标记一律拒绝（含 Markdown 代码块内），取"拒绝 HTML/SVG 内容"的严格解读。
- 权限码细节：文件服务先判成员（非成员恒 404），背景编辑用共享 `require_project_supervisor` 先判角色（员工 403）；两者都不泄露对象存在性。

## 9 前端视觉重构（2026-10-15 第二轮）

用户反馈「现有前端页面不符合设计要求，尤其是任务看板需要类 Trello 的可拖拽形态」，据此对全部前端页面做了一轮视觉与版式重构。功能、文案、`data-testid` 与后端契约均未改变。

### 9.1 改了什么

| 范围 | 内容 |
| --- | --- |
| 设计规范 | 新增 [frontend/UI-DESIGN.md](../frontend/UI-DESIGN.md)：把产品设计文档 §3/§6/§9 的界面要求落成可执行的令牌、共享类与硬约束 |
| 设计令牌 | 新增 `frontend/src/styles/tokens.css`（颜色/间距/圆角/阴影/字号/五态与五难度配色）与 `base.css`、`components.css`（按钮、面板、徽标、表格、表单、页头、统计块、状态块等共享类）；`styles.css` 改为三层入口 |
| 应用外壳 | `App.vue` 改为左侧深色导航（概览/管理/审计三组）+ 置顶栏，窄屏折叠为顶部横向导航（五个入口仍可见可点） |
| 任务看板 | `TaskCard/TaskColumn/TaskFiltersBar/ProjectBoardView/TasksView` 重做：五列等高等宽、列头状态色点与计数、卡片分层（编号 + 难度徽标 → 标题 → 徽标 → 执行者 → 截止/附件 chips → 动作条）、拖拽落点指示线与整列高亮 |
| 其余页面 | 全局看板、活动、Agent 管理、项目（含背景/文档/成员/生命周期）与任务详情弹窗、九个业务表单统一到同一套令牌与共享类 |
| 顺带修正 | ① 进度表单 `percent` 绑定数字后调用 `.trim()` 导致控制台报错（既有缺陷）；② 领取表单里的 `**不支持续期**` 字面 Markdown；③ 项目成员在线状态、看板项目分布生命周期直接显示服务端枚举；④ `tests/e2e/cross_api.spec.ts` 两处既有类型错误（`pnpm build` 因此失败） |

### 9.2 环境变更（需要知悉）

新增 `backend/.env`，把本地运行的数据库从 SQLite 切到 MySQL（`DB_ENGINE=mysql`，库 `harvesstask`，root + 空密码）。原因：`frontend/tests/e2e` 的播种助手按 `DB_ENGINE=mysql MYSQL_DATABASE=harvesstask` 直连 ORM，若应用跑在 SQLite，用例会因「execution not found」失败（3 个用例受影响）。切换后应用与验收指向同一个库。原来的 SQLite 文件 `backend/harvesstask.sqlite3` 未删除，改回只需删掉该 `.env`。

### 9.3 实际运行的验证

| 命令 | 结果 |
| --- | --- |
| `pnpm exec vue-tsc --noEmit` | 0 类型错误 |
| `pnpm exec vitest run` | **14 files / 170 tests passed** |
| `pnpm exec playwright test`（真实 MySQL + WebAPI + AgentAPI + 前端） | **31 passed** |
| `pnpm build` | 构建成功（`✓ built in 267ms`） |
| 内置浏览器逐页巡检（真实数据，SPA 导航 + DOM 事件采集） | 全局看板 / Agent 管理与详情 / 项目列表与详情（页签切换）/ 任务看板 / 跨项目任务列表 / 活动：**console.error、window error、unhandledrejection 全程为 0 条** |
| 内置浏览器交互实测 | 卡片按钮列内排序（真实调用排序端点并回报「已在「待办」列内调整 TASK-0002 的顺序」）、跨列拖拽弹出代领表单、非法转换（待办→已完成）被拒绝并回退、合法直连动作（准备中→待办）发布成功、详情弹窗分区完整、ESC 关闭弹窗、难度筛选收敛到 1 项 |

内置浏览器的已知环境限制：本会话 IAB 面板的指针输入在若干次操作后不再送达页面（`locator.click`、`cua.click` 超时或无效，截图表面准备也会超时）；可用的替代路径是 `dom_cua`（DOM 节点点击，用于导航与按钮）、`cua.drag`（坐标拖拽，拖拽表单用例即由此完成）与页面内派发标准 HTML5 拖放事件（与仓库 e2e 助手同法）。真实指针路径由 Playwright 用例覆盖（31 个用例均为真实 Chromium 指针事件）。

### 9.4 演示数据

`tmp/seed_demo.py`（本地开发脚本，不属于交付代码）通过真实 WebAPI 播种演示项目「HarnessTask 演示项目」：6 个演示 Agent、1 个进行中项目、五态任务各 1–3 张，用于人工巡检看板。脚本可重复执行（同名对象复用）。

## 10 删除、批量删除与项目内任务管理（2026-10-16 第三轮）

用户要求：项目与 Agent 支持「删除 / 批量删除」；项目详情页要能管理该项目名下的全部任务（增删改查）。

### 10.1 设计取舍：删除 = 软删除

产品设计 11.1 明确「Agent、项目与任务首期采用禁用或归档，拒绝级联删除执行、提交和审核记录」，
执行/提交/审核是审计证据，物理删除会破坏历史。因此本轮实现的是**可恢复的软删除**：

| 对象 | 删除语义 | 前置校验（全部复用既有守卫） | 恢复 |
| --- | --- | --- | --- |
| Agent | `enabled=False` + `deleted_at`/`deleted_reason`：从列表、候选、看板 Agent 行与统计中消失，Key 立即失效（AgentAPI 401 `AGENT_DISABLED`） | 持有有效执行必须选 WAIT/NOW；进行中项目最后一名主管拒绝（409 `LAST_SUPERVISOR_REQUIRED`）；原因必填 | `POST /agents/{id}/enable`（启用即清空删除标记） |
| Project | 冻结为 `ARCHIVED`（记录 `archived_from_status`）+ `deleted_at`：从列表与统计中消失 | 存在进行中/待审核任务拒绝（409 `INVALID_TRANSITION`，与归档同一条规则）；原因必填 | `POST /projects/{id}/restore`（回到删除前生命周期） |
| Task | 复用既有归档标记（五态不变、历史保留） | 有有效执行或待审核时拒绝 | `POST /tasks/{id}/restore` / 批量恢复 |

历史行（执行、提交、审核、成员关系、上下文版本）一行都不删，有测试断言兜底。

### 10.2 新增接口（均已实测）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /agents/{id}/delete` | body `{reason(必填), reclaim?: WAIT\|NOW}` |
| `POST /agents/bulk-delete` | body `{agent_ids[], reason, reclaim?}`，逐项结果 |
| `POST /agents/bulk-restore` | body `{agent_ids[]}`，逐项结果 |
| `POST /projects/{id}/delete` | body `{reason(必填), expected_version}` |
| `POST /projects/bulk-delete` / `POST /projects/bulk-restore` | 逐项结果 |
| `POST /tasks/bulk-archive` / `POST /tasks/bulk-restore` | body `{task_ids[], reason?}`，逐项结果 |
| `GET /agents?deleted=true`、`GET /projects?deleted=true` | 只看已删除（回收站视图） |
| `GET /tasks?...&status=ALL` | 不按状态过滤，一次返回全部五态（任务管理面板依赖） |

批量动作对每个目标使用独立 savepoint：一项失败只回滚该项，`failed[]` 逐项返回 `code/message/details`，
其余照常成功；同一幂等键重放返回原回执。批量路由必须注册在 `/{resource}/{id}` 之前，
否则 `bulk-delete` 会被当成 ID 解析（代码内有注释说明）。

### 10.3 前端

- **Agent 管理**：行复选框 + 表头全选；选中出现批量操作条（已选 n 项 / 批量删除 / 批量恢复 / 取消选择）；
  行内「删除 / 恢复」；删除确认弹窗（原因必填 + WAIT/NOW 处置 + 「历史保留可恢复」提示）；
  批量结果逐项回执弹窗（成功 n 项 / 失败 m 项 + 每项错误码与原因）；「状态」筛选新增「已删除」；
  已删除行显示徽标与原因；翻页/改筛选清空选择。
- **项目管理**：同样的选择、批量条、删除/恢复与「已删除」筛选；详情页新增「任务管理」页签。
- **任务管理面板**（`components/task/TaskManagementPanel.vue`，自包含，按项目取数）：
  筛选（关键词防抖 / 状态含「全部」→`status=ALL` / 难度 / 领取模式 / 包含已归档）+ 表格（勾选、编号、标题、
  状态与难度徽标、领取模式、执行者、计划截止、操作）+ 分页 + 新建（复用 TaskAuthoringForm）+
  编辑（复用 TaskAuthoringForm edit）+ 详情（复用 TaskDetailDialog）+ 归档/恢复与批量归档/批量恢复（逐项回执）。
  看板仍是业务动作（领取/提交/审核/回收）的执行入口，任务管理面板负责编制与清理。

### 10.4 验证

| 命令 | 结果 |
| --- | --- |
| `backend/.venv/bin/python -m pytest -q -m "not mysql"` | **406 passed**（含新增 `tests/test_t40_delete_and_bulk.py` 19 条：删除/批量/恢复/守卫/幂等/历史不删/`status=ALL`），连续两轮全绿 |
| `pnpm exec vue-tsc --noEmit` | 0 类型错误 |
| `pnpm exec vitest run` | **14 files / 170 tests passed** |
| `pnpm exec playwright test`（真实 MySQL + 双 API + 前端） | **31 passed** |
| 数据库迁移 | `identity.0002`、`projects.0002`（`deleted_at`/`deleted_reason` + 索引），`makemigrations --check` 无待生成迁移 |

内置浏览器实测（真实数据，`window.__errs` 采集 console.error/error/unhandledrejection 全程为空数组）：
Agent 批量删除 → 命中「最后一名主管保护」得到逐项失败原因（`LAST_SUPERVISOR_REQUIRED` + 服务端原话）→
删除无用 Agent 成功 →「已删除」筛选看到徽标与原因 → 恢复后回到默认列表；
项目删除 → 提示「软删除：冻结为只读，可在筛选『已删除』中恢复」→ 恢复 → 回到删除前生命周期；
任务管理页签看到全部五态任务、翻页与筛选可用。

期间发现并修复：任务管理面板的关键词输入只更新了模型、从不触发拉取（`onKeywordInput` 缺 `applyFilters()`），
已改为 350ms 防抖触发并在组件卸载/重置时取消挂起的定时器。


## 11 OSS 文件链路：oss_url、新建任务上传、提示词嵌入（2026-10-16 第四轮）

用户要求：启用上传文件到 OSS 接口并启用 OSS 访问；新建任务支持上传文件并统一上传 OSS，用 OSS URL 替换进提示词；优化 README Agent 提示词，要求 Agent 提交结果时若有文件也走上传接口并把文件 URL 写进提交文字。

### 11.1 设计取舍：私有 bucket + 签名 URL

阿里云账号安全策略禁止 public-read 对象（`Put public object acl is not allowed`），且公开读与本项目
「文件默认私有」的权限模型冲突。因此采用 **私有 bucket + 按次签名 GET URL**：`oss_url` 有效期
`OSS_URL_TTL_SECONDS`（默认 7 天，即阿里云长期 AK 上限），覆盖任务最长执行时限（L5 = 1 天）。
上传仍先落本地私有存储（校验/下载/备份/清理语义不变），persist 步骤内镜像到 OSS
（`FileAsset.oss_uploaded_at` 非空才可签名），OSS 失败整笔上传失败（503 `OSS_UNAVAILABLE`，原幂等键重试）。

### 11.2 交付内容

| 能力 | 说明 |
| --- | --- |
| `domain/oss_storage.py` + `config` OSS 段 | oss2 惰性客户端（启动不做网络 IO）、上传/签名/删除、`OssUnavailableError`（503） |
| 两套 API `POST /files` | 编制用暂存上传（multipart），返回 `file_id` + `oss_url`；agentapi 侧仅 SUPERVISOR；同键同内容幂等重放 |
| 视图 `oss_url` | `attachment_view` / `document_version_view` / `file_asset_view` 均带按次签名链接 |
| `POST /tasks` 新增 `attachment_file_ids` | 创建事务内绑定 TASK_REFERENCE，并把每个文件的 OSS URL 以「## 参考附件」小节追加进 description（即 Agent 提示词），领取时随 `requirements_snapshot` 冻结分发 |
| 前端新建任务表单 | 文件多选（白名单扩展名）、列表管理、逐个上传（独立幂等键）后创建；上传失败保留表单并回显 |
| README Agent 提示词 | 4.1 记住 `data.oss_url`；4.2 **必须**把产物 oss_url 写进提交 `content` 文字；PI_RESULT 增加 `artifact_oss_url`；错误表与契约清单补充 OSS 条目 |
| 单测防网络 | `config/settings_test.py` 强制 `OSS_ENABLED=false`（pytest-django 在任何 conftest 前构建 settings），真实 OSS 只对运行中的服务验证 |

### 11.3 实测证据（本轮全部真实执行）

| 命令/操作 | 结果 |
| --- | --- |
| `python -m pytest -q -m "not mysql"` | **429 passed**（含新增 T41 13 例、T42 7 例），13 deselected |
| `python manage.py check` / `makemigrations --check --dry-run` | 无问题 / `No changes detected`（`files.0003_fileasset_oss_uploaded_at` 已迁移到正式 MySQL） |
| `pnpm exec vitest run` / `pnpm build` | **172 passed** / 构建成功（含 vue-tsc 0 错误） |
| `python tests/manual/t43_oss_flow.py`（连 7101/7102 + 真实 bucket `harvesstask`） | **24 项全 PASS**：暂存上传→匿名下载字节一致→提示词嵌入→领取快照携带→产物 oss_url 下载→提交正文含 URL→审核 DONE→OSS 对象存在复核 |
| 浏览器 GUI 实测（内置浏览器，项目看板） | 新建任务表单上传 2 个文件 → TASK-0002 落「准备中」附件 2 个 → 详情提示词含 2 个真实 OSS 链接 → 链接匿名下载 HTTP 200 内容一致；**Console error 计数 0** |

环境备注：bucket `harvesstask`（oss-cn-shenzhen，标准存储，私有）由本轮创建；凭据来自本机
`~/.ossutilconfig`，仅写入 `backend/.env`（`.env.example` 只留占位符）。

## 12 导航优化：项目菜单改名与项目上下文返回（2026-10-16 第五轮）

用户要求：左侧菜单「项目」改为「项目管理」；修复「从项目详情跳到活动/任务看板等页面后无法回到原项目」的问题。

### 12.1 设计取舍：记住「最近项目」，而不是改写导航语义

五个固定入口（全局看板 / Agent 管理 / 项目管理 / 任务看板 / 活动）的顺序与目标不动（产品 §3.1、A25）。
新增 `frontend/src/stores/projectContext.ts`：进入项目看板或展开项目详情时记住最近项目
（id + 标题，localStorage 持久化；损坏数据静默忽略）。离开项目后：

| 位置 | 返回入口 |
| --- | --- |
| App 顶栏（全局常驻） | 「← 返回项目看板：<项目名>」；位于该项目看板本身时不显示（`topbar-return-project`） |
| 任务看板全局页页头 | 同上（`tasks-return-project`） |
| 活动页页头 | 优先取 `?project_id=` 深链目标（项目详情「项目活动」页签），否则取最近项目；标题优先取项目目录（`activity-return-project`） |
| 项目管理页 | 回到列表自动恢复上次打开的详情；详情 404 时清记忆并关闭，避免死链 |

### 12.2 实测证据（本轮全部真实执行）

| 命令/操作 | 结果 |
| --- | --- |
| `pnpm exec vitest run` | **17 文件 / 195 passed**（新增 project-context 7 例、project-return-links 8 例、project-board-context 4 例、ProjectsView 记忆 4 例；router.spec 标签同步） |
| `pnpm exec vue-tsc --noEmit` / `pnpm build` | 0 类型错误 / 构建成功 |
| `pnpm exec playwright test`（真实后端） | **31 passed**（smoke 五入口标签同步为「项目管理」） |
| 内置浏览器实测（真实 dev server + 真实 WebAPI） | 项目看板 → 侧栏「活动」→ 页头/顶栏返回入口 → 点击回到原项目看板；侧栏「任务看板」同样往返；`/activity?project_id=` 深链返回入口正确；侧栏「项目管理」打开后原项目详情自动恢复（标题正确）；**Console error 计数 0、无 Vite overlay** |

说明：子 Agent A/B 的浏览器验证与主控复测均受 IAB 会话限制（Playwright 指针 click 卡 actionability），
交互统一改用页面内真实 `element.click()` 驱动 Vue Router 完成。

## 13 README/Agent 提示词强化：文件上传与提交结果格式（2026-10-16 第六轮）

用户要求：更新 README（尤其 AGENT-PROMPT），补上文件上传相关内容，并规定提交任务结果的格式
（有输出文件必须上传 OSS，并把 OSS URL 放进结果文本）。

### 13.1 改动内容

| 位置 | 变化 |
| --- | --- |
| 提示词第 3 步 | 明确「参考附件」小节的 OSS 链接是任务输入；截止后上传/进度也会被拒 |
| 提示词第 4 步（重写） | 标题改为「先把产物上传到 OSS，再正式提交」；4.1 增加硬限制自查（单个 ≤20 MiB、最多 10 个产物、内容须与扩展名相符）与逐文件 file_id/oss_url 说明；4.2 新增**「提交结果格式（强制）」**：content 必须逐行写「产物下载（文件名）：oss_url」、artifacts 逐项引用 file_id、self_check evidence 引用 URL、summary 需满足验收标准附加要求、无文件时写明「本次无文件产物」 |
| 提示词第 5 步 | PI_RESULT 的 artifact_oss_url 说明补充「多个用逗号分隔」 |
| 错误表 | 新增 413 FILE_TOO_LARGE、415 UNSUPPORTED_MEDIA_TYPE、409 ATTACHMENT_LIMIT_EXCEEDED、409 EXECUTION_NOT_CURRENT；VERSION_CONFLICT 拆成提交场景专用指引（用 409 details.current_version 重读版本后重新提交）；EXECUTION_TIMEOUT 覆盖上传/进度；删除响应中不存在的 `reason` 字段引用 |
| 契约列表 | 新增「有产物必须上传并写入结果文本」与「产物硬限制」两条 |
| 新增小节 | 「文件与 OSS 的往返（Agent 视角）」：下行（Web `/files` 暂存上传 + `attachment_file_ids` → 提示词嵌入）、上行（产物上传 + 提交文本）、7 天签名过期（403）时的处理指引、t43 验收入口 |
| CONTRACT.md | 修正 `NO_MATCHING_TASK` 表述：领域服务内部保留 reason，AgentAPI 信封当前只透出 `data=null`，调用方不得依赖 |

### 13.2 独立校验（子 Agent，只读）

对提示词逐条对照 `errors.py`/`file_service.py`/`submission_service.py`/`claim_service.py`/env 上限等
共 9 项核对：首轮 7 PASS / 2 FAIL（`reason` 字段不存在；错误表遗漏 415/413/10 个上限/过期上传与
VERSION_CONFLICT 指引错误）→ 修复后复审全部 PASS。残留张力同步修复：
t39 的「产物恰好一条」断言放宽为「≥1 且 ≤10、交付文件必在库、全部冻结绑定」（与多产物提示词一致）。

### 13.3 实测证据

| 命令 | 结果 |
| --- | --- |
| `t39_pi_agent_flow.py --print-prompt` | exit=0，8953 字节，含「提交结果格式（强制）」「产物下载（」「本次无文件产物」；无 `reason` 残留 |
| `t39_pi_agent_flow.py`（真实 pi Agent × 3 次，连 7101/7102 真实服务） | **全部通过**（21 PASS）：pi 自报 submitted、库中执行/提交各一条、摘要含验收标记、**提交正文包含产物 OSS 链接（新加强制格式断言）**、审核闭环 DONE |
| 端口可配置 | 新增 `T39_WEB_BASE` / `T39_AGENT_BASE`（默认仍为 8000/8001），与 t43 的覆盖方式一致 |

## 14 修复：项目管理「打开项目」无可见反馈（2026-10-16 第七轮）

用户反馈：项目管理页点击「打开项目」没有弹出详情窗口。

### 14.1 根因（浏览器实测复现）

详情区块渲染在项目列表（20 行约 1500px）**下方**，视口只有 720px：点击后 `selectedId` 与内容
其实都已切换（标题、成员、文档全部更新，Console 无错误），但页面不滚动，
`detailTop=1494px` 始终在视口外 —— 用户看到的只是「点了没反应」。

### 14.2 修复

| 改动 | 说明 |
| --- | --- |
| 点击后定位详情 | `selectProject` 后经 `nextTick` 把详情区块滚入视野，顶部留出置顶栏高度（读取 CSS `scroll-margin-top`，60+12=72px，与视觉同源） |
| 不用平滑滚动 | 实测该环境（内嵌浏览器）`behavior:'smooth'` 既不执行动画也不落到目标；平滑+兜底还存在「动了一点但没到位」的竞态。改为 `behavior:'auto'` 瞬间定位：任何环境 100% 可见、无延迟 |
| 主动打开 404 的行为 | 用户主动点击「打开项目」而项目已不可见（已删除/无权访问）时，**保留详情面板并显示错误原因**（可关闭/可重试），不再静默关闭；仍清除「最近项目」记忆避免顶栏返回入口指向死链。挂载时自动恢复的旧记忆保持静默关闭（避免每次进入都弹错误面板） |

### 14.3 验证

| 命令/操作 | 结果 |
| --- | --- |
| `pnpm exec vitest run` | **197 passed**（新增：滚动目标与 `behavior:'auto'` 断言、主动打开 404 显示错误面板；原「自动恢复 404 静默关闭」用例保持通过） |
| `pnpm exec vue-tsc --noEmit` / `pnpm build` | 0 错误 / 构建成功 |
| `pnpm exec playwright test` | **31 passed**（含 admin.spec 全部项目详情用例） |
| 浏览器实测（内置浏览器，真实数据） | 页面顶部点击「打开项目」→ `scrollY 972`、详情顶部进入视口、标题正确；已打开状态下切换另一项目 → 同样滚入视口；连续两次操作 + 全程 **Console error 计数 0** |
