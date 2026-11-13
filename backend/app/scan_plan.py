"""Shared execution metadata for actual scans and phase-based estimates."""

import hashlib
import platform
from sqlalchemy import text
from .bulk_writer import RESULT_BATCH_ROWS, RESULT_BUFFER_BYTES
from .db import SCAN_WORK_MEM
from .matrix import ENGINE, MatrixSettings, available_cpus

# Bump this when computation or persistence changes invalidate phase calibration.
PLAN_VERSION = "matrix-copy-v2"


def matrix_cells(chunks, block_size):
    # Diagonal tiles compute a full square before the triangular mask is applied.
    whole,tail=divmod(chunks,block_size)
    return (chunks*chunks+whole*block_size*block_size+tail*tail)//2


def execution_plan(config, dimension, session, settings=None):
    settings=settings or MatrixSettings.load()
    database=session.execute(text("""SELECT pg_size_bytes(current_setting('shared_buffers')) AS shared_buffers_bytes,
        current_setting('server_version_num')::int / 10000 AS pg_major,
        pg_indexes_size('similarity_pairs'::regclass) AS index_bytes""")).mappings().one()
    identity="|".join((platform.node(),platform.system(),platform.machine(),str(available_cpus())))
    return dict(version=PLAN_VERSION,engine=ENGINE,
                matrix_block_size=settings.block_size(config["batch_size"],dimension or 1),
                matrix_threads=settings.threads,matrix_memory_mb=settings.memory_mb,
                result_batch_rows=RESULT_BATCH_ROWS,result_buffer_bytes=RESULT_BUFFER_BYTES,
                writer_work_mem=SCAN_WORK_MEM,
                shared_buffers_bytes=database["shared_buffers_bytes"],pg_major=database["pg_major"],
                index_bytes=database["index_bytes"],
                worker=hashlib.sha256(identity.encode()).hexdigest()[:16])
