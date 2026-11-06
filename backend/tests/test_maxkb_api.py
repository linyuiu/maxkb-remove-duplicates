import json
from types import SimpleNamespace
import httpx
import pytest
from app.maxkb_api import MaxKBAPI, APIError, normalize_base
from app.security import encrypt

TARGET = {
    "workspace_id": "default",
    "knowledge_id": "knowledge",
    "document_id": "document",
    "paragraph_id": "p31",
}


def source():
    return SimpleNamespace(
        api_base_url="http://maxkb.test/admin/api",
        api_key=encrypt("Bearer unit-test-key"),
    )


def test_paginated_document():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer unit-test-key"
        page = int(request.url.path.split("/")[-2])
        rows = [
            {
                "id": f"p{i}",
                "document_id": "document",
                "title": " 标题",
                "content": "正文\n",
            }
            for i in range((page - 1) * 30, min(page * 30, 35))
        ]
        return httpx.Response(
            200, json={"code": 200, "data": {"total": 35, "records": rows}}
        )

    with MaxKBAPI(source(), transport=httpx.MockTransport(handler)) as api:
        result = api.current(TARGET)
    assert result["paragraph_id"] == "p31" and result["content"] == "正文\n"
    assert len(requests) == 2 and requests[1].url.path.endswith("/2/30")
    assert all(r.method == "GET" for r in requests)


def test_put_and_delete():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "data": True})

    payload = {"title": " 标题", "content": "\n修改后的文本\n111"}
    with MaxKBAPI(source(), transport=httpx.MockTransport(handler)) as api:
        api.mutate("update_paragraph", TARGET, payload)
        api.mutate("delete_paragraph", TARGET, None)
    assert requests[0].method == "PUT" and json.loads(requests[0].content) == payload
    assert requests[1].method == "DELETE" and requests[1].url.path.endswith("/p31")


def test_timeout_never_retries():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    with MaxKBAPI(source(), transport=httpx.MockTransport(handler)) as api:
        with pytest.raises(APIError) as err:
            api.mutate("delete_paragraph", TARGET, None)
    assert err.value.uncertain and len(calls) == 1


def test_business_error():
    with MaxKBAPI(
        source(),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 500})
        ),
    ) as api:
        with pytest.raises(APIError):
            api.document(TARGET)


def test_scope_validation():
    body = {
        "code": 200,
        "data": {
            "total": 1,
            "records": [
                {"id": "p31", "document_id": "other", "title": "", "content": "text"}
            ],
        },
    }
    with MaxKBAPI(
        source(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
    ) as api:
        with pytest.raises(APIError):
            api.current(TARGET)


def test_base_validation():
    assert (
        normalize_base("http://maxkb.test:18080/")
        == "http://maxkb.test:18080/admin/api"
    )
    with pytest.raises(ValueError):
        normalize_base("http://user:password@host")


def test_document_delete_clean_url_without_vue_payload():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 200, "data": True})

    with MaxKBAPI(source(), transport=httpx.MockTransport(handler)) as api:
        assert api.mutate("delete_document", TARGET, None) is True
    request = calls[0]
    assert request.method == "DELETE"
    assert (
        request.url.path
        == "/admin/api/workspace/default/knowledge/knowledge/document/document"
    )
    assert not request.url.query and not request.content


def test_whole_document_hash_detects_other_paragraph_edit():
    from app.maxkb_api import document_hash

    rows = [
        {"id": "first", "title": "标题", "content": "same", "position": 1},
        {"id": "second", "content": "original", "position": 2},
    ]
    before = document_hash(rows)
    assert document_hash(list(reversed(rows))) == before
    rows[1]["content"] = "changed"
    assert document_hash(rows) != before


def test_document_delete_requires_positive_ack():
    with MaxKBAPI(
        source(),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 200, "data": False})
        ),
    ) as api:
        with pytest.raises(APIError) as exc:
            api.mutate("delete_document", TARGET, None)
    assert exc.value.uncertain


@pytest.mark.parametrize("paragraph_id,expected_pages", [("p0", 1), ("p31", 2), ("missing", 3)])
def test_current_stops_at_matching_page(paragraph_id, expected_pages):
    calls = []
    def handler(request):
        page = int(request.url.path.split("/")[-2])
        calls.append(page)
        rows = [{"id": f"p{i}", "document_id": "document", "title": "", "content": "text"}
                for i in range((page - 1) * 30, min(page * 30, 65))]
        return httpx.Response(200, json={"code": 200, "data": {"total": 65, "records": rows}})
    with MaxKBAPI(source(), transport=httpx.MockTransport(handler)) as api:
        result = api.current({**TARGET, "paragraph_id": paragraph_id})
    assert calls == list(range(1, expected_pages + 1))
    assert (result is None) == (paragraph_id == "missing")
    calls.clear()
    with MaxKBAPI(source(), transport=httpx.MockTransport(handler)) as api:
        assert len(api.document(TARGET)) == 65
    assert calls == [1, 2, 3]


def test_current_rejects_changing_page_total():
    def handler(request):
        page = int(request.url.path.split("/")[-2])
        return httpx.Response(200, json={"code": 200, "data": {
            "total": 60 if page == 1 else 61,
            "records": [{"id": f"p{page}", "document_id": "document", "content": "text"}],
        }})
    with MaxKBAPI(source(), transport=httpx.MockTransport(handler)) as api:
        with pytest.raises(APIError, match="数量变化"):
            api.current(TARGET)


def test_document_preview_includes_paragraph_hash(monkeypatch):
    from app import main
    from app.security import content_hash
    target = {**TARGET, "document_name": "Document"}
    monkeypatch.setattr(main, "pair_target", lambda *args: (None, None, source(), target))
    class Client:
        def __init__(self, source): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def document(self, target):
            return [{"id": "p31", "title": "Title", "content": "Body"}]
    monkeypatch.setattr(main, "MaxKBAPI", Client)
    result = main.document_content("pair", "left", s=None, user=None)
    assert result["records"][0]["content_hash"] == content_hash("Title", "Body")
