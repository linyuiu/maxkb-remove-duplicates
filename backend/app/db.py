from .config import database_url
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .profiling import current, measure


class Base(DeclarativeBase):
    pass


engine = create_engine(
    database_url(), pool_pre_ping=True, connect_args={"connect_timeout": 10}
)
Session = sessionmaker(engine, expire_on_commit=False)
SCAN_WORK_MEM = "64MB"


@contextmanager
def transaction():
    if current() is None:
        with Session.begin() as session:
            yield session
        return
    # Explicit boundaries expose checkout/pre-ping, ORM flush, commit and cleanup.
    # This follows Session.begin()'s existing commit/rollback lifecycle.
    with measure("transaction", "系统库事务"):
        session = Session()
        try:
            with measure("connection", "获取连接及健康检查"):
                session.connection()
            yield session
            with measure("flush", "写入 ORM 变更"):
                session.flush()
            with measure("commit", "提交事务并归还连接"):
                session.commit()
        except BaseException:
            with measure("rollback", "回滚事务"):
                session.rollback()
            raise
        finally:
            with measure("close", "关闭 Session"):
                session.close()


@contextmanager
def scan_connection(*, work_mem=False):
    """One exclusively owned raw connection, returned to the existing pool."""
    from psycopg.rows import dict_row
    from psycopg.pq import TransactionStatus
    raw = engine.raw_connection()
    conn = raw.driver_connection
    old_factory, old_autocommit = conn.row_factory, conn.autocommit
    settings = None
    try:
        conn.autocommit = True
        conn.row_factory = dict_row
        settings = conn.execute("SELECT current_setting('statement_timeout') AS statement_timeout, current_setting('lock_timeout') AS lock_timeout, current_setting('work_mem') AS work_mem").fetchone()
        conn.execute("SELECT set_config('statement_timeout','120s',false), set_config('lock_timeout','5s',false)")
        if work_mem:
            conn.execute("SELECT set_config('work_mem',%s,false)",(SCAN_WORK_MEM,))
        yield conn
    finally:
        try:
            if conn.closed:
                raw.invalidate()
            else:
                if conn.info.transaction_status != TransactionStatus.IDLE:
                    conn.rollback()
                if settings:
                    conn.execute("SELECT set_config('statement_timeout',%s,false), set_config('lock_timeout',%s,false)",
                                 (settings["statement_timeout"], settings["lock_timeout"]))
                if work_mem and settings:
                    # RESET first, then restore a pool connection's custom baseline.
                    conn.execute("RESET work_mem")
                    conn.execute("SELECT set_config('work_mem',%s,false)",(settings["work_mem"],))
                conn.row_factory, conn.autocommit = old_factory, old_autocommit
        except Exception:
            raw.invalidate()
        finally:
            raw.close()
