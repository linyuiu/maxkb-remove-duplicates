"""Bounded SQL writes and atomic publication using a disposable PostgreSQL DB."""
import pytest
from .support import CONFIG
from app.ingestion.pgstore import Connection


def prepare(store):
    store.create_batch('audit-batch', [{'id': 'audit-file', 'name': 'audit.docx', 'bytes': 1}], CONFIG)


def test_save_large_split_sql_round_trips(workbench, monkeypatch):
    store = workbench.store
    prepare(store)
    queries = []
    execute = Connection.execute
    def counted(self, sql, params=()):
        queries.append(sql)
        return execute(self, sql, params)
    monkeypatch.setattr(Connection, 'execute', counted)
    store.save_paragraphs('audit-file', [
        {'name': name, 'paragraphs': [{'title': name, 'content': f'paragraph {i}'} for i in range(500)]}
        for name in ['first', 'second']
    ])
    assert store.file('audit-file')['paragraph_count'] == 1000
    staging = [i for i, sql in enumerate(queries) if sql.startswith('INSERT INTO ingestion_paragraph_stage')]
    publication = [i for i, sql in enumerate(queries) if sql.startswith('INSERT INTO paragraphs')]
    lock = next(i for i, sql in enumerate(queries) if 'pg_advisory_xact_lock' in sql)
    assert len(staging) == 2 and len(publication) == 1
    assert max(staging) < lock < publication[0]
    rows = store.preview('audit-file', 0, 1000)
    assert [r['position'] for r in rows] == list(range(1000))
    assert rows[499]['document_name'] == 'first'
    assert rows[500]['document_name'] == rows[500]['title'] == 'second'
    assert rows[999]['content'] == 'paragraph 499'


@pytest.mark.parametrize('failure', ['stage', 'publish', 'event'])
def test_save_failure_rolls_back_everything(workbench, monkeypatch, failure):
    store = workbench.store
    prepare(store)
    original = [{'name': 'old', 'paragraphs': [{'content': 'keep me'}]}]
    store.save_paragraphs('audit-file', original)
    before = store.file('audit-file')
    events = store.events('audit-batch', 0)
    execute = Connection.execute
    staged = 0
    def fail(self, sql, params=()):
        nonlocal staged
        if sql.startswith('INSERT INTO ingestion_paragraph_stage'):
            staged += 1
        if ((failure == 'stage' and staged == 2)
            or (failure == 'publish' and sql.startswith('INSERT INTO paragraphs'))
            or (failure == 'event' and sql.startswith('INSERT INTO events'))):
            raise RuntimeError('injected failure')
        return execute(self, sql, params)
    with monkeypatch.context() as patch:
        patch.setattr(Connection, 'execute', fail)
        with pytest.raises(RuntimeError, match='injected'):
            store.save_paragraphs('audit-file', [{'name': 'new', 'paragraphs': [{'content': 'replace'} for _ in range(1001)]}], auto_import=True)
    assert store.file('audit-file') == before
    assert store.preview('audit-file', 0, 10)[0]['content'] == 'keep me'
    assert store.events('audit-batch', 0) == events
    # A pooled connection must be reusable after rollback (no leaked temp table).
    store.save_paragraphs('audit-file', [])
    assert store.preview('audit-file', 0, 10) == []
    assert store.file('audit-file')['paragraph_count'] == 0


def test_stop_before_publication_is_not_revived(workbench):
    store = workbench.store
    prepare(store)
    store.request_stop(file_id='audit-file')
    before = store.file('audit-file')
    store.save_paragraphs('audit-file', [{'name': 'new', 'paragraphs': [{'content': 'new'}]}], auto_import=True)
    assert store.file('audit-file') == before
    assert store.preview('audit-file', 0, 10) == []


def test_large_text_batches_are_bounded(workbench, monkeypatch):
    store = workbench.store
    prepare(store)
    sizes = []
    execute = Connection.execute
    def counted(self, sql, params=()):
        if sql.startswith('INSERT INTO ingestion_paragraph_stage'):
            sizes.append(sum(len(v.encode('utf-8')) for v in params if isinstance(v, str)))
        return execute(self, sql, params)
    monkeypatch.setattr(Connection, 'execute', counted)
    content = '正文' * 100_000
    store.save_paragraphs('audit-file', [{'name': 'large', 'paragraphs': [{'content': content} for _ in range(3)]}])
    assert len(sizes) == 3
    assert max(sizes) <= 1024 * 1024
    assert [r['content'] for r in store.preview('audit-file', 0, 10)] == [content] * 3
