#!/usr/bin/env bash
# Read data files literally; never source/eval configuration or print credentials.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_FILE="$SCRIPT_DIR/.env.example"
DOCKER_DIR="$PROJECT_DIR/docker"
IMAGES_DIR="$DOCKER_DIR/images"
COMPOSE_TEMPLATE="$DOCKER_DIR/compose.yaml"
INIT_ONLY=false
case "${1:-}" in
  "") ;;
  --init-env) INIT_ONLY=true ;;
  -h|--help)
    printf '%s\n' '用法：bash install/install.sh [--init-env]' \
      '读取 install/.env.example，在 WORK_DIR/maxkb-remove-duplicates 下生成配置、compose.yaml 和数据目录。' \
      '默认还会加载 docker/images 镜像并重启服务；--init-env 不需要 Docker。'
    exit 0 ;;
  *) printf '%s\n' '不支持的参数；使用 --help 查看用法。' >&2; exit 1 ;;
esac
if [[ $# -gt 1 ]]; then printf '%s\n' '参数过多。' >&2; exit 1; fi
[[ -f "$COMPOSE_TEMPLATE" ]] || { printf '%s\n' '缺少 docker/compose.yaml 模板。' >&2; exit 1; }
[[ -f "$TEMPLATE_FILE" ]] || { printf '%s\n' '缺少 install/.env.example。' >&2; exit 1; }

# Directory settings use a single KEY=value line, optionally surrounded by quotes.
# No shell interpolation is performed (use an absolute path, not $HOME or ~).
read_setting() {
  local key="$1" file="$2" line value='' found=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ "$line" == "$key="* ]]; then
      found=$((found + 1)); value="${line#*=}"
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"
    fi
  done < "$file"
  [[ $found -le 1 ]] || { printf '配置项 %s 重复。\n' "$key" >&2; return 1; }
  case "$value" in
    \"*\") value="${value:1:${#value}-2}" ;;
    \'*\') value="${value:1:${#value}-2}" ;;
  esac
  printf '%s' "$value"
}
WORK_DIR="$(read_setting WORK_DIR "$TEMPLATE_FILE")"
[[ -n "$WORK_DIR" ]] || { printf '%s\n' '请在 install/.env.example 填写 WORK_DIR。' >&2; exit 1; }
case "$WORK_DIR" in
  /*) ;;
  *) WORK_DIR="$PROJECT_DIR/$WORK_DIR" ;;
esac
umask 077
mkdir -p -- "$WORK_DIR"
WORK_DIR="$(cd -- "$WORK_DIR" && pwd -P)"
INSTALL_DIR="$WORK_DIR/maxkb-remove-duplicates"
# Use unambiguous single-quoted dotenv paths, including paths with spaces or $. 
case "$WORK_DIR" in
  *\'*|*$'\n'*|*$'\r'*) printf '%s\n' '工作目录不能包含单引号或换行符。' >&2; exit 1 ;;
esac
mkdir -p -- "$INSTALL_DIR" "$INSTALL_DIR/postgres" "$INSTALL_DIR/ingestion" "$INSTALL_DIR/documents"
ENV_FILE="$INSTALL_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  recorded_dir="$(read_setting INSTALL_DIR "$ENV_FILE")"
  if [[ -z "$recorded_dir" ]]; then
    printf "\nINSTALL_DIR='%s'\n" "$INSTALL_DIR" >> "$ENV_FILE"
  elif [[ "$recorded_dir" != "$INSTALL_DIR" ]]; then
    printf '%s\n' '已有 .env 的 INSTALL_DIR 与当前工作目录不同，请核对配置后再安装。' >&2; exit 1
  fi
  printf '%s\n' '保留工作目录下已有 .env，不覆盖数据库密码、管理员密码和加密密钥。'
elif [[ -e "$ENV_FILE" ]]; then
  printf '%s\n' '.env 必须是普通配置文件。' >&2; exit 1
else
  temp_file="$(mktemp "$INSTALL_DIR/.env.tmp.XXXXXX")"
  trap 'rm -f -- "${temp_file:-}"' EXIT
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    case "$line" in
      WORK_DIR=*|INSTALL_DIR=*) ;;
      ENCRYPTION_KEY=replace-with-fernet-key)
        key="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\r\n')"
        printf 'ENCRYPTION_KEY=%s\n' "$key" ;;
      ADMIN_PASSWORD=replace-with-at-least-12-characters)
        password="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \r\n')"
        printf 'ADMIN_PASSWORD=%s\n' "$password" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$TEMPLATE_FILE" > "$temp_file"
  printf "\nWORK_DIR='%s'\nINSTALL_DIR='%s'\n" "$WORK_DIR" "$INSTALL_DIR" >> "$temp_file"
  if ln "$temp_file" "$ENV_FILE" 2>/dev/null; then
    printf '%s\n' '已从 install/.env.example 生成工作目录下的 .env，权限为 600。'
  else
    printf '%s\n' '.env 创建失败或被其他进程创建，请检查后重新执行。' >&2; exit 1
  fi
  rm -f -- "$temp_file"; trap - EXIT
fi
# Runtime Compose uses relative mounts, so it can run without the installation package.
COMPOSE_FILE_PATH="$INSTALL_DIR/compose.yaml"
if [[ -f "$COMPOSE_FILE_PATH" ]] && ! cmp -s "$COMPOSE_TEMPLATE" "$COMPOSE_FILE_PATH"; then
  cp -p -- "$COMPOSE_FILE_PATH" "$COMPOSE_FILE_PATH.bak"
fi
compose_temp="$(mktemp "$INSTALL_DIR/.compose.yaml.XXXXXX")"
trap 'rm -f -- "${compose_temp:-}"' EXIT
cp -- "$COMPOSE_TEMPLATE" "$compose_temp"
mv -f -- "$compose_temp" "$COMPOSE_FILE_PATH"
trap - EXIT
printf '服务目录：%s\n配置文件：%s\n本地文档目录：%s\n' "$INSTALL_DIR" "$ENV_FILE" "$INSTALL_DIR/documents"

[[ "$INIT_ONLY" == false ]] || exit 0

command -v docker >/dev/null 2>&1 || { printf '%s\n' '请先安装 Docker Engine 和 Docker Compose V2。' >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { printf '%s\n' '需要 Docker Compose V2（docker compose）。' >&2; exit 1; }
docker info >/dev/null 2>&1 || { printf '%s\n' 'Docker 服务未启动或当前用户无访问权限。' >&2; exit 1; }
shopt -s nullglob
images=("$IMAGES_DIR"/*.tar "$IMAGES_DIR"/*.tar.gz "$IMAGES_DIR"/*.tgz)
if [[ ${#images[@]} -eq 0 ]]; then
  printf '%s\n' 'docker/images 没有镜像包，请放入应用和 postgres:16 镜像包。' >&2; exit 1
fi
for image_file in "${images[@]}"; do
  printf '加载镜像：%s\n' "$(basename -- "$image_file")"
  docker image load --input "$image_file"
done
cd -- "$INSTALL_DIR"
compose=(docker compose --env-file .env -f compose.yaml)
printf '%s\n' '停止旧容器并保留数据，然后启动 PostgreSQL 和应用。'
"${compose[@]}" down --remove-orphans
"${compose[@]}" up -d --no-build --pull never --remove-orphans
"${compose[@]}" ps
printf '%s\n' '容器已启动，首次启动会自动建库并由应用迁移业务表；请查看服务日志确认初始化进度。' \
  '管理员初始密码在工作目录的 .env 中查看，不会打印到日志。'

printf '后续操作请先进入服务目录：%s\n' "$INSTALL_DIR"
