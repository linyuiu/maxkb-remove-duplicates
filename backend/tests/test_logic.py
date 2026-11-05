import math
from datetime import datetime, timezone
import pytest
from app.source import parse_vector, validated_ids
from app.jobs import next_fire
from app.security import content_hash
from app.main import TaskBody, task_values


def test_vector_validation():
    assert parse_vector("[1,2,0]", 3) == [1, 2, 0]
    for value in ["[]", "[0,0]", "[1,NaN]", "[1,Infinity]", '["1",2]']:
        with pytest.raises(ValueError):
            parse_vector(value)
    with pytest.raises(ValueError):
        parse_vector("[1,2]", 3)


def test_ids_are_canonical_and_deduplicated():
    value = "ABCDEFAB-1234-5678-1234-123456789ABC"
    assert validated_ids([value, value.lower()]) == [value.lower()]
    with pytest.raises(ValueError):
        validated_ids(["invalid"])


def test_schedule_timezone():
    base = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)
    assert next_fire("0 2 * * *", "Asia/Shanghai", base) == datetime(
        2026, 11, 5, 18, 0, tzinfo=timezone.utc
    )


def test_task_validation():
    body = TaskBody(
        name="scan",
        source_id="source",
        knowledge_ids=["abcdefab-1234-5678-1234-123456789abc"],
    )
    assert task_values(body)["next_fire"] is None
    with pytest.raises(Exception):
        TaskBody(name="scan", source_id="s", knowledge_ids=["x"], threshold=math.nan)
    with pytest.raises(Exception):
        task_values(body.model_copy(update={"enabled": True}))


def test_content_fingerprint_includes_title_and_delimiters():
    assert content_hash("a", "bc") != content_hash("ab", "c")
    assert content_hash("标题", "正文") == content_hash("标题", "正文")
