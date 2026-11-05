"""PostgreSQL repository connection. All data lives in the ingestion schema.

Keeps the ported domain repositories' positional parameter API; transactions,
constraints and concurrency are PostgreSQL, not a SQLite side database.
"""
import re
from contextlib import contextmanager
from psycopg.rows import dict_row
from psycopg.pq import TransactionStatus
from .. import db

class Record:
    def __init__(self,values):self.data=values
    def keys(self):return self.data.keys()
    def items(self):return self.data.items()
    def values(self):return self.data.values()
    def get(self,key,default=None):return self.data.get(key,default)
    def __getitem__(self,key):return tuple(self.data.values())[key] if isinstance(key,int) else self.data[key]
    def __iter__(self):return iter(self.data.values())
    def __len__(self):return len(self.data)

class Result:
    def __init__(self,cursor):self.cursor=cursor
    @property
    def rowcount(self):return self.cursor.rowcount
    def fetchone(self):
        row=self.cursor.fetchone()
        return Record(row) if row is not None else None
    def fetchall(self):return [Record(row) for row in self.cursor.fetchall()]
    def __iter__(self):
        for row in self.cursor:yield Record(row)

def compile_query(sql):
    # PostgreSQL requires explicit integer aggregation of boolean predicates.
    offset=0
    while (match:=re.search(r'\bSUM\(',sql[offset:],re.I)):
        start=offset+match.start();opening=offset+match.end();depth=1;end=opening
        while depth and end<len(sql):
            if sql[end]=='(':depth+=1
            if sql[end]==')':depth-=1
            end+=1
        expression=sql[opening:end-1]
        if re.search(r'\bIN\s*\(|\bIS\b|[=<>]',expression,re.I):
            replacement='SUM(CASE WHEN '+expression+' THEN 1 ELSE 0 END)'
            sql=sql[:start]+replacement+sql[end:];offset=start+len(replacement)
        else:offset=end
    if re.search(r'INSERT\s+OR\s+IGNORE',sql,re.I):
        sql=re.sub(r'INSERT\s+OR\s+IGNORE','INSERT',sql,flags=re.I).rstrip().rstrip(';')+' ON CONFLICT DO NOTHING'
    # Replace bind markers outside string literals; do not alter literal question marks.
    sql=sql.replace('%','%%');out=[];quote=None;i=0
    while i<len(sql):
        c=sql[i]
        if quote:
            out.append(c)
            if c==quote:
                if i+1<len(sql) and sql[i+1]==quote:out.append(sql[i+1]);i+=1
                else:quote=None
        elif c in "'\"":quote=c;out.append(c)
        elif c=='?':out.append('%s')
        else:out.append(c)
        i+=1
    return ''.join(out)

class Connection:
    def __init__(self,connection,schema):self.connection=connection;self.schema=schema
    def execute(self,sql,params=()):
        if sql.strip().upper()=='BEGIN':
            sql='SELECT 1'
        if self.connection.info.transaction_status==TransactionStatus.IDLE:
            with self.connection.cursor() as cursor:
                cursor.execute('SET LOCAL search_path TO '+self.schema+', public')
                cursor.execute("SET LOCAL lock_timeout = '15s'")
        cursor=self.connection.cursor(row_factory=dict_row)
        cursor.execute(compile_query(sql),tuple(int(x) if isinstance(x,bool) else x for x in params))
        return Result(cursor)
    def executemany(self,sql,params):
        count=0
        for row in params:count+=max(0,self.execute(sql,row).rowcount)
        return count

@contextmanager
def connect(schema="ingestion"):
    if not re.fullmatch(r"[a-z][a-z0-9_]*",schema):raise ValueError("Invalid schema")
    connection=db.engine.raw_connection()
    try:
        yield Connection(connection.driver_connection,schema)
        connection.commit()
    except BaseException:
        connection.rollback();raise
    finally:connection.close()
