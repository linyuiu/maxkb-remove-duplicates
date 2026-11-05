"""FastAPI serves the built frontend without swallowing API or asset errors."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.frontend import install_frontend


@pytest.fixture
def client(tmp_path):
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'index.html').write_text('<html><body>frontend fixture</body></html>')
    (dist / 'assets').mkdir()
    (dist / 'assets' / 'app.js').write_text('console.log("asset fixture")')
    (tmp_path / 'secret.txt').write_text('private source data')
    (dist / '.env').write_text('PRIVATE_SECRET=should-not-be-served')
    app = FastAPI()

    @app.get('/api/ping')
    def ping():
        return {'ok': True}

    install_frontend(app, dist)
    with TestClient(app) as test_client:
        yield test_client


def test_pages_refresh_to_spa(client):
    for path in ['/', '/sources', '/history', '/settings', '/page/nested']:
        result = client.get(path)
        assert result.status_code == 200
        assert 'frontend fixture' in result.text
        assert result.headers['content-type'].startswith('text/html')
    result = client.head('/history')
    assert result.status_code == 200 and result.content == b''


def test_static_asset_and_api_keep_their_own_responses(client):
    asset = client.get('/assets/app.js')
    assert asset.status_code == 200 and 'asset fixture' in asset.text
    assert 'javascript' in asset.headers['content-type']
    assert client.get('/api/ping').json() == {'ok': True}


@pytest.mark.parametrize('path', ['/api', '/api/missing', '/assets/missing.js',
    '/assets/missing', '/missing.css', '/.env', '/..%2Fsecret.txt'])
def test_unknown_api_assets_and_private_files_are_not_spa(client, path):
    result = client.get(path)
    assert result.status_code == 404
    assert 'frontend fixture' not in result.text
    assert 'private source data' not in result.text
    assert 'should-not-be-served' not in result.text


def test_api_only_development_does_not_require_a_frontend(tmp_path, monkeypatch):
    monkeypatch.delenv('FRONTEND_DIST_DIR', raising=False)
    from app import frontend
    monkeypatch.setattr(frontend, 'PROJECT_ROOT', tmp_path)
    app = FastAPI()
    install_frontend(app)
    assert TestClient(app).get('/').status_code == 404


def test_explicit_missing_dist_is_reported(tmp_path):
    with pytest.raises(RuntimeError, match='前端构建文件不存在'):
        install_frontend(FastAPI(), tmp_path)
