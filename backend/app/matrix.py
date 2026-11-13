"""Exhaustive cosine comparison with bounded arrays and temporary disk snapshots."""

from dataclasses import dataclass
from functools import lru_cache
import math
import os
from pathlib import Path
import sqlite3
import struct
from tempfile import TemporaryDirectory
from .profiling import measure, timed, count

ENGINE = "blocked-matrix-v1"
RESULT_BATCH = 500
MAX_BLOCK_SIZE = 4096
RESERVE_BYTES = 16 * 1024**2


def positive_env(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须介于 {minimum} 和 {maximum} 之间")
    return value


def available_cpus():
    cpus = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    if hasattr(os, "sched_getaffinity"):
        cpus = min(cpus, len(os.sched_getaffinity(0)))
    # Linux containers can expose all host cores while having a smaller CPU quota.
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpus = min(cpus, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return cpus


# Bound native pools before NumPy initializes them, including macOS Accelerate.
_initial_threads = str(min(available_cpus(), positive_env("SCAN_THREADS", 4, 1, 64)))
for _thread_env in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS",
                    "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[_thread_env] = _initial_threads

import numpy as np
from threadpoolctl import threadpool_limits


def working_bytes(block_size, dimension):
    # Two float64 vector blocks, one score tile, metadata/masks and native workspace.
    return RESERVE_BYTES + 16 * block_size * dimension + 8 * block_size**2 + 4096 * block_size


@dataclass(frozen=True)
class MatrixSettings:
    memory_mb: int
    threads: int

    @classmethod
    def load(cls):
        return cls(
            positive_env("SCAN_MEMORY_MB", 2048, 32, 2048),
            min(available_cpus(), positive_env("SCAN_THREADS", 4, 1, 64)),
        )

    def block_size(self, requested, dimension):
        if not 1 <= requested <= MAX_BLOCK_SIZE:
            raise ValueError(f"矩阵块大小必须介于 1 和 {MAX_BLOCK_SIZE} 之间")
        lo, hi = 0, requested
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if working_bytes(mid, dimension) <= self.memory_mb * 1024**2:
                lo = mid
            else:
                hi = mid - 1
        if not lo:
            raise ValueError("扫描内存预算不足以容纳一个向量块")
        return lo


@dataclass
class VectorBlock:
    values: np.ndarray
    embedding_ids: list
    paragraph_ids: list
    paragraph_keys: np.ndarray
    document_keys: np.ndarray


class VectorStore:
    """Sequential binary vectors plus a disk-backed, bounded-cache metadata index.

    Read blocks into ordinary arrays instead of mapping the entire file: this also
    bounds resident vector pages on systems where mmap pages are charged to a cgroup.
    """

    def __init__(self):
        self.directory = None
        self.file = None
        self.db = None
        self.count = 0
        self.dimension = None

    def __enter__(self):
        root = os.environ.get("SCAN_TEMP_DIR")
        if not root and os.environ.get("INGESTION_DATA_DIR"):
            root = str(Path(os.environ["INGESTION_DATA_DIR"]) / ".scan-tmp")
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        self.directory = TemporaryDirectory(prefix="maxkb-scan-", dir=root)
        try:
            self.file = open(Path(self.directory.name) / "vectors.bin", "w+b", buffering=0)
            self.db = sqlite3.connect(Path(self.directory.name) / "metadata.sqlite")
            self.db.executescript("""
                PRAGMA cache_size=-2048;
                PRAGMA temp_store=FILE;
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                CREATE TABLE groups (
                    id INTEGER PRIMARY KEY, kind TEXT, source_id TEXT,
                    UNIQUE(kind, source_id)
                );
                CREATE TABLE paragraph_counts (
                    paragraph_key INTEGER PRIMARY KEY, chunks INTEGER
                );
                CREATE TABLE chunks (
                    ordinal INTEGER PRIMARY KEY, embedding_id TEXT,
                    paragraph_key INTEGER, document_key INTEGER
                );
            """)
            self.group_key = lru_cache(maxsize=2048)(self._group_key)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if hasattr(self, "group_key"):
            self.group_key.cache_clear()
        try:
            if self.file:
                self.file.close()
            if self.db:
                self.db.close()
        finally:
            if self.directory:
                self.directory.cleanup()

    def _group_key(self, kind, source_id):
        self.db.execute("INSERT OR IGNORE INTO groups(kind,source_id) VALUES (?,?)", (kind, source_id))
        return self.db.execute("SELECT id FROM groups WHERE kind=? AND source_id=?", (kind, source_id)).fetchone()[0]

    def append(self, row):
        raw = row["vector"]
        if isinstance(raw, (bytes, bytearray, memoryview)):
            # pgvector's send format: network-endian int16 dimension/reserved,
            # followed by the stored float32 coordinates. No decimal conversion.
            if len(raw) < 4:
                raise ValueError(f"向量 {row['embedding_id']} 的二进制格式无效")
            dimension, reserved = struct.unpack_from("!HH", raw)
            if not 1 <= dimension <= 16000 or reserved or len(raw) != 4 + dimension * 4:
                raise ValueError(f"向量 {row['embedding_id']} 的二进制格式无效")
            vector = np.frombuffer(raw, dtype=">f4", offset=4).astype(np.float64)
        elif isinstance(raw, str):
            # Round to pgvector's stored float32 values before computing in float64.
            vector = np.fromstring(raw.strip()[1:-1], sep=",", dtype=np.float32).astype(np.float64)
        else:
            vector = np.asarray(raw, dtype=np.float32).astype(np.float64)
        if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
            raise ValueError(f"向量 {row['embedding_id']} 为空或包含非有限值")
        if self.dimension is not None and vector.size != self.dimension:
            raise ValueError("向量维度不一致")
        scale = np.max(np.abs(vector))
        if scale <= 0:
            raise ValueError(f"向量 {row['embedding_id']} 是零向量")
        vector /= scale
        vector /= math.sqrt(float(vector @ vector))
        self.dimension = vector.size
        data = memoryview(vector).cast("B")
        while data:
            written = self.file.write(data)
            if not written:
                raise OSError("无法写入临时向量快照")
            data = data[written:]
        self.db.execute(
            "INSERT INTO chunks VALUES (?,?,?,?)",
            (self.count, str(row["embedding_id"]), self.group_key("p", row["paragraph_id"]),
             self.group_key("d", row["document_id"])),
        )
        self.count += 1

    def flush(self):
        self.db.commit()

    def prepare_paragraphs(self):
        self.db.execute("""
            INSERT INTO paragraph_counts
            SELECT paragraph_key, count(*) FROM chunks GROUP BY paragraph_key
        """)
        self.flush()
        return self.db.execute("SELECT count(*) FROM paragraph_counts").fetchone()[0]

    def paragraph_chunks(self, paragraph_id):
        row = self.db.execute("""
            SELECT c.chunks FROM paragraph_counts c JOIN groups p ON p.id=c.paragraph_key
            WHERE p.kind='p' AND p.source_id=?
        """, (paragraph_id,)).fetchone()
        if row is None:
            raise ValueError(f"段落 {paragraph_id} 不在本次向量快照中")
        return row[0]

    def comparisons(self, exclude_same_document):
        column = "document_key" if exclude_same_document else "paragraph_key"
        excluded = self.db.execute(
            f"SELECT coalesce(sum(n*(n-1)/2),0) FROM (SELECT count(*) n FROM chunks GROUP BY {column})"
        ).fetchone()[0]
        return self.count * (self.count - 1) // 2 - int(excluded)

    @timed("block_read", "读取本地矩阵块")
    def read_block(self, start, width):
        size = min(width, self.count - start)
        with measure("vectors", "读取本地向量文件"):
            values = np.empty((size, self.dimension), dtype=np.float64)
            self.file.seek(start * self.dimension * 8)
            target = memoryview(values).cast("B")
            offset = 0
            while offset < len(target):
                size_read = self.file.readinto(target[offset:])
                if not size_read:
                    raise ValueError("临时向量快照不完整")
                offset += size_read
        with measure("metadata", "读取本地块索引及组装"):
            rows = self.db.execute("""
                SELECT c.embedding_id, p.source_id, c.paragraph_key, c.document_key
                FROM chunks c JOIN groups p ON p.id=c.paragraph_key
                WHERE c.ordinal>=? AND c.ordinal<? ORDER BY c.ordinal
            """, (start, start + size)).fetchall()
            if len(rows) != size:
                raise ValueError("临时向量索引不完整")
            return VectorBlock(
                values, [r[0] for r in rows], [r[1] for r in rows],
                np.array([r[2] for r in rows], dtype=np.int64),
                np.array([r[3] for r in rows], dtype=np.int64),
            )


def compare(store, *, threshold, exclude_same_document, block_size, threads,
            consume, checkpoint):
    """Visit each unordered chunk pair once, streaming at most 500 unique pairs.

    checkpoint(completed_comparisons, completed_chunks) is also the cancellation
    boundary. BLAS uses one bounded thread pool, never nested Python worker pools.
    """
    completed = 0
    with threadpool_limits(limits=threads, user_api="blas"):
        for a_start in range(0, store.count, block_size):
            a = store.read_block(a_start, block_size)
            for b_start in range(0, a_start + 1, block_size):
                checkpoint(completed, a_start)
                b = a if b_start == a_start else store.read_block(b_start, block_size)
                with measure("multiply", "矩阵乘法"):
                    scores = a.values @ b.values.T
                count("matrix_tiles")
                with measure("filter", "阈值过滤及段落对聚合"):
                    np.clip(scores, -1.0, 1.0, out=scores)
                    pending = {}
                    for i, row in enumerate(scores):
                        # Normalize tiny endpoint rounding errors (e.g. an exact duplicate).
                        row[np.abs(row - 1.0) <= 8 * np.finfo(np.float64).eps] = 1.0
                        valid = b.paragraph_keys != a.paragraph_keys[i]
                        if exclude_same_document:
                            valid &= b.document_keys != a.document_keys[i]
                        if a_start == b_start:
                            valid[i:] = False
                        completed += int(np.count_nonzero(valid))
                        valid &= row >= threshold
                        for j in np.flatnonzero(valid):
                            left, right = a.paragraph_ids[i], b.paragraph_ids[j]
                            left_e, right_e = a.embedding_ids[i], b.embedding_ids[j]
                            if left > right:
                                left, right, left_e, right_e = right, left, right_e, left_e
                            match = dict(left_id=left, right_id=right, similarity=float(row[j]),
                                         left_embedding_id=left_e, right_embedding_id=right_e)
                            key = (left, right)
                            old = pending.get(key)
                            if old is None or match["similarity"] > old["similarity"] or (
                                match["similarity"] == old["similarity"]
                                and (left_e, right_e) < (old["left_embedding_id"], old["right_embedding_id"])
                            ):
                                pending[key] = match
                            if len(pending) >= RESULT_BATCH:
                                consume(list(pending.values()))
                                pending.clear()
                        if i % 64 == 0:
                            checkpoint(completed, a_start)
                    if pending:
                        consume(list(pending.values()))
                del row, scores, b, pending
                checkpoint(completed, min(a_start + block_size, store.count)
                           if b_start == a_start else a_start)
            del a
    return completed
