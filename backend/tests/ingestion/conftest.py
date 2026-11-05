import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from .support import CONFIG, SFTP, Upstream, Workbench

from app.ingestion.main import create_app


@pytest.fixture
def workbench(tmp_path):
    upstream = Upstream()
    mapped, remote = tmp_path / "mapped", tmp_path / "remote"
    mapped.mkdir()
    remote.mkdir()
    sftp = SFTP(remote)
    app = create_app(
        tmp_path / "data",
        source_dir=mapped,
        run_workers=False,
        client_factory=upstream.factory,
        ssh_factory=sftp.factory,
    )
    with TestClient(app) as client:
        assert client.put("/api/ingestion/settings", json=CONFIG).status_code == 200
        yield Workbench(client, upstream, sftp, mapped, dict(CONFIG))



# Disposable PostgreSQL only. Each Store directory receives its own scratch schema.
import os
import hashlib
from sqlalchemy import create_engine
from cryptography.fernet import Fernet
from app.ingestion import pgstore
from app.ingestion.store import Store
from psycopg import sql

@pytest.fixture(autouse=True)
def postgres_repository(monkeypatch):
    if os.environ.get('INGESTION_PG_TEST_DB') != '1':
        pytest.skip('Requires disposable PostgreSQL server on port 15438')
    from app import db
    engine=create_engine('postgresql+psycopg://postgres:postgres@127.0.0.1:15438/postgres?sslmode=disable',pool_size=1,max_overflow=0)
    monkeypatch.setattr(db,'engine',engine)
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    original=Store.__init__
    schemas=set()
    def init(self, directory):
        self.test_schema='t_'+hashlib.sha256(str(directory).encode()).hexdigest()[:24]
        if self.test_schema not in schemas:
            with engine.begin() as conn:
                conn.exec_driver_sql('CREATE SCHEMA '+self.test_schema)
                conn.exec_driver_sql('SET LOCAL search_path TO '+self.test_schema+',public')
                ddl=(Path(pgstore.__file__).parent/'schema.sql').read_text()
                for statement in ddl.split(';'):
                    if statement.strip():conn.exec_driver_sql(statement)
            schemas.add(self.test_schema)
        original(self,directory)
    monkeypatch.setattr(Store,'__init__',init)
    monkeypatch.setattr(Store,'connect',lambda self:pgstore.connect(self.test_schema))
    yield
    with engine.begin() as conn:
        for schema in schemas:conn.exec_driver_sql('DROP SCHEMA '+schema+' CASCADE')
    engine.dispose()
