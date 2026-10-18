# Harness看板（HarnessKanban）WebUI 设计规范

本文件把 [docs/product-design.md](../docs/product-design.md) 第 3、6、9 节的界面要求落成可执行的视觉与交互规范。
页面实现必须同时满足：**本文件 + 产品设计文档**。产品设计文档是契约，本文件只规定「长什么样、怎么组织」。


> **2026-10 首页重构（本文件部分内容仍描述旧设计系统）**
>
> 首页已按 `docs/html/demo-light.html`（浅色）与 `docs/html/demo-dark.html`（深色）重做，
> 规范如下；旧设计系统（`styles/components.css` + `styles/tokens.css`）仅继续服务
> 全局看板 / Agent 管理 / 活动日志等二级页面，且其令牌已映射到新配色。
>
> - **主题**：`styles/demo.css` 由 `scripts/extract_demo_css.py` 从两版 demo 提取；未加前缀的规则是浅色，
>   `html[data-theme="dark"]` 是深色覆盖。`stores/theme.ts` 负责 `data-theme` 与
>   `localStorage['harvesstask.theme']`（默认浅色）。主题只换材质，**不换布局**：
>   `styles/app.css` 第 8 节把深色 demo 的漂浮顶栏 / 横版项目卡片拉回与浅色一致。
> - **首页结构**：`/` = 任务，`/projects` = 项目，顶部胶囊标签切换；顶栏右侧是
>   小分队入口、「更多」（全局看板 / Agent 管理 / 活动）、主题切换、**半透明设置按钮**。
> - **任务模块**：默认 `status=ALL` + 全部项目；筛选支持项目 / 难度 / 领取模式 / 执行者头像 /
>   关键词 / 含归档；卡片点开是任务详情浮窗（要求、验收标准、执行、提交、审核、附件、时间线、动作）。
> - **拖拽**：卡片可拖动换列，跨列只允许服务端动作表内的转换（准备中→待办=发布、待办→进行中=领取、
>   进行中→待验收=提交、待验收→已完成/待办=审核通过/驳回、已完成→待办=重开、待办→准备中=撤回、
>   进行中→待办=回收）；需要填写的动作会打开对应表单，非法列不高亮、落下后提示拒绝。
>   列内拖动调用 `POST /projects/{id}/task-order` 保存顺序（锚点语义：`before_task_id`=左邻居）。
> - **项目模块**：状态胶囊（含计数，含「已删除」档）+ 搜索 + 卡片网格；详情浮窗含背景、文档、
>   成员、进度、生命周期与删除；封面配色/emoji 是本机外观偏好（`localStorage['harvesstask.project-skins']`）。
> - **设置浮窗**：LLM（base_url / api_key / model / temperature / max_tokens / system_prompt）
>   与环境变量（分组目录 + 自定义项，密文只写不读）；数据存 `backend/var/runtime_settings.json`，
>   接口 `GET/PUT /api/web/v1/settings`、`POST /api/web/v1/settings/llm/test`。
> - **控件约定**：五列看板的列头/筛选条/卡片沿用 demo 的 `.column` / `.people-filter` / `.search` /
>   `.task-card`；新组件只使用 demo 类名；旧版确认框（`.dialog-backdrop`）层级抬到首页浮窗之上。
> - **测试**：`tests/unit/home-*.spec.ts`、`settings-and-theme.spec.ts`；
>   `tests/e2e/{smoke,tasks,projects,agents,observability,settings,cross_api}.spec.ts`
>   （真实服务、无业务 mock，每个用例断言 console 无 error）。

## 1 设计令牌

全部视觉数值来自 `src/styles/tokens.css`，组件只引用 CSS 变量，不写死色值、字号、间距。

| 类别 | 变量 | 用途 |
| --- | --- | --- |
| 画布 | `--color-canvas` | 应用背景 |
| 表面 | `--color-surface` / `--color-surface-muted` / `--color-surface-sunken` | 面板 / 表头与底栏 / 内嵌块 |
| 边框 | `--color-border` / `--color-border-strong` | 分隔 / 输入与按钮描边 |
| 文本 | `--color-text` / `--color-text-secondary` / `--color-text-muted` | 主 / 次 / 弱 |
| 品牌 | `--color-primary` 等 | 主按钮、选中态、链接 |
| 语义 | `--color-success` / `--color-warning` / `--color-danger` / `--color-info`（各带 `-soft`、`-border`） | 结果提示、异常 |
| 五态 | `--status-*`（由 `[data-status]` 注入 `--status-color/-soft/-border`） | 看板列、状态徽标 |
| 五级难度 | 由 `[data-difficulty="1..5"]` 注入 `--difficulty-*` | 难度徽标 |
| 间距 | `--space-1..--space-10` | 一切留白 |
| 圆角 | `--radius-xs..--radius-xl`、`--radius-pill` | 控件 / 面板 / 胶囊 |
| 阴影 | `--shadow-xs/sm/md/lg/drag` | 面板 / 悬浮 / 弹窗 / 拖拽浮层 |

状态色永远与文字同时出现：颜色只是辅助，文字必须能独立表达（product §3.2）。

## 2 共享类（`src/styles/components.css`）

页面只组合这些类，不各自造外观：

- 骨架：`.page`、`.page-head`、`.page-head__text`、`.page-title`、`.page-subtitle`、`.page-actions`、`.section`、`.section-head`、`.section-title`、`.section-hint`
- 容器：`.panel`、`.panel-head`、`.panel-title`、`.panel-body`、`.panel-body--tight`、`.panel-foot`
- 按钮：`.btn`（次级）、`.btn--primary`、`.btn--ghost`、`.btn--danger`、`.btn--sm`、`.btn--xs`、`.btn--block`、`.btn-group`
- 徽标：`.badge`、`.badge--neutral/primary/success/warning/danger/info`、`.badge--mono`、`.badge--status`（配 `data-status`）、`.badge--difficulty`（配 `data-difficulty`）、`.chip`、`.chip--warn/danger/success`
- 头像：`.avatar`、`.avatar--muted`、`.avatar--lg`（取名字首字）
- 元数据：`.meta-list`（`dl` + `dt`/`dd` 两列）
- 表格：`.table-wrap`、`.table`、`.table__num`
- 表单：`.form-grid`、`.form-grid--2`、`.field`、`.field-label`、`.field-hint`、`.field-required`、`.input`、`.select`、`.textarea`、`.checkbox-line`
- 工具栏：`.toolbar`、`.toolbar__field`、`.toolbar__label`、`.toolbar__spacer`、`.toolbar__summary`、`.filter-count`
- 页签：`.tabs`、`.tab`、`.tab--active`
- 提示：`.notice`、`.notice--info/success/error/warning`
- 统计：`.stat-grid`、`.stat`、`.stat__label`、`.stat__value`、`.stat__hint`
- 状态块：`.state-block`、`.state-title`、`.state-description`
- 工具：`.row`、`.row--between`、`.row--wrap`、`.stack`、`.grid-2`、`.grid-3`、`.muted`、`.mono`、`.tabular`、`.truncate`、`.text-sm`、`.text-xs`

## 3 应用外壳

- 左侧固定深色侧栏（`--nav-bg`）：品牌区 + 三组导航（概览 / 管理 / 审计）+ 页脚说明。
- 右侧：置顶栏（当前页名 + 返回项目入口 + 本机说明）与内容区；内容区最大宽度 `--content-max` 并居中。
- 窄屏（≤860px）侧栏折叠为顶部横向导航，五个入口必须仍然可见可点（A25）。
- 导航入口与顺序固定为 `NAV_ENTRIES`（全局看板 / Agent 管理 / 项目管理 / 任务看板 / 活动），不得增删。
- 项目上下文记忆（`stores/projectContext.ts`）：进入项目看板或展开项目详情时记住「最近项目」（localStorage 持久化）。
  离开到全局页面后，置顶栏与任务看板/活动页页头显示「返回项目看板：<项目名>」；「项目管理」页回到列表时自动恢复上次打开的详情。
  UI 共享组件不得借此持久化任何密钥或业务数据（见测试的 localStorage 约束）。

## 4 页面级要求

### 4.1 公共交互（product §3.2）

1. 每个列表/看板都有加载态、空态、错误态与重试入口；失败**绝不**渲染成「暂无数据」。
2. 提交期间相关按钮禁用，防止重复点击。
3. 所有状态同时以文字表达。
4. 修改失败保留已填写内容，并展示可理解的错误原因（错误码 + 重试入口）。
5. 日期默认按 Asia/Shanghai 显示并标明时区，UTC 原值在需要时可查。
6. 拖拽不是唯一操作方式：卡片必须同时提供按钮入口。

### 4.2 全局看板（product §9）

分区顺序固定：概览计数 → 任务分布（五态 + 五难度）→ 执行与容量 → Agent 状态 → 待处理异常 → 项目状态 → 最近活动。
每个计数块标明统计范围与 `as_of`，空范围显示「无任务」而不是 100%。

### 4.3 任务看板（product §6.3 / §6.4 / §6.6）——重点

必须呈现 **Trello 式可拖拽看板**：

- 五列固定顺序「准备中 / 待办 / 进行中 / 待审核 / 已完成」，列头包含状态名、任务计数徽标、颜色标识；列体纵向可滚动，列高一致。
- 卡片信息层次：编号（等宽）→ 标题（主视觉，最多两行截断）→ 徽标行（难度、领取模式、异常）→ 执行者（头像 + 姓名）→ 截止信息 → 附件数量。卡片可拖拽（`draggable`），hover 有抬升反馈。
- 拖拽反馈：被拖卡片半透明，目标列高亮，插入位置有落点指示（占位线/占位块）。
- 拖到其他列 = 发起一次业务操作：弹出对应业务表单；非法转换拒绝并把卡片回退到服务端状态；取消不发送动作。
- 同列拖拽 = 列内排序，调用排序端点。
- 列内滚动、横向滚动在窄屏可用；窄屏按列切换并提供按钮式状态操作。
- 卡片上的动作按钮保持可见（键盘与窄屏入口），但视觉上收在卡片底部工具条，不与标题竞争。

### 4.4 详情弹窗（product §6.6）

分区：任务要求 → 验收标准 → 领取与执行 → 参考上下文 → 历史结果与审核 → 附件 → 活动时间线。
按当前状态限制编辑入口；主管可在同一弹窗内发布、回收、审核、重新打开。

## 5 硬约束

- **不得删除或改名任何 `data-testid`**，也不得删除 `data-task-id` / `data-status` / `data-display-number` 属性：自动化用例与无障碍定位依赖它们。
- 现有面向用户的固定文案（如「服务端判定」「等待回收」「统计时刻」「尚未选择项目」）不得改写。
- 不引入新的运行时依赖；只用 Vue 3 + 现有依赖。
- 不写 `localStorage` / `sessionStorage`。
- 每个页面的根元素保留其 `data-testid`（`view-dashboard`、`view-agents`、`view-projects`、`view-board`、`view-activity`、`view-tasks`）。
- 窄屏与键盘路径必须与鼠标路径等价。
