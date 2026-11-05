from sqlalchemy.engine import make_url
from app import config


def test_loads_root_file_independent_of_working_directory(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('DEDUP_CONFIG_TEST=from-root\n')
    elsewhere = tmp_path / 'another'
    elsewhere.mkdir()
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    monkeypatch.delenv('DEDUP_CONFIG_TEST', raising=False)
    monkeypatch.chdir(elsewhere)
    config.load_environment()
    import os
    assert os.environ['DEDUP_CONFIG_TEST'] == 'from-root'


def test_process_environment_wins(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_text('DB_HOST=file-host\n')
    monkeypatch.setenv('DB_HOST', 'process-host')
    config.load_environment(path)
    assert config.database_url().host == 'process-host'


def test_literal_password_and_url_encoding(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    secret = 'test@:/?#${HOME}value'
    path.write_text(f'DB_PASSWORD="{secret}"\n')
    monkeypatch.delenv('DB_PASSWORD', raising=False)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    config.load_environment(path)
    url = config.database_url()
    assert url.password == secret
    assert make_url(url.render_as_string(hide_password=False)).password == secret


def test_explicit_url_override(monkeypatch):
    url = 'postgresql+psycopg://test:fake@db:5432/test'
    monkeypatch.setenv('DATABASE_URL', url)
    assert config.database_url() == url


def test_invalid_port_has_clear_error(monkeypatch):
    import pytest
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DB_PORT', 'invalid')
    with pytest.raises(RuntimeError, match='DB_PORT'):
        config.database_url()
