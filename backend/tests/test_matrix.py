"""Independent exact oracle, resource bounds and cancellation for the matrix engine."""
import math
from pathlib import Path
import random
import struct
import tracemalloc

import numpy as np
import pytest

from app.matrix import MatrixSettings, VectorStore, compare, working_bytes, RESULT_BATCH


def add(store, i, vector, paragraph=None, document=None):
    store.append(dict(embedding_id=f"e{i:06}", paragraph_id=paragraph or f"p{i}",
                      document_id=document or f"d{i}", vector=vector))


@pytest.mark.parametrize("block", [1, 2, 7, 32])
@pytest.mark.parametrize("exclude_doc", [True, False])
@pytest.mark.parametrize("threshold", [0, 0.7, 0.9, 1])
def test_exact_pairs_and_max_scores_across_blocks(block, exclude_doc, threshold):
    rng = random.Random(42)
    rows = [(np.array([rng.uniform(-1, 1) for _ in range(9)], dtype=np.float32).astype(float),
             f"p{i // 2}", f"d{i // 6}") for i in range(30)]
    rows += [(np.ones(9), "duplicate", "other"), (np.ones(9), "duplicate2", "other2")]
    expected = {}
    comparisons = 0
    for i, (a, pa, da) in enumerate(rows):
        for b, pb, db in rows[:i]:
            if pa == pb or (exclude_doc and da == db):
                continue
            comparisons += 1
            score = math.fsum(float(x) * float(y) for x, y in zip(a, b)) / math.sqrt(
                math.fsum(float(x) ** 2 for x in a) * math.fsum(float(y) ** 2 for y in b))
            if score >= threshold:
                key = tuple(sorted((pa, pb)))
                expected[key] = max(score, expected.get(key, -1))
    actual = {}
    progress = []
    def consume(matches):
        assert len(matches) <= RESULT_BATCH
        for row in matches:
            key = row["left_id"], row["right_id"]
            actual[key] = max(row["similarity"], actual.get(key, -1))
    with VectorStore() as store:
        for i, (v, p, d) in enumerate(rows):
            add(store, i, v, p, d)
        store.flush()
        done = compare(store, threshold=threshold, exclude_same_document=exclude_doc,
                       block_size=block, threads=1, consume=consume,
                       checkpoint=lambda completed, chunks: progress.append((completed, chunks)))
        assert done == comparisons == store.comparisons(exclude_doc)
    assert actual.keys() == expected.keys()
    for key, score in expected.items():
        assert actual[key] == pytest.approx(score, abs=2e-14)
    assert progress == sorted(progress)
    assert progress[-1] == (comparisons, len(rows))


def test_memory_budget_shrinks_blocks_and_validates_settings(monkeypatch):
    monkeypatch.setenv("SCAN_MEMORY_MB", "32")
    monkeypatch.setenv("SCAN_THREADS", "2")
    settings = MatrixSettings.load()
    width = settings.block_size(4096, 768)
    assert 1 < width < 4096
    assert working_bytes(width, 768) <= 32 * 1024**2
    assert working_bytes(width + 1, 768) > 32 * 1024**2
    for name, value in [("SCAN_MEMORY_MB", "0"), ("SCAN_MEMORY_MB", "oops"),
                        ("SCAN_MEMORY_MB", "2049"),
                        ("SCAN_THREADS", "0"), ("SCAN_THREADS", "65")]:
        with monkeypatch.context() as patch:
            patch.setenv(name, value)
            with pytest.raises(ValueError, match=name):
                MatrixSettings.load()


@pytest.mark.parametrize("vector", [[], [0, 0], [1, float("nan")], [1, float("inf")]])
def test_invalid_vectors_and_cleanup(vector, tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN_TEMP_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        with VectorStore() as store:
            add(store, 0, vector)
    assert list(tmp_path.iterdir()) == []


def test_dimension_mismatch_and_empty_store():
    with VectorStore() as store:
        assert compare(store, threshold=0, exclude_same_document=False, block_size=10,
                       threads=1, consume=lambda rows: pytest.fail("unexpected match"),
                       checkpoint=lambda *args: None) == 0
        add(store, 0, [1, 0])
        with pytest.raises(ValueError, match="维度"):
            add(store, 1, [1, 0, 0])


def test_dense_matches_bounded_sink_and_ties_are_deterministic():
    winners = {}
    batches = []
    with VectorStore() as store:
        for i in range(85):
            add(store, i, [3, 4], paragraph=f"p{i % 70:02}")
        store.flush()
        def consume(rows):
            batches.append(len(rows))
            for row in rows:
                key = row["left_id"], row["right_id"]
                candidate = row["similarity"], row["left_embedding_id"], row["right_embedding_id"]
                old = winners.get(key)
                if old is None or candidate[0] > old[0] or candidate[0] == old[0] and candidate[1:] < old[1:]:
                    winners[key] = candidate
        compare(store, threshold=1, exclude_same_document=False, block_size=85,
                threads=1, consume=consume, checkpoint=lambda *args: None)
    assert len(winners) == 70 * 69 // 2
    assert max(batches) == RESULT_BATCH
    assert all(score == 1 for score, *_ in winners.values())
    assert winners["p00", "p01"][1:] == ("e000000", "e000001")


def test_cancellation_releases_files_and_restores_blas_threads(tmp_path, monkeypatch):
    from threadpoolctl import threadpool_info
    monkeypatch.setenv("SCAN_TEMP_DIR", str(tmp_path))
    previous = [(p["prefix"], p["num_threads"]) for p in threadpool_info() if p["user_api"] == "blas"]
    class Cancelled(Exception):
        pass
    def stop(*args):
        raise Cancelled()
    with pytest.raises(Cancelled):
        with VectorStore() as store:
            add(store, 0, [1, 0])
            store.flush()
            compare(store, threshold=0, exclude_same_document=False, block_size=1,
                    threads=1, consume=lambda rows: None, checkpoint=stop)
    assert list(tmp_path.iterdir()) == []
    assert previous == [(p["prefix"], p["num_threads"]) for p in threadpool_info() if p["user_api"] == "blas"]


def test_vector_count_does_not_allocate_a_full_vector_or_score_matrix():
    # Includes snapshot metadata/cache allocations as well as NumPy's working arrays.
    tracemalloc.start()
    try:
        with VectorStore() as store:
            for i in range(2500):
                add(store, i, [1] + [0] * 63, document="same")
            store.flush()
            compare(store, threshold=0.9, exclude_same_document=True, block_size=128,
                    threads=1, consume=lambda rows: pytest.fail("same document"),
                    checkpoint=lambda *args: None)
            _, peak = tracemalloc.get_traced_memory()
            assert peak < 8 * 1024**2
    finally:
        tracemalloc.stop()


def test_binary_vectors_match_float32_text_and_paragraph_counts():
    rng = np.random.default_rng(2026)
    values = rng.normal(size=(20, 768)).astype(np.float32)
    with VectorStore() as binary, VectorStore() as text:
        for i, vector in enumerate(values):
            raw = struct.pack("!HH", vector.size, 0) + vector.astype(">f4").tobytes()
            add(binary, i, raw, paragraph=f"p{i % 3}")
            add(text, i, "[" + ",".join(str(float(v)) for v in vector) + "]", paragraph=f"p{i % 3}")
        assert binary.prepare_paragraphs() == 3
        assert [binary.paragraph_chunks(f"p{i}") for i in range(3)] == [7, 7, 6]
        with pytest.raises(ValueError, match="不在本次向量快照"):
            binary.paragraph_chunks("missing")
        assert np.array_equal(binary.read_block(0, 20).values, text.read_block(0, 20).values)


@pytest.mark.parametrize("raw", [
    b"", b"abc", struct.pack("!HH", 0, 0), struct.pack("!HHf", 1, 1, 1),
    struct.pack("!HHf", 2, 0, 1), struct.pack("!HHff", 1, 0, 1, 2),
    struct.pack("!HHf", 1, 0, float("inf")), struct.pack("!HHf", 1, 0, 0),
])
def test_invalid_binary_vectors_are_rejected(raw):
    with VectorStore() as store:
        with pytest.raises(ValueError):
            add(store, 0, raw)
        assert store.count == 0
