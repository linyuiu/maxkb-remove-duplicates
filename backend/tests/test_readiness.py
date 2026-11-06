import os
import uuid
from types import SimpleNamespace
import pytest
from psycopg.rows import dict_row
from test_source_operations import connection
from app.source import check_vector_readiness

pytestmark = pytest.mark.skipif(os.environ.get("DEDUP_DISPOSABLE_TEST_DB") != "1", reason="Requires disposable test database")


@pytest.fixture
def source(connection):
    connection.exec_driver_sql('CREATE TABLE knowledge(id uuid primary key,name text)')
    connection.exec_driver_sql('CREATE TABLE document(id uuid primary key,name text)')
    connection.exec_driver_sql('CREATE TABLE paragraph(id uuid primary key,knowledge_id uuid,document_id uuid,status text)')
    connection.exec_driver_sql('CREATE TABLE embedding(paragraph_id uuid,knowledge_id uuid,document_id uuid,source_type text,embedding text)')
    ids=[str(uuid.uuid4()) for _ in range(3)]
    k,d,p=ids
    raw=connection.connection.driver_connection
    conn=SimpleNamespace(execute=lambda sql,args:raw.cursor(row_factory=dict_row).execute(sql,args))
    conn.execute('INSERT INTO knowledge VALUES (%s,%s)',(k,'知识库'))
    conn.execute('INSERT INTO document VALUES (%s,%s)',(d,'文档'))
    return conn,k,d,p


@pytest.mark.parametrize('status,has_vector,reason',[
    ('nnn2',True,None),('nnn2',False,'缺少正文向量'),
    ('nnn0',True,'等待向量化'),('nnn1',True,'正在向量化'),
    ('nnn3',True,'向量化失败'),('nnn4',True,'向量化取消中'),
    ('nnn5',True,'向量化已取消'),('nnnn',True,'状态未知'),
    (None,True,'状态未知'),('3112',True,None),
])
def test_readiness_states(source,status,has_vector,reason):
    conn,k,d,p=source
    conn.execute('INSERT INTO paragraph VALUES (%s,%s,%s,%s)',(p,k,d,status))
    if has_vector:
        conn.execute('INSERT INTO embedding VALUES (%s,%s,%s,%s,%s)',(p,k,d,'1','vector'))
    if reason:
        with pytest.raises(ValueError,match=reason) as error:check_vector_readiness(conn,[k])
        assert '知识库 / 文档' in str(error.value)
    else:check_vector_readiness(conn,[k])
    check_vector_readiness(conn,[str(uuid.uuid4())])


def test_problem_vectors_do_not_satisfy_body_readiness(source):
    conn,k,d,p=source
    conn.execute('INSERT INTO paragraph VALUES (%s,%s,%s,%s)',(p,k,d,'nnn2'))
    conn.execute('INSERT INTO embedding VALUES (%s,%s,%s,%s,%s)',(p,k,d,'0','vector'))
    with pytest.raises(ValueError,match='缺少正文向量'):check_vector_readiness(conn,[k])


def test_missing_vectors_for_entire_knowledge_are_detected(source):
    conn,k,d,p=source
    for _ in range(25):
        conn.execute('INSERT INTO paragraph VALUES (%s,%s,%s,%s)',(str(uuid.uuid4()),k,d,'nnn2'))
    with pytest.raises(ValueError,match='25 个段落'):check_vector_readiness(conn,[k])


@pytest.mark.parametrize('phase',['estimate','snapshot'])
def test_both_entry_points_block_before_scanning(source,monkeypatch,phase):
    from contextlib import nullcontext
    from app import estimation, scanning
    conn,k,d,p=source
    conn.execute('INSERT INTO paragraph VALUES (%s,%s,%s,%s)',(p,k,d,'nnn1'))
    config={'knowledge_ids':[k]}
    if phase=='estimate':
        monkeypatch.setattr(estimation,'connect',lambda source:nullcontext(conn))
        monkeypatch.setattr(estimation,'check_models',lambda *args:None)
        with pytest.raises(ValueError,match='正在向量化'):estimation.estimate(None,config,None)
    else:
        monkeypatch.setattr(scanning,'transaction',lambda:nullcontext(None))
        monkeypatch.setattr(scanning,'fenced',lambda *args:SimpleNamespace(snapshot_ready=False,total=0,config=config))
        monkeypatch.setattr(scanning,'check_models',lambda *args:None)
        with pytest.raises(ValueError,match='正在向量化'):scanning.take_snapshot('run','token',conn)
