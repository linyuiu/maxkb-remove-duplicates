# MaxKB 知识治理工作台

支持文档入库和相似段落扫描。前后端由 FastAPI 提供，PostgreSQL 独立运行。

## 从源码构建运行

需要 Docker Engine、Docker Compose V2 和可下载构建依赖的网络。在源码根目录使用 `compose.yaml`；安装包使用 `docker/compose.yaml`。

首次没有根目录 `.env` 时，编辑 `install/.env.example`，配置 `WORK_DIR`、数据库密码和 `ALLOWED_ORIGINS`（网页实际访问的协议、主机和端口），然后执行：

```bash
bash install/install.sh --init-env
# 默认 WORK_DIR=./work；自定义目录时改为脚本输出的配置文件路径。
ln -s ./work/maxkb-remove-duplicates/.env .env
```

初始化会生成加密密钥和管理员初始密码。已有 `.env` 时直接使用现有配置，容器内数据库主机应配置为 `DB_HOST=postgres`，并检查 `IMAGE_VERSION=v1.0.0`；不要保留指向其他数据库的 `DATABASE_URL`。

在源码根目录构建并启动：

```bash
docker compose up -d --build
```

也可以分别执行：

```bash
docker compose build
docker compose up -d
```

应用通过同一个 `docker/Dockerfile` 构建，缺少 `postgres:16` 时自动拉取。数据保存在 `WORK_DIR/maxkb-remove-duplicates/` 的 `postgres/`、`ingestion/`、`documents/`，默认位于源码目录下的 `work/`。后续修改根目录 `.env`；使用上述符号链接时，它与工作目录配置是同一个文件。

查看日志和停止服务也在源码根目录执行：

```bash
docker compose logs -f app postgres
docker compose down
```

切换源码构建和安装包运行时，使用相同的工作目录、数据库配置和加密密钥。

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
- **向量就绪检查**：预估及执行前检查所选知识库的正文段落；缺少向量、等待/正在向量化、失败或未知状态都会阻止扫描，并提示知识库、文档及异常数量。定时任务在扫描历史中记录未执行原因，等待下次触发。
- **删除源段落/文档**：接口回读确认删除成功后，所有对应的相似对自动标为已处理，从待处理筛选中移除；保留原文快照和关联操作记录。
- **修改源段落**：接口回读确认修改成功后，关联旧结果标记为“内容已变化，需重新扫描”，从待处理筛选中移除；保留扫描快照及修改前后文本，重新扫描后再处理。
- **删除扫描记录**：清理本次扫描结果和快照，保留操作文本历史，不删除 MaxKB 源数据。运行中的任务需先取消。
- **修改密码**：点击右上角用户菜单，验证原密码并填写两次新密码（至少 12 个字符），成功后重新登录。

MaxKB API 地址示例：`http://maxkb.example.com:18080/admin/api`，请替换为实际地址。系统数据库与 MaxKB 源数据库需分开配置。

镜像版本通过 `.env` 的 `IMAGE_VERSION=v1.0.0` 配置，GitHub 打包时会同步写入安装模板。源码修改后执行 `docker compose up -d --build`；安装包的版本需与已加载的镜像标签一致。

需求与设计见 [PLAN.md](PLAN.md)。
