"""Idempotent startup initialization, serialized across API instances."""

from pathlib import Path
import os
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session
from . import db
from .models import User
from .security import cipher, passwords

LOCK_ID = 632417209


def ensure_database():
    try:
        with db.engine.connect():
            return
    except OperationalError:
        # libpq connection/startup errors do not reliably expose SQLSTATE.
        pass
    name = db.engine.url.database
    if not name:
        raise RuntimeError("请在 .env 配置 DB_NAME")
    admin = create_engine(
        db.engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": name}
            ).scalar()
            if exists:
                raise RuntimeError(
                    "本系统数据库连接失败，请检查 .env 认证、SSL 及连接权限"
                )
            quoted = admin.dialect.identifier_preparer.quote_identifier(name)
            try:
                connection.exec_driver_sql(f"CREATE DATABASE {quoted}")
            except (ProgrammingError, OperationalError) as exc:
                if getattr(exc.orig, "sqlstate", None) != "42P04":
                    raise RuntimeError(
                        "自动创建数据库失败；请给 DB_USER 创建数据库权限或预先创建 DB_NAME"
                    ) from None
    except OperationalError:
        raise RuntimeError(
            "数据库服务连接失败，请检查 .env 的 DB_* 配置和数据库服务"
        ) from None
    finally:
        admin.dispose()


def initialize():
    cipher()
    ensure_database()
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    with db.engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        with Session(bind=connection) as session:
            username = os.environ.get("ADMIN_USERNAME", "admin")
            if not session.scalar(select(User).where(User.username == username)):
                password = os.environ.get("ADMIN_PASSWORD", "")
                if len(password) < 12:
                    raise RuntimeError(
                        "首次初始化需要在 .env 设置至少 12 位的 ADMIN_PASSWORD"
                    )
                session.add(
                    User(username=username, password_hash=passwords.hash(password))
                )
                session.commit()
