# Windows CMD 本地运行

默认入口为根目录 `start.cmd`，不调用 Docker。PostgreSQL 16 和 NebulaGraph 3.8.0 在独立的 Ubuntu WSL2 中直接运行；FastAPI 和静态页面服务在 Windows 的项目 Python 虚拟环境中运行。

## 使用

```cmd
start.cmd
status.cmd
stop.cmd
```

也可以双击这些文件。启动完成后自动打开个人信审工作台。`status.cmd` 输出各服务状态；`stop.cmd` 先正常关闭页面和后端，再停止本项目数据库与 WSL 保活进程，不关闭整个 WSL 发行版，也不删除数据。

自动化或不希望打开浏览器时：

```cmd
start.cmd --no-browser --no-pause
stop.cmd --no-pause
```

旧的 `scripts/start-windows.cmd` 和 `scripts/stop-windows.cmd` 已转到同一个本地运行入口。`scripts/setup-windows.cmd` 可以提前准备依赖，完成后仍需 `start.cmd` 启动服务。

## 首次准备

需要 Windows x64、Python 3.10+、WSL2 和独立的 Ubuntu 24.04（不能使用 Docker Desktop 自带的发行版）。这台开发电脑已具备 Python 和 Ubuntu-24.04 WSL2。新电脑缺少 WSL 时，先在管理员终端执行并按系统要求重启：

```cmd
wsl --install -d Ubuntu-24.04
```

首次启动需要联网下载 Python 依赖、Ubuntu PostgreSQL 16 软件包和官方 NebulaGraph 发布包。NebulaGraph 归档使用代码中固定的 SHA-256 校验，安装到 WSL `/opt/credit-ontology-mvp/nebula-3.8.0`。后续启动复用本地依赖，不要求 Docker 和外部模型在线。

启动器自动执行：

1. 创建或复用 `.venv`，补齐 `backend/requirements.txt` 的依赖。
2. 首次生成 `.env.local`，从既有 `.env`（不存在时使用 `.env.example`）继承业务设置。
3. 启动 WSL 保活进程和本项目专用 PostgreSQL、Nebula Meta/Storage/Graph。
4. 仅在空业务数据库中导入仓库种子；按记录应用结构迁移 002、005；不自动导入额外手测数据集 003、004、006。
5. 等待 Nebula Storage `ONLINE`，首次轮换 root 凭据、初始化图空间。空图首次通过现有审核/发布门禁发布确定性基线；已有版本和图数据不被重新发布或覆盖。
6. 启动后端、校验映射，启动页面服务，全部就绪后打开浏览器。

重复启动会复用已就绪的项目进程。端口被其他应用占用时明确报错，不结束无关进程。启动失败时尽量只停止本轮新增进程，数据和日志保留。

## 地址

| 服务 | 地址 |
|---|---|
| 个人信审 Agent 工作台 | http://127.0.0.1:8080/credit-review.html |
| 本体建模后台 | http://127.0.0.1:8080 |
| 兼容工作台 | http://127.0.0.1:8080/workbench.html |
| 后端 API | http://127.0.0.1:8000/docs |
| PostgreSQL | 127.0.0.1:5433 |
| NebulaGraph | 127.0.0.1:9669 |

Web、PostgreSQL 和 Nebula HTTP 状态端口绑定 loopback；Nebula RPC 运行在 WSL 网络空间，Windows 应用通过本机转发访问，并启用密码认证。部分 WSL2 环境只转发 Nebula 的 IPv6 端口，启动器会按需增加仅监听 `127.0.0.1` 的 IPv4→IPv6 桥接进程，不修改系统端口代理或防火墙。本地页面服务直接提供 `frontend` 文件，不依赖 Nginx；浏览器通过已有 CORS 和 HttpOnly 本机会话访问 8000 端口。

## 配置与数据

- `.env.local`：本地唯一配置源，包含本机地址、业务模式和私密凭据，已被 Git 忽略。默认 `MVP_AUTH_MODE=local_session`、`MVP_LOCAL_SESSION_TRUST_PROXY=false`，匿名旁路关闭。占位或默认数据库口令会替换为随机值；既有有效模型管理加密设置和 MVP Key 尽量继承。
- `MVP_WSL_DISTRO`：默认 `Ubuntu-24.04`。其他发行版必须自行提供兼容的 PostgreSQL 16 与 Python 运行环境；自动安装流程按 Ubuntu 24.04 验证。
- `MVP_LOCAL_INSTANCE_ID`：区分项目实例。已初始化后不要随意修改。
- `NEBULA_REQUEST_TIMEOUT_MS`：图数据库连接和请求超时，默认 10000 毫秒，可配置范围 1000–60000 毫秒。数据库访问在后台线程执行，慢查询不会占用 API 事件循环。
- `.runtime/state.json`：本项目进程 PID、Windows 创建时间、停止标记和非敏感运行状态；不保存数据库/模型口令。创建时间校验防止 PID 被复用后误停无关进程。
- `.runtime/logs/backend.log`、`frontend.log`、`wsl-setup.log`、`launcher.log`：页面、后端、数据库准备及启动器诊断日志；按需桥接的日志在 `graph-bridge.log`。
- WSL `/var/lib/credit-ontology-mvp/<实例ID>/postgres`：独立 PostgreSQL 数据，不使用发行版默认 main 集群。
- WSL `/var/lib/credit-ontology-mvp/<实例ID>/nebula`：图谱数据、配置、日志和 PID。实际目录可用 `status.cmd` 查询。

修改 `.env.local` 后先执行 `stop.cmd`，再运行 `start.cmd`。已经运行的后端不会自动读取磁盘上的新配置。已有数据库口令不能只改配置文件；应按数据库正常口令轮换流程处理。NebulaGraph 3.8 的密码最长 24 个字符，启动器为新图数据库生成 24 字符随机值。

停止 WSL PostgreSQL 前会核对可执行文件、进程用户与数据目录，并通过 Linux pidfd 向已验证的进程发送停止信号；遗留 PID 文件和 PID 复用不会授权停止无关进程。

## 八 Action 验收

本地入口复用既有安全边界，不会自动把外部服务切为沙箱。合成业务验收要求 `.env.local` 的以下四项均为 `sandbox`：

```dotenv
CREDIT_PROVIDER_MODE=sandbox
CREDIT_CONSENT_MODE=sandbox
FRAUD_PROVIDER_MODE=sandbox
PHONE_TASK_DISPATCH_MODE=sandbox
```

重启后执行：

```cmd
.venv\Scripts\python.exe scripts\action_chain_uat.py prepare
.venv\Scripts\python.exe scripts\action_chain_uat.py run
.venv\Scripts\python.exe scripts\action_chain_uat.py status
```

验收脚本直接通过 psycopg 连接本地数据库、通过工作流 API 执行业务 Action，已去掉 Docker CLI 依赖。它读取启动器记录的运行模式，并拒绝磁盘配置与运行中配置不一致的情况。

## 与旧 Docker 环境的关系

旧 `.env`、Compose 文件和 Docker 数据卷保留，可继续作为另一套部署。两套环境的数据独立；本次不会自动迁移旧 Docker 中的业务记录、模型配置或本体历史。需要原有数据时应先从旧 PostgreSQL 备份并恢复到本地集群，再通过已发布本体重建/同步图谱；不要将 Linux 数据目录直接复制到不同版本的数据库中。

两套环境不能同时占用相同的 Web/Nebula 端口。`stop.cmd` 只停止本地运行环境；旧 Docker 容器仍由原部署工具管理。
