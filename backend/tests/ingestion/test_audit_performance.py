"""Characterize SQL round trips without connecting to real upstream services."""
from .support import CONFIG
from app.ingestion.pgstore import Connection


def test_save_large_split_sql_round_trips(workbench, monkeypatch):
    store=workbench.store
    store.create_batch('audit-batch',[{'id':'audit-file','name':'audit.docx','bytes':1}],CONFIG)
    queries=[]
    execute=Connection.execute
    def counted(self, sql, params=()):
        queries.append(sql)
        return execute(self,sql,params)
    monkeypatch.setattr(Connection,'execute',counted)
    store.save_paragraphs('audit-file',[{'name':'audit','paragraphs':[{'content':f'paragraph {i}'} for i in range(1000)]}])
    inserts=sum('INSERT INTO paragraphs' in sql for sql in queries)
    assert store.file('audit-file')['paragraph_count']==1000
    # Diagnostic measurement, not a throughput benchmark or a desired performance target.
    print(f'\nAUDIT: 1000 paragraphs -> {inserts} paragraph INSERT calls; global lock acquired first: {"pg_advisory_xact_lock" in queries[0]}')
