from unittest.mock import MagicMock
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.exc import OperationalError, ProgrammingError
from app import initialize as startup


def prepare(monkeypatch, name="dedup"):
    target = MagicMock()
    target.url = make_url("postgresql+psycopg://user:fake@localhost/" + name)
    target.connect.side_effect = OperationalError(
        None, None, Exception("connection startup failure")
    )
    admin = MagicMock()
    admin.dialect = dialect()
    connection = admin.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar.return_value = None
    monkeypatch.setattr(startup.db, "engine", target)
    monkeypatch.setattr(startup, "create_engine", lambda *a, **k: admin)
    return admin, connection


def test_missing_database_created_even_without_sqlstate(monkeypatch):
    admin, connection = prepare(monkeypatch)
    startup.ensure_database()
    connection.exec_driver_sql.assert_called_once_with('CREATE DATABASE "dedup"')
    admin.dispose.assert_called_once()


def test_existing_database_auth_failure_does_not_create(monkeypatch):
    admin, connection = prepare(monkeypatch)
    connection.execute.return_value.scalar.return_value = 1
    with pytest.raises(RuntimeError, match="认证"):
        startup.ensure_database()
    connection.exec_driver_sql.assert_not_called()
    admin.dispose.assert_called_once()


def test_concurrent_database_creation_is_safe(monkeypatch):
    admin, connection = prepare(monkeypatch)
    error = Exception("duplicate database")
    error.sqlstate = "42P04"
    connection.exec_driver_sql.side_effect = ProgrammingError(None, None, error)
    startup.ensure_database()
    admin.dispose.assert_called_once()
