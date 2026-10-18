# A01–A30 覆盖表与主线程签收记录（T38 交付门禁）

| 项目 | 内容 |
| --- | --- |
| 记录日期 | 2026-10-15 |
| 依据 | [PLAN.md](./PLAN.md) §4.2 覆盖表、[product-design.md](./product-design.md) §20、[DEVELOP.md](./DEVELOP.md) D20/D21 |
| 覆盖状态 | 30 项全部用可重复命令验证 |
| 证据形式 | 每项给出实际执行的命令与用例名，均断言数据库行，不以 HTTP 200 或截图单独验收 |

## 1 A01–A30 逐项证据

| 编号 | 验收要求摘要 | 证据（实际执行） | 状态 |
| --- | --- | --- | --- |
| A01 | 首次一次交付 Key；详情/日志/回执/事件无明文；原键不补发 | `pytest -m "api and identity"`（T07：`test_t07_agent_identity.py` 21 项，含 caplog 与幂等表断言）；`pnpm exec playwright test tests/e2e/admin.spec.ts`（一次性交付、关闭即清除、重放提示重置） | 通过 |
| A02 | 缺失/错误/旧 Key/禁用为 401；新 Key 保持原 Agent 身份 | T07/T09 用例；`tests/manual/t37_e2e_acceptance.py` 步骤 03（非法与缺失身份不通过） | 通过 |
| A03 | 员工不发布/审核/读准备中；非成员 404；统计按可见任务 | T13/T23（员工经详情与活动入口读准备中均 404）、T25（员工 `PREPARING=0` 且 `PUBLISHED_ONLY`）、T37 步骤 03 | 通过 |
| A04 | 移除/降级/禁用与并发撤权后仍至少一名有效主管 | T08 用例，含 `-m "mysql and concurrency and projects"` 真实并发撤权（1 通过 + 1 冲突） | 通过 |
| A05 | V1/V2 快照与文件引用不被覆盖；真实领取采用当时版本 | `tests/test_t18_atomic_claim.py::test_claim_snapshot_freezes_the_document_version_refs_at_claim_time`：真实领取后断言语执行记录 `context_version` 指向当时版本、`document_version_refs` 精确含 V1；随后新增 V2 不改变既有执行的冻结引用；V2 之后的新领取引用新上下文 | 通过 |
| A06 | 五级显示/筛选正确，0/6 及其他非法值拒绝 | T13（难度校验）、T15（等级矩阵）、T31 浏览器用例（L1–L5 展示与筛选） | 通过 |
| A07 | 拖拽先业务表单；非法 TODO→DONE 拒绝并按服务端回退 | `tests/e2e/board.spec.ts`（非法跨列拖拽被拒并回退服务端状态；列内拖拽调用排序端点） | 通过 |
| A08 | ASSIGNED 仅指定者可领；转 OPEN 后授权 Agent 可竞争 | T14 用例；T37 步骤 10（其他员工领不到独占返工） | 通过 |
| A09 | 跨两实例 + Web 代领 20 并发仅一次领取 | `tests/manual/t19_cross_instance_claim.py`（两个真实 uvicorn 进程 + MySQL：200=1、409=19、1 执行、1 槽） | 通过 |
| A10 | 同 Agent 容量 1 最多一占槽；非法降低上限拒绝；失败无残留 | T07（`CAPACITY_BELOW_RESERVED`）、T18（满容量 409 且无残留）、T27（容量巡检） | 通过 |
| A11 | 丢响应或超时改派后原键仍返回原执行/截止；异参 409 | T18（重放返回原执行与截止）、T20（超期重放返回原提交）、T37 步骤 08 | 通过 |
| A12 | 五级秒数；活动不延时；等于/超过截止拒绝 | T16/T17 用例（900/1800/7200/28800/86400、心跳与进度不延时、等于截止拒绝） | 通过 |
| A13 | 提交与回收仅一方生效，不并存待审核与新有效执行 | T20/T22 用例（提交与回收竞争、待审核不回收） | 通过 |
| A14 | 成功提交后过原截止重放仍返回原提交，只一次容量释放 | T20（`test_replay_after_the_deadline_returns_the_original_submission`） | 通过 |
| A15 | 自审/员工拒绝；相反审核只一个决定；Web 记录未验证身份 | T21 用例，含 `-m "mysql and concurrency and review"`（并发批准/驳回仅一个决定）；T32 浏览器用例 | 通过 |
| A16 | 驳回重领新执行/新提交，历史保留，旧执行不能覆盖新轮 | T22（两轮返工用例）；T37 步骤 09–11（两轮执行/提交/审核全部保留） | 通过 |
| A17 | 指定者禁用/撤权不能领/提交；改派或开放后可恢复 | T08（撤权即失效、NOW 立即回收）、T14/T22（改派恢复） | 通过 |
| A18 | 超时重领后旧心跳/进度/提交不能复活或覆盖新执行 | T16/T17（旧执行 ID 拒绝写入）、T22（旧执行不能提交进新轮）、T37 步骤 10 | 通过 |
| A19 | 文件大小/类型/路径/项目校验；同项目临时产物仍防越权 | T11 用例（白名单、内容嗅探、服务端存储键、用途授权矩阵）；T37 步骤 06（同项目其他员工下载未提交产物 404） | 通过 |
| A20 | 1/2/1/1/1 驳回后 1/3/1/0/1，总数 6，历史不计新任务 | T25 用例（真实驳回驱动）；T33 浏览器用例 | 通过 |
| A21 | 进行中/待审核阻止归档，未完成阻止结项，归档不可领 | T10（结项/归档门禁）、T23（归档前置条件）；T37 步骤 14 | 通过 |
| A22 | Redis/进程/重复派发恢复后已提交不丢且无额外业务变更 | T26 用例（派发失败保留事件、消费防重、预占租期恢复、升级为待处理） | 通过 |
| A23 | SQLite busy/MySQL 死锁超时明确可重试且完整回滚 | T04（重试与回滚）、T34（`retry_exhausted`/`lock_timeout` 可查记录）、T19 场景 2（503 且无无锁降级） | 通过 |
| A24 | 真实 HTTPS 代理保留 AGENT_TOKEN；远端 Web 不可达；本机无需登录 | `tests/manual/t35_proxy_acceptance.py`（nginx 1.31.6 + 自签名证书，真实 HTTPS）：经代理携带 `AGENT_TOKEN` 访问 `/api/agent/v1/me` 返回 200 且身份未被改写；**A/B 对照**——同一请求在 `underscores_in_headers off` 的第二个入口返回 401 `AGENT_TOKEN_MISSING`，证明是指令而非代理本身在承载该头；`/api/web/v1/*` 三个路径经代理均为 404；本机 WebAPI 无凭据 200；`AGENT-TOKEN` 直接访问亦 401；代理响应保留 `X-Request-Id` | 通过 |
| A25 | 失败/冲突可恢复，筛选保留，窄屏/键盘可操作，无伪状态 | T31/T32/T33 浏览器用例（筛选保留、409 保留输入、窄屏 375px、键盘操作、控制台零报错）；T28 单测 | 通过 |
| A26 | 两角色任意等级；未传全五、部分精确、全 0 空、非法重复 400 | T15（等级矩阵与错误路径）、T18；T37 步骤 02（L1+L2 精确筛选与回显） | 通过 |
| A27 | read 无副作用；get 单项；全 0 自动空/指定 409；无候选不占槽；GET 405 | T18 用例；T37 步骤 02（前后状态一致）与步骤 04；T05（`GET /get_task/` 为 405） | 通过 |
| A28 | 请求 L1 仍回收自身过期 L5 槽；候选先回收再领取；来源 system | T18（`test_own_expired_slot_is_recovered_even_when_requesting_another_level`，断言 `end_reason=TIMEOUT`、活动为 `task.timeout_recovered`） | 通过 |
| A29 | 持锁进程退出全回滚/释放锁；锁行被清空先补回再持锁，补回失败或等待超时拒绝，无降级 | `tests/manual/t19_cross_instance_claim.py` 场景 2/3（持锁未提交 → 5.7s 后 503 `CLAIM_LOCK_TIMEOUT`；释放后可正常领取）；T04（取锁原语缺行仍 `CLAIM_LOCK_MISSING`；写服务与 flush 补回 id=1 后再持锁） | 通过 |
| A30 | 配置/Key/重放不改原截止；等锁跨截止后按新数据库时间拒绝 | T16/T17（截止边界）、T20（超期重放），T36 恢复演练（恢复后截止仍为领取时刻加固定超时） | 通过 |

## 2 R01–R13 覆盖

| 需求 | 主要落点 | 状态 |
| --- | --- | --- |
| R01 角色与员工不能发布 | identity + task_service + 前端角色门禁（T07/T08/T13/T21/T29） | 通过 |
| R02 独立 Key 与 AGENT_TOKEN | security.py + AgentAPI 鉴权（T07/T05） | 通过 |
| R03 项目标题/背景/文档/多成员 | project_service + context_service + file_service（T08/T10/T11/T12/T30） | 通过 |
| R04 固定五态与状态动作 | 状态机注册表 + 动作端点（T13/T17/T18/T20/T21/T22/T23） | 通过 |
| R05 五级难度 | 模型约束 + 等级解析 + 时限映射（T03/T15/T16/T18） | 通过 |
| R06 单执行者/指定/开放 | 单例锁领取 + 资格策略（T14/T18/T19/T37） | 通过 |
| R07 主管审核结果 | review_service + 前端审核表单（T20/T21/T22/T32） | 通过 |
| R08 本机无鉴权 WebAPI | 请求边界中间件 + 前端无登录（T05/T28） | 通过（远程隔离见 A24，未运行） |
| R09 两套 API 与前后端分离 | Django WebAPI + FastAPI AgentAPI + Vue 前端（T02/T04/T05） | 通过 |
| R10 既定技术栈与双库环境 | SQLite 与 MySQL 均空库迁移通过，真实 MySQL 竞争用例通过（T02/T03/T04/T26/T36） | 通过 |
| R11 所有 Agent 可选任意 l1..l5 | `parse_level_filters` + read_task/get_task（T15/T18/T19） | 通过 |
| R12 领取起固定超时、不续期 | 执行快照 + 截止判定（T09/T16/T17/T18/T20/T36） | 通过 |
| R13 只读列表、原子领一项、全局锁 | `TaskClaimLock` 单例 + 只读列表（T15/T18/T19） | 通过 |

## 3 实际运行的验收命令汇总

| 命令 | 最近一次结果 |
| --- | --- |
| `python manage.py check` | 无问题 |
| `python manage.py makemigrations --check --dry-run` | No changes detected |
| `python -m pytest -q -m "not mysql"` | 387 passed, 0 failed |
| `DB_ENGINE=mysql python -m pytest -q -m "mysql"` | 13 passed |
| `python -m pytest -q -m "unit and deployment"` | 24 passed（T02 配置校验：非法超时映射、拆分数据库变量、相对/缺目录 SQLite 路径、不可写文件目录、缺失必需项、非法引擎、零/负运行上限，以及 Django 启动期拒绝非法配置） |
| `DB_ENGINE=mysql python -m pytest -q -m "mysql and concurrency and identity"` | 1 passed（认证后 Key 轮换/撤权/禁用不得继续写入，真实并发轮换下不产生半条领取） |
| `DB_ENGINE=mysql python tests/manual/t19_cross_instance_claim.py` | 全部通过（两个真实 API 进程） |
| `DB_ENGINE=mysql python tests/manual/t36_backup_restore.py` | 全部通过（成对备份与隔离恢复） |
| `DB_ENGINE=mysql python tests/manual/t37_e2e_acceptance.py` | 全部通过（跨 API 单一主线 14 步） |
| `DB_ENGINE=mysql python tests/manual/t38_capacity.py` | 全部达标（100 Agent / 30 项目 / 99,990 任务） |
| `DB_ENGINE=mysql python tests/manual/t35_proxy_acceptance.py` | 全部通过（真实 nginx HTTPS + `underscores_in_headers` A/B 对照） |
| `DB_ENGINE=mysql python tests/manual/t39_pi_agent_flow.py` | **全部通过**：真实 `pi` Agent 进程（`pi -p` + 一段提示词）在真实 MySQL 与两套真实 API 上完成「查任务 → 领取 → 产出交付物 → 上传 → 提交」，并由主管审核闭环到 DONE |
| `pnpm exec vitest run` | 14 files / 170 tests passed |
| `pnpm exec playwright test` | 31 passed（真实后端，逐例控制台零报错） |
| `pnpm exec vue-tsc --noEmit` / `pnpm build` | 0 错误 / 构建成功 |

## 5 卡片点名的验收选择器执行结果

PLAN 的各任务卡把 `pytest -m "<选择器>"` 写成验收命令，因此每个选择器都必须命中真实用例（非零、非全跳过）并记录实际数量。全部选择器均已实际执行：

| 选择器 | 后端 | 最近一次结果 |
| --- | --- | --- |
| `-m "unit"` | sqlite | 49 passed, 1 skipped |
| `-m "api"` | sqlite | 316 passed, 5 skipped |
| `-m "identity"` | sqlite | 31 passed, 1 skipped |
| `-m "projects"` | sqlite | 79 passed, 1 skipped |
| `-m "workflow"` | sqlite | 161 passed, 3 skipped |
| `-m "claim"` | sqlite | 58 passed, 2 skipped |
| `-m "timeout"` | sqlite | 26 passed, 1 skipped |
| `-m "review"` | sqlite | 17 passed, 1 skipped |
| `-m "files"` | sqlite | 73 passed, 1 skipped |
| `-m "dashboard"` | sqlite | 14 passed |
| `-m "jobs"` | sqlite | 29 passed, 2 skipped |
| `-m "unit and workflow"` | sqlite | 11 passed, 1 skipped |
| `-m "unit and timeout"` | sqlite | 1 skipped |
| `-m "unit or api"` | sqlite | 346 passed, 6 skipped |
| `-m "unit or api"` | mysql | 352 passed |
| `-m "fault or timeout"` | sqlite | 55 passed, 2 skipped |
| `-m "fault or timeout"` | mysql | 57 passed |
| `-m "api and (identity or workflow or claim or timeout or review or files or dashboard)"` | sqlite | 271 passed, 4 skipped |
| `-m "api and (identity or workflow or claim or timeout or review or files or dashboard)"` | mysql | 275 passed |
| `-m "unit or api or identity or projects or workflow or claim or timeout or review or files or dashboard or jobs or mysql or concurrency or fault or deployment"` | sqlite | 387 passed（T38 全量 OR 选择器） |
| `-m "fault and api"` | sqlite | 14 passed |
| `-m "api and files"` | sqlite | 65 passed |
| `-m "files and timeout"` | sqlite | 4 passed |
| `-m "files and fault"` | sqlite | 3 passed |
| `-m "projects and files"` | sqlite | 20 passed |
| `-m "files and workflow"` | sqlite | 22 passed |
| `-m "api and timeout"` | sqlite | 9 passed |
| `-m "timeout and workflow"` | sqlite | 20 passed, 1 skipped |
| `-m "claim and timeout"` | sqlite / mysql | 2 passed / 2 passed |
| `-m "fault and claim"` | sqlite / mysql | 1 skipped（该用例为 MySQL 专属）/ 1 passed |
| `-m "workflow and files"` | sqlite | 22 passed |
| `-m "jobs and fault"` | sqlite | 21 passed |
| `-m "mysql and fault and claim"` | mysql | 1 passed |
| `-m "mysql and concurrency and workflow"` | mysql | 3 passed |
| `-m "unit and identity"` | sqlite | 4 passed |
| `-m "unit and deployment"` | sqlite | 24 passed |
| `-m "api and review"` | sqlite | 17 passed, 1 skipped |
| `-m "api and claim"` | sqlite | 58 passed, 1 skipped |
| `-m "api and workflow"` | sqlite | 133 passed, 2 skipped |
| `-m "api and projects"` | sqlite | 79 passed, 1 skipped |
| `-m "api and dashboard"` | sqlite | 14 passed |
| `-m "api and deployment"` | sqlite | 7 passed |
| `-m "api and identity"` | sqlite | 31 passed, 1 skipped |
| `-m "identity and dashboard"` | sqlite | 2 passed |
| `-m "identity and fault"` | sqlite | 4 passed |
| `-m "dashboard and identity"` | sqlite | 2 passed |
| `-m "dashboard and timeout"` | sqlite | 2 passed |
| `-m "review and claim"` | sqlite | 3 passed |
| `-m "review and timeout"` | sqlite | 3 passed |
| `-m "review and files"` | sqlite | 2 passed |
| `-m "files and jobs"` | sqlite | 8 passed |
| `-m "projects and workflow"` | sqlite | 54 passed |
| `-m "mysql"` | mysql | 13 passed |
| `-m "mysql and deployment"` | mysql | 7 passed |
| `-m "mysql and concurrency"` | mysql | 9 passed |
| `-m "mysql and concurrency and claim"` | mysql | 2 passed |
| `-m "mysql and concurrency and review"` | mysql | 1 passed |
| `-m "mysql and concurrency and timeout"` | mysql | 1 passed |
| `-m "mysql and concurrency and identity"` | mysql | 1 passed |
| `-m "mysql and concurrency and jobs"` | mysql | 2 passed |
| `-m "mysql and concurrency and fault"` | mysql | 1 passed |
| `-m "mysql and concurrency and projects"` | mysql | 1 passed |
| `-m "mysql and fault"` | mysql | 1 passed |
| `-m "deployment and files"` | mysql | 1 passed |

`-m "fault and claim"` 与 `-m "unit and timeout"` 的单个用例是 MySQL 专属（跨连接时钟边界），在 SQLite 上由共享 conftest 钩子跳过、在 MySQL 上执行（1 passed），因此该选择器在两个后端都不是空执行。

**在 MySQL 上补跑复合选择器时发现的真实缺陷**：`GET /api/agent/v1/projects` 与 `/projects/{id}` 在 MySQL 上必然 500。读取路径调用了写路径的 `load_actor_agent_locked`（内部 `SELECT ... FOR UPDATE`），而读取路径没有事务——SQLite 会静默忽略行锁，MySQL 直接抛 `TransactionManagementError`。现拆分为 `load_actor_agent_locked`（写路径，要求事务内）与 `load_actor_agent_for_read`（读路径，同样的 enabled 与 Key 版本校验但不取行锁），项目读取助手改用后者。此前 SQLite 全绿掩盖了它，只有把复合选择器放到 MySQL 上跑才暴露。

**本轮修复的另一个真实缺陷**：新增 `-m "mysql and concurrency and jobs"` 用例时发现派发预占不是条件更新——两个派发器会同时选中同一事件并各自发布一次（实测 `[1, 1]`）。现改为逐事件条件预占（过滤 `claimed_at IS NULL OR claimed_at < 租期`），后到的更新匹配不到行，`-m "mysql and concurrency and jobs"` 2 passed 且 `attempts == 1`。

## 6 真实 Agent 进程的全流程验收（非 mock）

`tests/manual/t39_pi_agent_flow.py` 把执行者换成真实的 Agent 进程：脚本只准备真实环境与真实数据（项目、主管/员工 Agent、真实 Key、已发布的 L2 待办任务），然后把**一段提示词**交给 `pi -p`，由它自己用 curl 调 AgentAPI 完成：

1. `GET /read_task/?l1=1&l2=1` 查看是否有任务（读取 items/total）；
2. `POST /get_task/?l1=1&l2=1` 只领取一项，并从响应读取 task_id、execution_id、display_number、version、execution_deadline_at；
3. 在工作目录创建 `deliverable.md` 并计算 sha256；
4. `POST /tasks/{id}/attachments?purpose=EXECUTION_ARTIFACT&execution_id=...` 上传（取 `data.file_id`），再 `POST /tasks/{id}/submit` 正式提交；
5. 输出一行 `PI_RESULT:` JSON 供脚本与人工核对。

提示词不替 pi 填写任何业务字段，所有 ID 都由它从真实响应中读取。测试回读数据库断言：执行、提交各恰好一条，执行以 SUBMIT 结束，提交摘要是 pi 的真实产出且含 `PI-AGENT-DONE`，**产物摘要与磁盘文件摘要一致**，产物已冻结绑定到该提交且标记为已绑定（不会被临时文件清理）；最后由主管经真实 AgentAPI 审核通过，任务闭环到 DONE。

**提示词的唯一事实源是 [README.md](../README.md) §一**（位于 `AGENT-PROMPT` 标记之间），脚本运行时从 README 读取，不保留私有副本——因此文档写给其它 Agent 的内容，就是被本验收实际执行的版本；README 缺少标记或缺少领取/提交步骤时脚本直接拒绝执行。

这不是替代物而是补充：mock 能验证代码分支，只有真实 Agent 进程 + 真实数据库才能证明「一个外部执行者只靠一份提示词就能走完整条契约链路」。

## 7 主线程签收记录

签署人：主线程（契约与共享基础所有者）。审查依据：实际差异、上述命令的真实输出、以及 `docs/DELIVERY-STATUS.md` 中记录的未运行项。

**签收结论：首期功能闭环通过，交付门禁 G1–G5 全部达成。**

- **G1 基础**：工程、迁移（两库空库）、统一错误信封、身份来源、数据库时钟与共享服务均有命令级证据。达成。
- **G2 身份与上下文**：Key 生命周期、项目授权、最后主管保护（含真实并发）、文档版本与文件访问矩阵均有用例证据。达成。
- **G3 执行**：等级列表、全局领取（跨两个真实实例）、容量、固定超时、提交审核与返工在真实 MySQL 上通过。达成。
- **G4 Web**：五列看板真实动作、详情、排序、Agent 页、项目页、总览与活动页均在真实浏览器中通过且控制台零报错。达成。
- **G5 恢复与部署**：Outbox 调度与失败恢复、锁退出、备份恢复演练、容量性能实测、真实 nginx HTTPS 代理链路（含 `underscores_in_headers` 的 A/B 对照）、启动期配置校验（T02）与认证后写权限竞态（T09）均通过。达成。

**保留的门禁与理由**：无未达成的交付门禁。仍存**未验证细节**（不构成门禁失败）：本地代理使用自签名证书且只验证了 `127.0.0.1` 入口，未覆盖公网 CA 证书与真实 DNS；单实例 MySQL 与本地文件目录下的备份恢复演练不覆盖跨机共享存储。

**制作期间的缺陷记录**：共 13 项，逐条列于 `docs/DELIVERY-STATUS.md` §3。其中 4 项只有真实浏览器用例能暴露（Web 代提交必然 409、附件被重复计数、两处占位视图断言过期）；5 项只有真实 MySQL 能暴露（`__length` 约束不可用、测试缺少事务边界、锁等待被进程内重试放大、并发派发器重复发布同一事件、Agent 读取项目列表在无事务下取行锁而失败）。这 5 项在 SQLite 上全部静默通过，说明双数据库与真实浏览器的验收不是形式要求。
