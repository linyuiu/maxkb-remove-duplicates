"""Reparse through the public endpoint, including cached artifacts and rollback."""
import pytest
from app.ingestion.pgstore import Connection
from .support import PDF, office


def prepared(workbench, name='A/manual.pdf', content=PDF):
    _, queue = workbench.queue([name], contents={name: content})
    workbench.provision()
    workbench.prepare()
    return workbench.store.snapshot(queue['batches'][0]['id'])['files'][0]


def test_reparse_clears_results_and_submits_a_new_pdf_job(workbench):
    file = prepared(workbench)
    store = workbench.store
    directory = store.directory(file['id'])
    mineru = store.mineru_directory(file['id'])
    assert mineru.exists() and (directory / 'split.json').exists()
    assert store.preview(file['id'], 0, 10)
    uploads_before = len([r for r in workbench.upstream.requests if r.url.path == '/v1/uploads'])
    response = workbench.client.post(f"/api/ingestion/files/{file['id']}/reparse")
    assert response.status_code == 202, response.text
    row = store.file(file['id'])
    assert row['status'] in {'queued', 'cleanup_pending'}
    assert row['parse_state'] == 'pending'
    assert row['job_id'] is None and row['source_id'] is None
    assert row['paragraph_count'] == 0 and row['prepared_at'] is None
    assert store.preview(file['id'], 0, 10) == []
    assert not mineru.exists() and not (directory / 'split.json').exists()
    assert (directory / file['name']).exists()
    if row['status'] == 'cleanup_pending':
        with workbench.upstream.factory() as client:
            workbench.workers.monitor(store.claim_monitor(), client)
    workbench.prepare()
    row = store.file(file['id'])
    assert row['status'] == 'review', row
    assert row['parse_state'] == 'succeeded'
    assert len([r for r in workbench.upstream.requests if r.url.path == '/v1/uploads']) > uploads_before
    assert store.preview(file['id'], 0, 10)


def test_reparse_rollback_restores_artifacts(workbench, monkeypatch):
    file = prepared(workbench)
    store = workbench.store
    directory = store.directory(file['id'])
    old_split = (directory / 'split.json').read_bytes()
    before = store.file(file['id'])
    execute = Connection.execute
    def fail(self, sql, params=()):
        if sql.startswith('INSERT INTO events'):
            raise RuntimeError('injected')
        return execute(self, sql, params)
    with monkeypatch.context() as patch:
        patch.setattr(Connection, 'execute', fail)
        with pytest.raises(RuntimeError, match='injected'):
            store.retry(file['id'], reparse=True)
    assert store.file(file['id']) == before
    assert (directory / 'split.json').read_bytes() == old_split
    assert store.mineru_directory(file['id']).exists()
    assert store.preview(file['id'], 0, 10)


@pytest.mark.parametrize('status', ['imported', 'importing', 'import_unknown', 'stopped'])
def test_reparse_rejects_unsafe_states(workbench, status):
    file = prepared(workbench)
    workbench.store.update(file['id'], status=status)
    response = workbench.client.post(f"/api/ingestion/files/{file['id']}/reparse")
    assert response.status_code == 409
    assert workbench.store.file(file['id'])['status'] == status
    assert workbench.store.preview(file['id'], 0, 10)


def test_reparse_rejects_word(workbench):
    file = prepared(workbench, 'A/manual.docx', office('word'))
    assert workbench.client.post(f"/api/ingestion/files/{file['id']}/reparse").status_code == 409


def test_missing_original_preserves_old_parse(workbench):
    file = prepared(workbench)
    store = workbench.store
    directory = store.directory(file['id'])
    (directory / file['name']).unlink()
    before = store.file(file['id'])
    response = workbench.client.post(f"/api/ingestion/files/{file['id']}/reparse")
    assert response.status_code >= 400
    assert store.file(file['id']) == before
    assert store.mineru_directory(file['id']).exists()
    assert (directory / 'split.json').exists()
