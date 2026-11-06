# MaxKB 知识治理工作台

前后端分离，登录后可切换“知识入库 / 重复片段”。知识入库支持 MinerU、文件夹上传、映射目录、SSH 同步和 MaxKB 导入；重复片段模块选择 MaxKB 数据源中的一个或多个知识库，读取已入库正文向量，精确扫描相似 chunk，聚合并展示完整段落对，保存原文快照与人工处理历史。

完整需求和边界见 [PLAN.md](PLAN.md)。本目录的系统不修改 MaxKB 项目代码。

## 安装目录、配置和工作目录

安装模板统一放在 `install/.env.example`，安装脚本只从这里读取，不再从项目根目录读取模板。`docker/images/` 存放离线镜像包，Compose / Dockerfile 负责构建和运行。

```text
安装包目录/
├── install/
│   ├── .env.example        # 配置模板，先设置 WORK_DIR、数据库等参数
│   └── install.sh          # 创建工作目录、生成配置、加载镜像、启动服务
└── docker/
    ├── Dockerfile          # FastAPI 直接提供前端页面及接口
    ├── compose.yaml        # 运行模板，安装时复制到应用工作目录
    └── images/             # 应用和 PostgreSQL 镜像包
```

编辑 `install/.env.example` 中的工作目录，例如：

```dotenv
WORK_DIR=/opt/knowledge-workspace
```

脚本在该目录下创建名为 `maxkb-remove-duplicates` 的应用目录：

```text
/opt/knowledge-workspace/
└── maxkb-remove-duplicates/
    ├── .env                # 从 install/.env.example 生成的实际运行配置
    ├── compose.yaml        # 实际运行配置，后续在这里启动、停止服务
    ├── postgres/           # PostgreSQL 数据
    ├── ingestion/          # 入库原文件、解析产物和日志
    └── documents/          # 网页“映射”来源，放置待入库文档
```

模板默认 `WORK_DIR=./work`，相对路径按**安装包根目录**解析，不依赖执行脚本时所在目录。默认生成路径为 `安装包目录/work/maxkb-remove-duplicates/.env`。也可以设置绝对路径；路径不会展开 `$HOME` 或 `~`，不要写成 Shell 表达式。

生成的 `.env` 会包含脚本计算的绝对 `WORK_DIR` / `INSTALL_DIR`。实际 `compose.yaml` 与 `.env` 同目录，自动读取它，并使用相对路径挂载三个数据子目录。安装完成后，日常运维不再依赖安装包目录或源码路径。已有工作目录的 `.env` 不覆盖密码或密钥；后续修改实际配置请编辑这个文件。

## 数据库配置与初始化

部署两个容器：PostgreSQL 服务名称为 `postgres`，前后端应用服务名称为 `app`。模板数据库配置：

```dotenv
DB_HOST=postgres
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=postgres123!@#
DB_NAME=maxkbremove
```

Compose 按 `DB_USER`、`DB_PASSWORD`、`DB_NAME` 设置 PostgreSQL 的初始化环境变量，空数据目录首次启动时自动创建账号和数据库；数据库健康检查通过后启动应用，由程序自动初始化业务表和管理员。无需单独的 `pgsql.conf` / `pg.sql`。

已有数据目录不会因修改配置而重置账号密码。Web 管理员账号由 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 首次创建，与数据库账号不同。模板中的加密密钥和管理员密码占位符由安装脚本替换为随机值，不会打印到日志。

## 安装与启动

目标机器需要 Bash、Docker Engine、Docker Compose V2 和常见系统命令 `base64`、`od`、`head`。不需要安装 Python、Node 或 PostgreSQL。

1. 将 `install/` 和 `docker/` 目录复制到目标机器，保持二者为同级目录。
2. 把应用镜像和 `postgres:16` 镜像包放进 `docker/images/`，支持 `.tar`、`.tar.gz`、`.tgz`；一个包可包含多个镜像。
3. 编辑 `install/.env.example`，配置 `WORK_DIR`、数据库密码、Web 端口等。
4. 执行安装脚本：

```bash
bash install/install.sh
```

脚本先创建工作目录和子目录，将实际 `.env` 生成到 `WORK_DIR/maxkb-remove-duplicates/.env`（权限 `600`），并把 Compose 模板复制为同目录的 `compose.yaml`，再加载离线镜像、进入这个服务目录、保留数据并停止旧容器，使用本地镜像启动两个服务，不自动构建或拉取镜像。已有 Compose 内容发生变化时，会先备份为 `compose.yaml.bak`。已有目标 `.env` 会保留；单独修改模板不会覆盖已有运行配置。

只生成 `.env`、`compose.yaml` 和工作目录，不操作 Docker：

```bash
bash install/install.sh --init-env
```

脚本会显示实际配置文件和文档目录路径。浏览器访问 `http://localhost:8080`（按配置中的 `WEB_PORT` 调整），初始管理员密码在生成的 `.env` 中查看。容器启动后，业务表迁移可能仍在进行，可通过日志查看初始化进度。

对外部署时填写正确的 `ALLOWED_ORIGINS`，HTTPS 下设置 `COOKIE_SECURE=true`。备份实际 `.env`，尤其保留 `ENCRYPTION_KEY`。安装脚本不会执行配置文件中的 Shell 内容。

## GitHub Actions 按架构打包

打开仓库 **Actions → Build installation images → Run workflow**，选择：

- `architecture`：`amd64`（x86_64）、`arm64`（ARM 64 位）、`both`（分别生成两个安装包）。
- `image_tag`：镜像版本，默认 `latest`。

工作流在 GitHub 上构建应用镜像，准备同一 CPU 架构的 `postgres:16`，导出到安装包的 `docker/images/`，并附带 `install/`、Compose、README 和构建信息。模板的 `IMAGE_TAG` 会同步为输入的版本。不需要配置镜像仓库账号或上传项目实际 `.env`。

运行完成后，在该次运行的 **Artifacts** 下载对应架构的产物，解压外层 ZIP，再解压其中的 `.tar.gz`。进入解出的安装包目录，修改 `install/.env.example` 中的 `WORK_DIR` 等配置，执行 `bash install/install.sh`。包内另附 SHA-256 校验文件；镜像不能混用 CPU 架构。选择 `both` 时应按目标服务器分别下载对应产物。

构建产物默认保留 14 天，请下载保存。工作流仅手动触发，不会因每次代码上传自动构建。

实现参考 Docker 官方的 [GitHub Actions 多平台构建](https://docs.docker.com/build/ci/github-actions/multi-platform/)与[镜像导出说明](https://docs.docker.com/build/ci/github-actions/share-image-jobs/)。

## 构建和准备镜像包

镜像构建在源码目录完成；工作目录中的 Compose 只负责运行已构建的镜像，不包含对源码的 `build` 路径依赖。

```bash
# 在源码项目根目录执行
docker build -f docker/Dockerfile -t maxkb-remove-duplicates:latest .
docker pull postgres:16

mkdir -p docker/images
docker image save -o docker/images/maxkb-remove-duplicates.tar \
  maxkb-remove-duplicates:latest postgres:16
```

应用镜像版本与配置的 `IMAGE_TAG` 保持一致，默认 `latest`。构建架构应与目标服务器一致；例如 ARM 机器为 x86 服务器构建时设置 `DOCKER_DEFAULT_PLATFORM=linux/amd64`。`.dockerignore` 排除运行配置、默认工作目录、镜像包及依赖缓存，不将密码打入镜像。

后端使用 FastAPI / Uvicorn，直接提供前端构建文件和 `/api` 接口。刷新前端页面路由时回退到 `index.html`；未知 API 和缺失静态文件仍返回 404。应用容器只运行一个 Uvicorn 进程，不使用 Nginx 或 Supervisor。容器端口为 8000，宿主机仍通过 `WEB_PORT`（默认 8080）访问。

已在线构建好镜像、不使用离线包时，先运行 `bash install/install.sh --init-env`，然后直接进入生成的服务目录运行 `docker compose up -d`。

## 目录映射与日常操作

工作目录中的 Compose 使用相对路径进行以下读写挂载（`INSTALL_DIR` 为该服务目录）：

| 宿主机目录 | 容器内目录 | 用途 |
| --- | --- | --- |
| `INSTALL_DIR/postgres` | `/var/lib/postgresql/data` | PostgreSQL 数据 |
| `INSTALL_DIR/ingestion` | `/data/ingestion` | 入库文件和解析产物 |
| `INSTALL_DIR/documents` | `/sources` | 本地文档映射来源 |

将待入库文件放在 `documents/`，网页选择“映射”后手动扫描、选择文件并提交。成功后文件会归档到 `documents/已处理文档/`，目录需要读写和删除权限。不再单独配置 `SOURCE_HOST_DIR`。

安装完成后，直接在新服务目录执行操作。以下示例使用 `WORK_DIR=/opt/knowledge-workspace`：

```bash
cd /opt/knowledge-workspace/maxkb-remove-duplicates

# 启动 / 停止
docker compose up -d
docker compose down

# 状态 / 日志 / 重启
docker compose ps
docker compose logs -f app postgres
docker compose restart

# 修改本目录 .env 后重新创建容器
docker compose up -d --force-recreate
```

无需再指定安装包中的 Compose 文件或 `--env-file`。镜像已导入本机，Compose 设置为不自动拉取镜像；缺少镜像时先导入对应镜像包。停止容器不会删除这些宿主机数据目录。

配置和持久数据保存在宿主机工作目录。原先 Docker 命名卷的数据不会自动迁移到这些新目录，脚本不删除原卷。备份需保存完整的 `WORK_DIR/maxkb-remove-duplicates/`，运行中数据库应采用数据库备份方式。

数据库默认不发布宿主机端口。应用通过内部网络连接 `postgres`；连接宿主机 MaxKB 源数据库时，在数据源页面填写 `host.docker.internal`。

按用户要求，本次未执行本机 Docker 构建或运行验证。

## 修改密码

登录后点击右上角头像或用户名，在下拉菜单选择“修改密码”。填写原密码、新密码及确认新密码；新密码至少 12 个字符，且不能与原密码相同。原密码校验通过后保存密码哈希，并撤销该账号所有登录会话，需要使用新密码重新登录。`.env` 中的 `ADMIN_PASSWORD` 仅用于首次创建管理员，不会覆盖已修改的密码。

## 使用

1. 登录 → 数据源 → 添加 MaxKB PostgreSQL 连接 → 测试连接。
2. 扫描任务 → 新建 → 选择数据源 → 加载知识库列表 → 多选知识库 → 设置阈值。
3. 点击“预估并扫描”，查看数量和预计耗时并确认；或开启 Cron 定时（5 段，例如 `0 0 * * *`，默认 Asia/Shanghai）。
4. 扫描记录中查看快照提取、比较进度、相似段落对；失败或取消后可以创建新的重试运行，原记录保留。
5. 成功后查看左右完整段落，标记忽略/已处理，查看整篇文档，或读取当前源文本后提交修改、删除。
6. 操作历史查看修改前、拟修改后、源系统实际文本及操作状态。

知识库选择限定两侧候选集合，不额外强制两侧 knowledge_id 相同。所选知识库必须使用同一 embedding_model_id，且实际向量维度一致。所有已入库正文向量均参与，包括未启用段落；不包含问题向量。可选择排除同一文档，始终排除同一段落。

## 删除扫描记录

“扫描记录”的已完成、失败、已取消运行提供“删除记录”，二次确认后删除本次运行、相似段落对、判定记录和原文快照。扫描任务及其他运行保留，MaxKB 原文和向量不受影响。修改前后文本等操作历史独立保留；扫描删除后的草稿不能再执行。排队/运行中的任务需先取消并等待停止，正在执行或结果待核实的源系统操作需先处理。启动自动执行 `0005` 迁移，无需手动建表。

## MaxKB 管理接口

在“数据源 → 编辑”中配置 API 地址和 Bearer Key。地址可填 `http://maxkb.example.com:18080` 或完整的 `http://maxkb.example.com:18080/admin/api`。Key 可以包含或不包含 `Bearer ` 前缀；后端统一发送 `Authorization: Bearer <key>`。

Key 使用系统 ENCRYPTION_KEY 加密保存，不返回前端。编辑时留空保留原 Key；更换 API 地址必须重新填写 Key，避免凭证发送给新的地址。数据库扫描仍完全只读，不调用这些写接口。

已接入：

- GET `/workspace/{workspace_id}/knowledge/{knowledge_id}/document/{document_id}/paragraph/{page}/30`：按 `data.total`、`data.records` 读取全部分页，提供整篇文档查看，并按段落 ID 找到当前文本。
- PUT `/workspace/{workspace_id}/knowledge/{knowledge_id}/document/{document_id}/paragraph/{paragraph_id}`：提交 `{title, content}`。
- DELETE 同一段落路径：删除单个段落。

整篇文档 DELETE `/workspace/{workspace_id}/knowledge/{knowledge_id}/document/{document_id}` 已接入。只携带 Bearer 认证，不发送 Vue Ref 的 `dep`、`__v_isRef` 等字段；本地 MaxKB 代码确认这些参数未被读取。成功响应要求 `code=200` 且 `data=true`。所有接口同时检查 HTTP 状态和业务 `code=200`。

操作流程：读取 API 最新文本 → 用户编辑/确认删除 → 保存 before/proposed 和待提交记录 → 校验当前内容哈希 → 持久化 executing → 调用源接口 → GET 回读确认 → 保存 actual 和最终状态。文档级数据库锁避免整篇文档删除与本系统其他段落操作冲突。源接口未提供 ETag/条件更新能力，因此无法完全消除其他系统在最后一次检查和写入之间并发编辑的竞态。

超时、连接中断、回读不符标为“待核实”，不自动重发 PUT/DELETE。操作历史的“核实结果”只执行 GET，核对期望状态；重复提交同一成功操作不会再次写源系统。进程中断留下 executing 记录时，同样用核实入口处理。

API 未配置时可通过只读数据库查看当前段落并保留草稿，不能执行源系统写入。修改前/拟修改后/回读实际文本独立保存；段落删除保存删除前正文；整篇文档删除先展示并保存全部分页段落及全文哈希，提交前重新读取全文比较，任一段落增删、顺序或文本变化均需重新核对。文档删除后通过只读 MaxKB 数据库确认文档已不存在，确认后 actual 为空；确认失败标为待核实，不重发 DELETE。成功删除整篇文档后，该数据源下涉及该文档的相似结果标记已处理。历史扫描快照始终保留。

## 相似算法和数据一致性

- 相似度直接在 MaxKB 源数据库的 embedding 表中计算，复用源库已有 pgvector；本系统使用普通 PostgreSQL，不保存或复制向量，也不安装 pgvector。
- 数据范围由任务选择的 knowledge_id 集合决定，两侧均过滤该集合及 source_type='1'。两侧 knowledge_id 不必相等，始终排除同一 paragraph，可排除同一文档。
- 一次运行保持一个只读 REPEATABLE READ 源事务。模型校验、向量维度/非零校验、段落快照提取和全部比较使用同一个连接、同一个数据快照。
- 校验只从源库读取 vector_dims 和 vector_norm，不传输向量本体。遇到模型不兼容、维度不一致、零向量、断裂关联时明确失败。
- 在源库分批遍历 embedding.id，每个锚点与范围内 ID 更小的所有正文向量比较。每对 chunk 只比较一次。LIMIT 仅控制每批锚点数量，不截断匹配候选。
- 计算 `1 - (a.embedding <=> b.embedding)`，按实际浮点分数 `>= threshold` 筛选；没有 ANN 或 Top-K 截断。精确比较仍为 O(N²)。建议安排在每天 00:00 等空闲时段。
- 按无序 paragraph 对去重，取所有 chunk 配对的最高相似度，记录对应 embedding ID，不进行传递合并。
- 相似结果流式写入本系统，事务内 UPSERT 最大分；每次提交校验 后台扫描租约及令牌。扫描成功后才开放人工处理，部分结果明确标识为未完成。
- 源事务断开后不能恢复原快照。因此失败/取消后的“重新扫描”创建新运行，旧参数、部分结果和快照保留，不混用新旧快照。后台扫描租约失效时也按此规则重新执行。
- 默认心跳 15 秒、租约 180 秒、源库每条扫描 SQL 超时 120 秒；批次大小可配置。取消在当前 SQL 返回后的提交边界生效。
- 同任务至多一个排队/执行中的运行，重叠定时触发跳过。午夜仅是新任务 Cron 的默认填写值，启用开关默认关闭，不会自动启动定时任务。
- 后续扫描结果、原文快照和修改前后文本持续保留。本次调整按全新部署处理，不提供旧版向量暂存数据迁移。

## 本地开发与 .env

本系统仅需要普通 PostgreSQL；MaxKB 源库沿用其已有 pgvector 扩展。不要把本系统连接指向 MaxKB 数据库。

API 启动时自动创建缺失的系统数据库、执行迁移并初始化管理员；已有管理员密码不会被覆盖。多进程启动通过数据库锁串行初始化。若数据库不存在，DB_USER 需要 CREATEDB 权限；数据库已存在时只需建表权限。无需手动执行迁移或 bootstrap。

部署配置位于 `WORK_DIR/maxkb-remove-duplicates/.env`。容器通过 Compose 注入；源码运行时可用 `APP_ENV_FILE` 指定该文件，不指定时兼容读取项目根目录原有 `.env`。系统环境变量优先于文件。

首次可执行 `bash install/install.sh --init-env` 从 `install/.env.example` 生成工作目录中的配置，再按运行环境编辑实际 `.env`。`.env` 已被 Git 忽略；已有配置继续使用，不重新生成密钥。

| 配置 | 说明 |
| --- | --- |
| DB_HOST / DB_PORT | 本系统数据库地址、端口，Docker 模板为 postgres:5432；源码运行填写实际地址 |
| DB_NAME / DB_USER / DB_PASSWORD | 本系统数据库名、用户、密码 |
| DB_SSLMODE | PostgreSQL SSL 模式，默认 prefer |
| ENCRYPTION_KEY | 数据源凭证加密密钥 |
| ADMIN_USERNAME / ADMIN_PASSWORD | 首次创建的管理员账号 |
| ALLOWED_ORIGINS / COOKIE_SECURE | 前端来源和会话 Cookie 设置 |
| WEB_PORT | Compose Web 端口 |
| IMAGE_TAG | 应用镜像版本标签，默认 latest |
| WORK_DIR | 模板指定的工作目录 |
| INSTALL_DIR | 脚本生成的绝对应用目录，包含 .env 和持久数据 |
| APP_ENV_FILE | 源码运行时实际 .env 路径，可选 |
| SOURCE_DIR | 源码运行时扫描的文档目录；Compose 内固定为 /sources |

需要时也可用环境变量 `DATABASE_URL` 覆盖分项数据库配置；通常填写 `DB_*` 即可，密码里的特殊字符无需自行 URL 编码。

源码开发时自行准备可连接的 PostgreSQL，并将 `.env` 的 `DB_HOST` 改为实际地址（例如 `127.0.0.1`），`DB_PORT` 改为该实例端口。模板中的 `postgres` 是容器内部服务名，不能直接用于宿主机源码进程。Compose 数据库默认不发布宿主机端口。

```bash
cd /path/to/example/maxkb-remove-duplicates
# 只生成配置，不需要 Docker；已有 .env 保留
bash install/install.sh --init-env
# 默认工作目录；自定义时改为实际路径，并编辑此文件的数据库地址及端口
export APP_ENV_FILE="$(pwd)/work/maxkb-remove-duplicates/.env"
cd backend
uv sync --group dev
uv run uvicorn app.main:app --reload
```

后端这一个命令已经包含扫描队列和定时调度，无需再开终端启动 worker 或 scheduler。数据库迁移和管理员初始化在后台线程启动前完成。

开发时 `--reload` 会随代码变化重启整个后端，正在扫描的源快照会丢失。后端关闭时停止领取任务、取消正在执行的源查询并释放租约；下次启动为中断的扫描创建新运行重扫，保留原来的部分结果。强制终止或数据库不可达时，等待租约过期后恢复。

长期运行建议去掉 `--reload`，使用 `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`。扫描在独立线程执行，不阻塞 Web 事件循环；各线程使用各自数据库连接。持久化队列、运行租约及定时去重仍保留。

更改 `.env` 后重启后端。Compose 配置变更后，进入 `WORK_DIR/maxkb-remove-duplicates`，使用 `docker compose up -d --force-recreate --remove-orphans` 重新注入配置并清理旧后台服务容器。

前端：

```bash
cd frontend
npm ci
npm run dev
```

访问 http://localhost:5173 。Vite 将 `/api` 代理到本机 8000 端口。API 文档为后端 `/docs`（Compose 下默认不公开）。

## 开发检查

```bash
cd backend
npm ci --prefix tests/sql
uv run pytest -q
cd ../frontend
npm run build
```

SQL 测试用 PGlite 的 PostgreSQL/pgvector 执行生产比较 SQL，与独立 Python 全量余弦计算结果核对，覆盖分批、同段落排除、同文档选项、阈值和最大值聚合、选定知识库范围过滤及问题向量排除。不依赖外部 MaxKB，也不会连接生产数据库。未安装测试运行时会明确跳过这部分测试。

另有一次性数据库生命周期检查，验证本系统无需 vector 扩展、源库直接计算、失败后新运行重试、租约隔离及草稿前后文本留存：

```bash
# backend 目录，终端一；两个仅用于测试的内存数据库，占用 15438/15439
node tests/sql/serve.mjs
# 终端二
DEDUP_DISPOSABLE_TEST_DB=1 uv run pytest -q
```

每次执行生命周期检查前重启临时数据库服务。测试不连接真实 MaxKB。结束后 Ctrl+C 关闭临时服务。

数据表首次建表 SQL：`backend/migrations/versions/initial_schema.sql`。迁移使用已冻结 SQL，后续调整必须新增 Alembic revision。

## 扫描前预估

手动新扫描、失败后重新扫描均先统计所选知识库的正文 chunk、段落和文档数量，展示知识库名称与向量维度，再计算不同段落之间需要比较的 chunk 对数。排除同文档时按文档分组扣除。统计只做聚合，不进行相似度两两比较。

用户取消不创建运行；确认后才排队。预估有效期 10 分钟，与用户、任务配置绑定；任务配置变化或预估过期需要重新统计。同一预估重复确认只返回原运行，不重复启动。数量统计与正式扫描不是同一快照，期间数据变化会使实际工作量和耗时变化。

预计耗时显示区间，不含排队时间。优先参考同数据源、同维度、同批次/阈值/排除规则的成功运行速度（剔除过短小样本），取中位数；旧版没有实际开始时间的运行不参与。暂无历史时使用未校准经验速率，界面明确标注，给出较宽区间。首次经验值可在 `.env` 设置 `SCAN_ESTIMATE_COMPARISONS_PER_SECOND=50000`（768 维参考比较数/秒），按维度调整；不能视为性能承诺。

定时任务仍按已启用的规则自动运行，无需届时人工确认。自动重试沿用之前已授权的扫描范围。

启用或修改启用中的定时任务，保存前同样统计当前表单指定范围并展示预计耗时、Cron、时区和下次执行时间；确认后才写入任务配置，不立即执行扫描。取消不创建或修改任务。确认有效 10 分钟，并绑定完整表单、当前用户和任务标识；修改配置需重新预估。暂停定时任务无需等待统计，可直接保存。


## 知识入库

启动命令与以前相同，无需独立入库服务或额外数据库。后端启动自动执行 `0004`，创建 PostgreSQL 的 `ingestion` schema。左侧功能下拉切换“知识入库”，打开“入库设置”配置 MinerU、MaxKB API、工作空间和切分/导入参数。

也可在“重复片段 → 数据源”保存公共 MaxKB API 连接，入库顶部选择该连接并点击“应用连接”。仅做入库时数据库主机可留空；重复扫描任务只列出配置了数据库的连接。已有队列保存自己的配置快照，应用连接只影响后续新任务。

“文档入库”支持 PDF、Word、Excel 及文件夹任务，确认知识库匹配/创建和标签后处理。需要人工检查时保持关闭自动入库，处理后预览切分，再手动确认入库。PDF 使用 MinerU，Word、Excel 延续原项目的原生处理路径。文件同步页面支持服务器映射目录及 SSH 来源。原项目不需要启动，其 SQLite 数据不会导入。

本地开发文件默认保存在项目 `data/ingestion/`。可在源码运行实际读取的 `.env` 配置：

```dotenv
INGESTION_DATA_DIR=/absolute/path/to/persistent/ingestion
SOURCE_DIR=/absolute/path/to/documents
PARSE_WORKERS=1
SPLIT_WORKERS=2
IMPORT_WORKERS=5
```

Compose 将入库文件持久化到 `INSTALL_DIR/ingestion`，文档映射目录为 `INSTALL_DIR/documents`；工作目录的 `compose.yaml` 已包含读写挂载，目录归档操作需要写权限。`SOURCE_DIR` 是后端或容器可见的路径。默认每个文件上限 200MB、单次请求 20480MB，可通过实际 `.env` 中的 `MAX_*` 调整，由 FastAPI 的上传大小限制直接控制。

备份需同时保存 PostgreSQL、工作目录下的入库文件及实际 `.env` 的 `ENCRYPTION_KEY`。数据库不能替代原文件和解析文件备份。入库队列与远端回执持久化，服务重启会恢复可继续的任务；创建结果不明确时需要先核对，避免重复创建。

### 入库适配回归

`backend/tests/ingestion` 从原项目业务回归迁入，使用模拟上游接口与隔离 PostgreSQL schema。不会上传到真实 MaxKB。临时数据库准备方式沿用 `backend/tests/sql/serve.mjs`：

```bash
cd backend/tests/sql
npm ci
node serve.mjs
# 另一终端，在 backend 目录执行：
INGESTION_PG_TEST_DB=1 uv run pytest tests/ingestion -q
```

普通后端检查为 `uv run pytest -q`；需要临时数据库的检查默认跳过。临时数据库仅监听 15438/15439，不使用 `.env` 的业务数据库。不要同时运行不同进程的临时数据库回归：PGlite socket 服务仅适合单客户端连接。主系统生命周期检查使用 `DEDUP_DISPOSABLE_TEST_DB=1 uv run pytest tests/test_lifecycle.py -q`，应在重启临时数据库后单独运行。
