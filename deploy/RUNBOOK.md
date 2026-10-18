# 分布式/正式环境启动顺序（DEVELOP D19，product 17.3）
#
# 迁移由单一部署步骤执行，不由每个 API 进程在启动时争相执行。
# WebAPI、AgentAPI、Worker、Beat 共用同一写库与同一私有持久文件目录；
# Redis 只作为队列与限速，不是业务事实源。

## 1 准备
#   DB_ENGINE=mysql、MYSQL_* 指向同一写库（禁止按 API 拆分数据库配置）
#   FILE_STORAGE_ROOT 指向共享持久目录（多实例必须共享，不能各用容器临时目录）
#   REDIS_URL、ALLOWED_HOSTS、ALLOWED_ORIGINS 按部署环境设置
#   秘密只通过部署环境变量注入，不提交数据库密码或 Agent Key

## 2 迁移（唯一一次）
cd backend
python -m pip install -e ".[dev]"
python manage.py migrate
python manage.py check
# 迁移完成后核对全局领取锁恰有一行；缺失必须中止发布。
python manage.py shell -c "from apps.tasks.models import TaskClaimLock; assert TaskClaimLock.objects.filter(id=1).count() == 1, '缺少全局领取锁行'"

## 3 两个 API
python manage.py runserver 127.0.0.1:8000          # 仅本机 WebAPI（开发）
python -m uvicorn agentapi.main:app --host 127.0.0.1 --port 8001

## 4 异步（每台机器一个 Beat，Worker 可多实例）
python -m celery -A config.celery_app:app worker --loglevel=INFO
python -m celery -A config.celery_app:app beat   --loglevel=INFO

## 5 代理
#   把 deploy/nginx/harvesstask.conf 部署到代理并 reload。
#   验收：远端经 HTTPS 携带 AGENT_TOKEN 可鉴权；远端访问 /api/web/v1 不可达；
#   本机 WebUI 无需登录。

## 6 备份与恢复
#   数据库与文件必须成对备份（只备份数据库无法恢复附件）。
#   恢复顺序：数据库 → 文件 → migrate → 扫描未处理事件与超时执行。

## 7 已部署实例：task.opcdesk.top（wwg / 8.147.60.121）

- **代码**：`/srv/harvesstask/backend`（Python 3.12，`/srv/harvesstask/venv` 由 uv 管理）
- **前端**：`/srv/harvesstask/frontend/dist`（本地 `pnpm build` 后 rsync 上传）
- **配置**：`/srv/harvesstask/backend/.env`（SQLite `/srv/harvesstask/data/harvesstask.sqlite3`，上传文件 `data/files`）
- **服务**（systemd）：
  - `harvesstask-web` → `127.0.0.1:8010`（Django ASGI：WebAPI + Django admin）
  - `harvesstask-agent` → `127.0.0.1:8011`（FastAPI AgentAPI）
- **nginx**：`/etc/nginx/conf.d/task.opcdesk.top.conf`（复用 `*.opcdesk.top` 通配符证书）
  - `/` → SPA；`/api/web/v1/` → 8010；`/admin/` → 8010；`/api/agent/v1/` → 8011；`/static/` → `backend/staticfiles`
  - Agent 请求头 `AGENT_TOKEN` 含下划线，需 `underscores_in_headers on`
- **Django admin**：`https://task.opcdesk.top/admin`（领域模型只读；用户/组可管理；超管 `admin`）
- **访问密钥**：`/srv/harvesstask/backend/secret/webui_access_key`（600，属主 harvesstask）。
  配置后 WebUI 先登录，WebAPI 无密钥返回 403；轮换 = 改这个文件 + 重启 `harvesstask-web`（旧 Cookie 立即失效）。
- **更新发布**：
  1. 本地 `cd frontend && pnpm build`
  2. `rsync -az --delete --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.env' --exclude 'var/' --exclude 'tests/' --exclude 'staticfiles/' --exclude 'secret/' backend/ wwg:/srv/harvesstask/backend/`
  3. `rsync -az --delete frontend/dist/ wwg:/srv/harvesstask/frontend/dist/`
  4. `ssh wwg 'chown -R harvesstask:harvesstask /srv/harvesstask && systemctl restart harvesstask-web harvesstask-agent && nginx -s reload'`
- **注意**：
  - 该实例未安装 Redis / Celery，超时扫描与 outbox 投递不运行；需要时安装 Redis 后补 `celery worker/beat` 的 systemd 单元。
  - WebAPI 与首页按产品设计**没有登录**（本机信任模型）。域名对外后，任何人拿到地址都能管理数据；
    对外发布前建议在 nginx 上为 `/` 与 `/api/web/v1/` 加 Basic Auth 或改回仅回环访问。
