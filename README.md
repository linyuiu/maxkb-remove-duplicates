# MaxKB 知识治理工作台

支持文档入库和相似段落扫描。前后端由 FastAPI 提供，PostgreSQL 独立运行。

## 下载安装包

在 [GitHub Actions](https://github.com/linyuiu/maxkb-remove-duplicates/actions/workflows/build-images.yml) 点击 **Run workflow**：

- `architecture`：选择 `amd64`、`arm64` 或 `both`。
- `image_tag`：镜像版本，默认 `v1.0.0`。

构建完成后，从 **Artifacts** 下载对应架构的安装包。解压 ZIP，再解压其中的 `.tar.gz`，包内已包含应用、PostgreSQL 镜像和安装脚本。产物保留 14 天。

## 安装

服务器需要 Docker Engine、Docker Compose V2 和 Bash。

1. 编辑 `install/.env.example`，设置 `WORK_DIR`、数据库密码、访问地址等参数。`WORK_DIR` 默认 `./work`，相对于安装包根目录，也可填写绝对路径。
2. 在安装包目录执行：

```bash
bash install/install.sh
```

脚本会加载 `docker/images/` 中的镜像，并生成以下服务目录：

```text
WORK_DIR/maxkb-remove-duplicates/
├── .env
├── compose.yaml
├── postgres/       # 数据库数据
├── ingestion/      # 入库文件和解析产物
└── documents/      # 待入库的本地文档
```

首次启动自动创建数据库、业务表和管理员。通过部署主机的 `WEB_PORT` 访问，默认端口 8080；管理员初始账号密码在生成的 `.env` 中查看。

已有 `.env` 不会覆盖。安装后修改服务目录中的 `.env`，不是模板；已有数据库及管理员密码也不会随配置修改自动重置。

仅生成配置和目录、不操作 Docker：

```bash
bash install/install.sh --init-env
```

## 启动和停止

进入生成的服务目录后执行，无需再指定配置文件路径：

```bash
cd /path/to/example/work/maxkb-remove-duplicates

docker compose up -d                    # 启动
docker compose down                     # 停止，保留数据
docker compose ps                       # 状态
docker compose logs -f app postgres     # 日志
docker compose restart                  # 重启
docker compose up -d --force-recreate    # 修改 .env 后重新创建容器
```

备份数据库、`ingestion/`、`documents/` 和 `.env`，保留原来的 `ENCRYPTION_KEY`。

## 使用

- **知识入库**：在“入库设置”配置 MaxKB API、Bearer Key 和 MinerU；支持 PDF、Word、Excel，以及目录和 SSH 来源。公共连接复用的是 MaxKB API 地址和 Key。
- **本地目录**：把文档放进服务目录的 `documents/`，网页选择“映射”后手动扫描并提交。入库成功后，原文件会归档到 `documents/已处理文档/`，需要读写权限。
- **重复片段**：添加 MaxKB 数据库连接，选择知识库、阈值和手动/定时任务。扫描前显示数量与预计耗时，结果展示完整段落，修改及删除通过 MaxKB API 执行。
- **删除扫描记录**：清理本次扫描结果和快照，保留操作文本历史，不删除 MaxKB 源数据。运行中的任务需先取消。
- **修改密码**：点击右上角用户菜单，验证原密码并填写两次新密码（至少 12 个字符），成功后重新登录。

MaxKB API 地址示例：`http://maxkb.example.com:18080/admin/api`，请替换为实际地址。系统数据库与 MaxKB 源数据库需分开配置。

## 手动构建镜像

在源码根目录执行，架构与目标服务器保持一致：

```bash
docker build -f docker/Dockerfile -t maxkb-remove-duplicates:v1.0.0 .
docker pull postgres:16
mkdir -p docker/images
docker image save -o docker/images/images.tar maxkb-remove-duplicates:v1.0.0 postgres:16
```

修改镜像版本时，同步修改配置模板的 `IMAGE_TAG`。

需求与设计见 [PLAN.md](PLAN.md)。
