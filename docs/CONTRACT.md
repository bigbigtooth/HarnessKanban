# Harness看板（HarnessKanban）实施契约（T01 冻结件）

| 项目 | 内容 |
| --- | --- |
| 契约版本 | 1.0 |
| 冻结日期 | 2026-10-15 |
| 依据 | [product-design.md](./product-design.md) 1.1、[DEVELOP.md](./DEVELOP.md) D01–D21、[PLAN.md](./PLAN.md) T01 |
| 状态 | 已冻结。修改须由主线程裁决并同步修订本文件与两套接口契约 |
| 代码落地 | `backend/domain/contracts.py`（枚举、ActorContext、错误、动作矩阵）为本文件的代码化副本 |

本文件冻结实现期公共契约：模块边界、枚举取值、错误码与 HTTP 状态、响应信封、领域服务签名、锁顺序、幂等作用域与文件所有权。各任务实现不得自行新增状态、角色、续期端点或领取别名。

## 1 模块布局与导入路径

```text
backend/
  config/            settings.py, urls.py, asgi.py, wsgi.py, celery_app.py, env.py(配置校验)
  apps/
    identity/        Agent
    projects/        Project, ProjectMember, ProjectContextVersion, Document, DocumentVersion
    tasks/           Task, TaskExecution, TaskClaimLock, TaskSubmission, TaskReview
    files/           FileAsset, AttachmentLink
    audit/           ActivityEvent
    jobs/            IdempotencyRecord, OutboxEvent
  domain/            contracts.py, errors.py, clock.py, locking.py, transactions.py,
                     idempotency.py, events.py, execution.py, permissions.py,
                     agent_service.py, project_service.py, task_service.py, claim_service.py,
                     submission_service.py, review_service.py, file_service.py,
                     context_service.py, dashboard_service.py
  webapi/            Django DRF 适配：urls.py, views/*.py, serializers.py, envelope.py, middleware.py
  agentapi/          FastAPI 应用：main.py, deps.py, envelope.py, routers/*.py
  jobs/              celery 作业：timeout_scan.py, outbox_dispatch.py, cleanup.py, capacity_audit.py
  tests/
  manage.py
  pyproject.toml
frontend/
  src/api/           web client + 响应处理（两套接口中的 Web 客户端）
  src/components/    表单、卡片、详情弹窗、状态提示
  src/views/         Agent、项目、看板、全局视图
  src/stores/        查询条件、页面状态与请求协调
  src/router/
  tests/unit/        Vitest
  tests/e2e/         Playwright
deploy/
docs/
```

唯一模型体系为 `apps/*` 的 Django 模型与 Django migrations；不引入 SQLAlchemy 模型或 Alembic。两个 API、Celery 与测试共用同一领域服务。

## 2 枚举（唯一事实源：`backend/domain/contracts.py`）

| 枚举 | 取值 |
| --- | --- |
| `AgentRole` | `SUPERVISOR`、`WORKER` |
| `TaskStatus` | `PREPARING`、`TODO`、`IN_PROGRESS`、`IN_REVIEW`、`DONE` |
| `ClaimMode` | `OPEN`、`ASSIGNED` |
| `ProjectStatus` | `DRAFT`、`ACTIVE`、`COMPLETED`、`ARCHIVED` |
| `ReviewDecision` | `APPROVE`、`REJECT` |
| `EndReason` | `RELEASE`、`RECLAIM`、`TIMEOUT`、`SUBMIT` |
| `ActorType` | `web_operator`、`agent`、`system` |
| `AttachmentPurpose` | `TASK_REFERENCE`、`EXECUTION_ARTIFACT`、`SUBMISSION_ARTIFACT` |
| `DeliveryState` | `PENDING`、`PUBLISHED`、`PROCESSED`、`NEEDS_ATTENTION` |
| `IdempotencyState` | `IN_PROGRESS`、`DONE` |
| `DifficultyLevelName` | `1`→`L1` 入门、`2`→`L2` 简单、`3`→`L3` 中等、`4`→`L4` 困难、`5`→`L5` 极难 |

固定超时映射 `DIFFICULTY_TIMEOUT_SECONDS = {1:900, 2:1800, 3:7200, 4:28800, 5:86400}`；配置校验要求五级为正且严格递增，非法配置启动即失败。

展示编号：`number` 为项目内整数，对外显示 `TASK-%04d`。`selected_levels` 返回形如 `["L1","L2"]`。

## 3 响应信封与错误

两个 API 共用同一信封。

成功：

```json
{"data": {"...": "..."}, "request_id": "<uuid>"}
```

列表在 `data` 内：`items`、`total`、`page`、`page_size`（默认 20、最大 100）。

错误：

```json
{"error": {"code": "VERSION_CONFLICT", "message": "...", "details": {}}, "request_id": "<uuid>"}
```

`request_id` 同时写入响应头 `X-Request-Id` 与结构化日志。

| HTTP | 错误码 |
| --- | --- |
| 400 | `INVALID_ARGUMENT`、`IDEMPOTENCY_KEY_REQUIRED`、`UNSUPPORTED_PARAMETER` |
| 401 | `AGENT_TOKEN_MISSING`、`AGENT_TOKEN_INVALID`、`AGENT_DISABLED` |
| 403 | `ROLE_FORBIDDEN`、`SELF_REVIEW_FORBIDDEN`、`REQUEST_ORIGIN_FORBIDDEN` |
| 404 | `RESOURCE_NOT_FOUND` |
| 405 | `METHOD_NOT_ALLOWED` |
| 409 | `VERSION_CONFLICT`、`TASK_ALREADY_CLAIMED`、`TASK_DIFFICULTY_MISMATCH`、`INVALID_TRANSITION`、`EXECUTION_TIMEOUT`、`CAPACITY_EXCEEDED`、`IDEMPOTENCY_KEY_REUSED`、`IDEMPOTENCY_IN_PROGRESS`、`EXECUTION_NOT_CURRENT`、`REVIEW_ALREADY_DECIDED`、`AGENT_NAME_CONFLICT`、`MEMBERSHIP_CONFLICT`、`LAST_SUPERVISOR_REQUIRED`、`PROJECT_NOT_ACTIVE`、`SUBMISSION_EXISTS`、`CAPACITY_BELOW_RESERVED`、`TASK_NUMBER_CONFLICT`、`ATTACHMENT_LIMIT_EXCEEDED`、`DOCUMENT_CONFLICT` |
| 413 | `FILE_TOO_LARGE` |
| 415 | `UNSUPPORTED_MEDIA_TYPE` |
| 429 | `RATE_LIMITED` |
| 503 | `DATABASE_BUSY`、`CLAIM_LOCK_TIMEOUT`、`CLAIM_LOCK_MISSING`、`DEPENDENCY_UNAVAILABLE`、`OSS_UNAVAILABLE` |

框架级校验错误统一转换为上述结构，不返回堆栈、SQL 或磁盘路径。无项目授权的资源统一 `404`。业务错误一律由 `domain.errors.DomainError` 抛出并携带上表的 HTTP 状态。

## 4 ActorContext

```python
@dataclass(frozen=True)
class ActorContext:
    actor_type: ActorType            # web_operator | agent | system
    request_id: str
    agent_id: UUID | None = None     # 仅 agent 来源：已鉴权身份
    key_version: int | None = None   # 仅 agent 来源：请求鉴权时的 Key 版本
    web_operator_note: str | None = None  # web_operator 来源说明
```

- `web_operator`：无登录，来源记为本地管理者；目标 Agent 通过业务参数传入，不进入 `agent_id`。
- 目标 Agent ID 是业务对象，不是已认证身份；Web 代操作在审计中记 `reviewer_identity_verified=false`、`actor_type=web_operator`。
- AgentAPI 的领取者、执行者、审核者只能来自 Key；请求体中的身份字段一律忽略并 400 `INVALID_ARGUMENT`（出现即拒绝）。
- `key_version` 只在请求内用于撤销竞态校验，不进入幂等作用域。

## 5 领域服务入口（`backend/domain/`）

统一签名风格：显式 `actor: ActorContext`、`session` 由调用方（HTTP 适配层或 jobs）打开最外层事务后传入；服务不吞异常、不自行 commit。

```python
create_agent(actor, payload, idem_key) -> dict
update_membership(actor, project_id, agent_id, action, payload, idem_key) -> dict
publish_task(actor, task_id, expected_version, idem_key) -> dict
list_tasks(actor, filters, page, page_size) -> dict
claim_task(actor, parsed_levels, scope, body, idem_key) -> dict
update_progress(actor, task_id, execution_id, message, percent, idem_key) -> dict
end_execution(actor, task_id, execution_id, reason, expected_version, idem_key) -> dict
submit_result(actor, task_id, payload, idem_key) -> dict
review_submission(actor, task_id, payload, idem_key) -> dict
reopen_task(actor, task_id, reason, expected_version, idem_key) -> dict
upload_and_bind(actor, payload, temp_file, idem_key) -> dict
upload_staging_file(actor, *, upload, project_id=None, idem_key) -> dict
get_dashboard(actor, scope) -> dict
```

命名空间补充（同文件冻结，避免各任务自造）：`author_task`/`edit_task`/`withdraw_task`/`assign_task`/`archive_task`/`restore_task`/`activate_project`/`complete_project`/`archive_project`/`restore_project`/`upload_document`/`update_context`/`reorder_tasks`/`heartbeat`/`create_execution`…

服务返回**普通 dict / dataclass**，禁止返回 QuerySet 或惰性对象。

## 6 T04 锁内原语（唯一实现，供所有包装器复用）

```python
def finish_execution_locked(execution, *, ended_at, end_reason) -> None
    # 条件 UPDATE execution: ended_at IS NULL -> ended_at/end_reason，必须匹配一行
    # 条件 UPDATE Agent: reserved_execution_count>0 -> 减一，必须匹配一行
    # 不修改任务状态、不写审计/Outbox、不开或提交事务

def reset_in_progress_task_locked(task, *, expected_execution_id, now) -> None
    # CAS Task: active_execution_id==expected_execution_id AND status==IN_PROGRESS
    #         -> status=TODO, active_execution_id=NULL, assignee_agent_id=NULL,
    #            claimed_at=NULL, submitted_at=NULL, version=version+1
    # 受影响行数必须为 1，否则 DomainError(DATABASE_BUSY/VERSION_CONFLICT) 整笔回滚
```

- `end_execution` 包装器 = `finish_execution_locked` + `reset_in_progress_task_locked` + 一次活动/Outbox/回执。
- `submit_result` 只用 `finish_execution_locked`，随后自行写 `IN_REVIEW`（绝不先写 `TODO`）。
- 包装器不重复改状态或版本。

## 7 锁顺序、事务与时钟

固定锁顺序：**全局领取锁（TaskClaimLock.id=1，需要时） → 项目 → Agent → 任务 → 执行**；同类多行按 ID 升序。多任务撤权/禁用/补偿回收先确定全部相关 ID，再按类别一次性锁全部行。

- MySQL：显式 `READ COMMITTED`，决策实体锁定后重读。
- SQLite：任何业务 SELECT 之前先真实 `UPDATE TaskClaimLock SET revision=revision+1 WHERE id=1` 取得写库锁；不在 `atomic()` 内另发 `BEGIN IMMEDIATE`。
- 锁计划失效（关系变化、候选不在集合内）：整笔回滚后重新规划，最多 3 次，超限返回 503 `DATABASE_BUSY`；不得伪装成 `NO_MATCHING_TASK`。
- 时间：取得全部业务锁后由数据库读取 UTC（MySQL `UTC_TIMESTAMP(6)`，SQLite UTC 字符串统一精度），返回 timezone-aware；禁止复用等锁前的时间。生产不接受客户端上传 `now`。
- `atomic()` 不作为最外层事务入口（`ATOMIC_REQUESTS` 关闭）；写服务自开 durable 事务。

## 8 幂等契约

- 作用域：`actor_scope` ∈ {`agent:<agent_uuid>`, `web_operator`}；`operation` = `"<METHOD> <route template>"`，例如 `POST /api/agent/v1/get_task/`；`key` 来自 `Idempotency-Key` 头。
- 唯一约束 `(actor_scope, operation, key)`。请求摘要 = 规范化 JSON：API 操作、资源 ID、目标项目、实际业务 body、等级集合；**不含** request_id、token 明文、Key 版本，因此换 Key 后仍可重试。
- 同键不同摘要 → 409 `IDEMPOTENCY_KEY_REUSED`；同摘要成功记录 → 返回原回执并加 `idempotent_replay=true`。
- 回执读取前必须校验当前身份、项目授权与角色；未授权不返回正文（404）。历史回执不检查新动作的状态/指派/容量/超时。
- 自动领取空结果也记录成功回执（`data=null`、`reason=NO_MATCHING_TASK`）；数据库失败与业务冲突不记录成功回执。
- 并发同键：唯一约束竞争失败方退出并**重开事务**读取获胜回执；Django 已标记回滚的事务内禁止继续查库。未完成竞争返回 409 `IDEMPOTENCY_IN_PROGRESS`。
- Agent 创建/重置 Key 的回执只存 Agent ID、操作结果与 Key 版本；原文丢失时同键返回 `key_delivery_unavailable=true`。明文 Key 不入回执/审计/Outbox/日志/前端持久存储。
- 默认保留 ≥7 天。

## 9 动作矩阵（唯一允许集合）

| 动作 | 起始 → 目标 | 权限 | 关键条件 |
| --- | --- | --- | --- |
| `publish` | PREPARING → TODO | 项目主管 / Web | 项目 ACTIVE、标题/要求/验收/难度完整、ASSIGNED 者已授权启用 |
| `withdraw` | TODO → PREPARING | 项目主管 / Web | 无当前执行、必填原因 |
| `claim` | TODO → IN_PROGRESS | 主管与员工（含 Web 代领） | 全局锁、项目授权、指定关系、容量、`enabled` |
| `submit` | IN_PROGRESS → IN_REVIEW | 当前执行者（含 Web 代提交） | 当前 `execution_id`、`now < deadline`、正式结果唯一 |
| `release` / `reclaim` / `timeout` | IN_PROGRESS → TODO | 自己 / 主管 / system | 单次结束、释放一次槽 |
| `approve` | IN_REVIEW → DONE | 另一有效主管 | 当前提交、非提交者；Web 必须选主管且记未验证 |
| `reject` | IN_REVIEW → TODO | 项目主管 | 必填原因、保留历史、默认 ASSIGNED 原执行者 |
| `reopen` | DONE → TODO | 项目主管 / Web | 项目 ACTIVE、必填原因、清当前轮字段、保留历史 |
| `archive`（独立标记） | 任意（不改状态） | 项目主管 / Web | 无有效执行且非 IN_REVIEW |

其他转换（PREPARING→IN_PROGRESS、TODO→DONE、IN_PROGRESS→DONE 等）一律 409 `INVALID_TRANSITION`。

等级参数解析（`read_task` / `get_task` 共用同一实现）：

```text
读取原始多值 query；出现 l0/l6 等非法等级键 → 400
重复同名等级键 → 400
无任何 l1..l5 → levels = [1,2,3,4,5]
否则值必须严格为 "0" 或 "1"（否则 400）
levels = 值为 "1" 的 N 的升序列表（可为空）
```

领取优先级：自动模式先指定给自己，再按 `sort_order`、`created_at`、`id`；无候选 → 200 `data=null`（即使容量满；领域服务内部带 `reason=NO_MATCHING_TASK`，但 AgentAPI 响应信封当前只透出 `data=null`，调用方不要依赖 `reason` 字段）；有候选但容量满 → 409 `CAPACITY_EXCEEDED`；指定 `task_id` 与等级/范围不匹配 → 409，不在授权范围 → 404；`GET /get_task/` → 405。

## 10 文件所有权

单一所有者（其他任务需要修改时先提出，由主线程整合）：

| 文件/区域 | 所有者 |
| --- | --- |
| `docs/CONTRACT.md`、`backend/domain/contracts.py`、`backend/domain/errors.py` | 主线程 |
| `backend/apps/**`（全部模型与迁移）、`backend/config/settings.py` | T03 所有者 |
| `backend/domain/transactions.py`、`clock.py`、`locking.py`、`execution.py` | T04 所有者 |
| `backend/webapi/urls.py`、`backend/agentapi/main.py` 总路由出口 | T05 所有者 |
| `frontend/src/router/index.ts`、`frontend/src/api/client.ts` | T28 所有者 |
| `backend/pyproject.toml`、`frontend/package.json` 及锁文件 | T02 所有者 |
| 公共测试 fixture（`backend/tests/conftest.py`、`backend/tests/factories.py`） | 主线程指派唯一所有者 |

并行约束：同一文件不并行写入；每个并行组最多 3 个子任务；出现契约变化立即停写并回报主线程。

## 12 运维管理与失败恢复入口（T34 冻结）

管理入口只存在于 WebAPI（本机回环）。远程 Agent 代理只转发 `/api/agent/v1`，因此这些端点天然不出现在远程入口上。

| 方法与路径 | 功能 |
| --- | --- |
| `GET /api/web/v1/ops/failures` | 可查的失败记录：未处理且需要干预的 Outbox 事件，以及系统侧失败记录（锁等待超时、重试耗尽、容量偏差修复）。分页，`items/total/page/page_size` |
| `POST /api/web/v1/ops/outbox/{event_id}/retry` | 人工重派：保留原事件行，仅重置为可派发并记录一次审计。已处理事件拒绝（409），不得复活已消费事件 |
| `POST /api/web/v1/ops/capacity-audit` | 立即执行容量巡检，返回检查/修复/上报计数 |

失败记录载体：`ActivityEvent`（`actor_type=system`，`action` ∈ {`system.retry_exhausted`、`system.lock_timeout`、`agent.capacity_repaired`}，`request_id` 串联原请求，`reason` 保存原因）。锁失败发生在事务回滚之后，因此该记录写在**新的短事务**中，且为尽力而为：写失败只记日志，绝不掩盖原始错误，也绝不把已提交业务误报为未提交。

脱敏要求：失败记录与运维响应不得包含 Key 明文、完整结果正文、磁盘路径；Outbox payload 继续经过 `domain.events._sanitize` 过滤。

## 11 未实现的接口一律 404

未列入本契约的动作端点、状态与别名（含 `GET /get_task/` 之外的领取别名、续期端点、第六种状态）不得注册。`GET /api/agent/v1/get_task/` 必须返回 405，而非 404 或静默成功。

## 13 文件与 OSS（oss_url，T40–T42 冻结补充）

私有 bucket + 签名 GET URL：上传仍先落本地私有存储（校验、下载、备份、清理不变），启用 OSS（`OSS_ENABLED=true`）时在同一 persist 步骤内镜像到阿里云 OSS；`FileAsset.oss_uploaded_at` 非空表示 OSS 副本已落地，只有此时才允许签名出 `oss_url`。OSS 上传/签名失败 → 503 `OSS_UNAVAILABLE`，整笔上传失败（不留本地对象与行），按**原幂等键**重试。

- `oss_url`：对 `storage_key` 现签名的 GET 链接，有效期 `OSS_URL_TTL_SECONDS`（60..604800，阿里云长期 AK 上限 7 天）。视图（`attachment_view`/`document_version_view`/`file_asset_view`）按需现签，响应间不缓存不持久化；OSS 未启用或文件无 OSS 副本时为 `null`。
- `POST /api/web/v1/files`（multipart `file`，可选 `project_id`）：任务编制用**暂存上传**，返回 `data.file_id` 与 `data.oss_url`；不产生链接/版本，未绑定 24h 后由清理任务回收。同键同内容重放返回原回执。
- `POST /api/agent/v1/files`：同上，Agent 侧仅 SUPERVISOR 可用；引用他人暂存文件 → 404。
- `POST /tasks`（两套 API）新增可选 `attachment_file_ids`（UUID 数组，去重，上限沿用 `MAX_TASK_REFERENCE_ATTACHMENTS`）：必须为本人上传、未绑定、属本项目的暂存文件；创建事务内绑定为 `TASK_REFERENCE` 链接（活动/Outbox 与显式附件上传同构），并把每个文件的 `oss_url` 以「## 参考附件（OSS 下载链接，可直接下载）」小节追加进 `description`（即 Agent 提示词正文），领取时随 `requirements_snapshot` 冻结分发。无 OSS 副本时照常绑定但不追加 URL 行。
