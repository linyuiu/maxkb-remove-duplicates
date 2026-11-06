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
from app.scanning import COMPARE_SQL


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
@pytest.mark.parametrize(
    "threshold,exclude_doc,batch_size",
    [(0, False, 1), (0.8, False, 3), (0.8, True, 2), (1, False, 4)],
)
def test_production_sql_matches_independent_all_pairs(
    threshold, exclude_doc, batch_size, seed
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
    payload = dict(
        sql=COMPARE_SQL,
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
    expected = oracle(rows, threshold, exclude_doc)
    assert actual.keys() == expected.keys()
    for key in actual:
        assert actual[key] == pytest.approx(expected[key], abs=1e-6)
