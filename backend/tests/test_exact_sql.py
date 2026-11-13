"""Execute the production query in real PostgreSQL/pgvector (PGlite WASM).

Install the small test harness with `npm ci --prefix tests/sql` first.
"""

import json
import math
from pathlib import Path
import random
import subprocess
import os
import pytest
from app.source import document_scope
from app.matrix import VectorStore, compare

# Frozen SQL baseline, used only to check the new matrix engine against pgvector.
ANCHORS_SQL = f"""
SELECT id FROM embedding e
WHERE knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND source_type='1'
  AND {document_scope('e')}
  AND (%(start)s::text IS NULL OR id>%(start)s)
ORDER BY id LIMIT %(batch_size)s
"""
COMPARE_SQL = f"""
WITH scores AS (
 SELECT LEAST(a.paragraph_id::text,b.paragraph_id::text) AS left_id,
        GREATEST(a.paragraph_id::text,b.paragraph_id::text) AS right_id,
        1-(a.embedding <=> b.embedding) AS similarity
 FROM embedding a JOIN embedding b ON b.id<a.id
 WHERE a.knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND a.source_type='1'
   AND b.knowledge_id=ANY(%(knowledge_ids)s::uuid[]) AND b.source_type='1'
   AND {document_scope('a')} AND {document_scope('b')}
   AND (%(start)s::text IS NULL OR a.id>%(start)s) AND a.id<=%(end)s
   AND a.paragraph_id<>b.paragraph_id
   AND (NOT %(exclude_doc)s OR a.document_id<>b.document_id)
)
SELECT left_id,right_id,max(similarity) AS similarity FROM scores
WHERE similarity >= %(threshold)s GROUP BY left_id,right_id
"""


def oracle(rows, threshold, exclude_doc):
    pairs = {}
    for i, a in enumerate(rows):
        for b in rows[:i]:
            if a["paragraph_id"] == b["paragraph_id"] or (
                exclude_doc and a["document_id"] == b["document_id"]
            ):
                continue
            score = sum(x * y for x, y in zip(a["vector"], b["vector"])) / math.sqrt(
                sum(x * x for x in a["vector"]) * sum(x * x for x in b["vector"])
            )
            if score >= threshold:
                key = tuple(sorted([a["paragraph_id"], b["paragraph_id"]]))
                pairs[key] = max(score, pairs.get(key, -1))
    return pairs


@pytest.mark.parametrize("seed", [7, 42, 103])
@pytest.mark.parametrize("include_inactive_documents", [True, False])
@pytest.mark.parametrize(
    "threshold,exclude_doc,batch_size",
    [(0, False, 1), (0.8, False, 3), (0.8, True, 2), (1, False, 4)],
)
def test_production_sql_matches_independent_all_pairs(
    threshold, exclude_doc, batch_size, seed, include_inactive_documents
):
    harness = Path(__file__).parent / "sql"
    if not (
        harness / "node_modules/@electric-sql/pglite"
    ).exists() and not os.environ.get("PGLITE_MODULE"):
        pytest.skip(
            "Run npm ci --prefix tests/sql to install PostgreSQL WASM SQL test harness"
        )
    rng = random.Random(seed)
    rows = [
        dict(
            embedding_id="e" + str(i),
            paragraph_id="p" + str(i // 2),
            document_id="d" + str(i // 6),
            vector=[rng.uniform(-1, 1) for _ in range(4)],
        )
        for i in range(18)
    ]
    # Exact duplicates across different paragraphs/documents, orthogonal and opposite vectors.
    rows += [
        dict(
            embedding_id="special" + str(i),
            paragraph_id="special" + str(i),
            document_id="special" + str(i),
            vector=v,
        )
        for i, v in enumerate([[1, 0, 0, 0], [1, 0, 0, 0], [0, 1, 0, 0], [-1, 0, 0, 0]])
    ]
    inactive_documents = ["d1", "special0"]
    payload = dict(
        anchors_sql=ANCHORS_SQL,
        sql=COMPARE_SQL,
        inactive_documents=inactive_documents,
        include_inactive_documents=include_inactive_documents,
        rows=rows,
        threshold=threshold,
        exclude_doc=exclude_doc,
        batch_size=batch_size,
    )
    out = subprocess.run(
        ["node", str(harness / "test_scan.mjs")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    actual = {
        (p["left_id"], p["right_id"]): p["similarity"] for p in json.loads(out.stdout)
    }
    expected_rows = [
        row for row in rows
        if include_inactive_documents or row["document_id"] not in inactive_documents
    ]
    expected = oracle(expected_rows, threshold, exclude_doc)
    assert actual.keys() == expected.keys()
    matrix = {}
    def consume(matches):
        for match in matches:
            key = match["left_id"], match["right_id"]
            matrix[key] = max(matrix.get(key, -1), match["similarity"])
    with VectorStore() as store:
        for row in sorted(expected_rows, key=lambda r: r["embedding_id"]):
            store.append(dict(row, vector=row["vector"]))
        store.flush()
        compared = compare(store, threshold=threshold, exclude_same_document=exclude_doc,
                           block_size=batch_size, threads=1, consume=consume,
                           checkpoint=lambda *args: None)
        assert compared == store.comparisons(exclude_doc)
    assert matrix.keys() == actual.keys() == expected.keys()
    for key in actual:
        assert actual[key] == pytest.approx(expected[key], abs=1e-6)
        assert matrix[key] == pytest.approx(expected[key], abs=1e-6)
