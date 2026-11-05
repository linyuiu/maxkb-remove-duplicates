"""Load the project .env independent of the process working directory."""

import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_environment(path=None):
    env_file = Path(path) if path is not None else Path(os.environ.get("APP_ENV_FILE", str(PROJECT_ROOT / ".env")))
    # Process/container environment wins. Keep literal '$' sequences in passwords.
    load_dotenv(env_file, override=False, interpolate=False)


def database_url():
    if value := os.environ.get("DATABASE_URL"):
        return value
    try:
        port = int(os.environ.get("DB_PORT", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise RuntimeError("DB_PORT 必须是 1 到 65535 的整数") from None
    password = os.environ.get("DB_PASSWORD")
    if not password:
        raise RuntimeError("请执行 bash install/install.sh --init-env，并通过 APP_ENV_FILE 指定生成的 .env")
    # URL.create escapes special characters without changing the actual password.
    return URL.create(
        "postgresql+psycopg",
        username=os.environ.get("DB_USER", "dedup"),
        password=password,
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=port,
        database=os.environ.get("DB_NAME", "dedup"),
        query={"sslmode": os.environ.get("DB_SSLMODE", "prefer")},
    )
