"""Strict upstream fakes and real files; never fabricate application DOM or database rows."""

import base64
import io
import json
import posixpath
import uuid
import zipfile
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import httpx
from openpyxl import Workbook

PDF = b"%PDF-1.7\n% integration fixture\n%%EOF\n"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jK1sAAAAASUVORK5CYII="
)
CONFIG = {
    "mineru_url": "http://mineru.test",
    "maxkb_base_url": "http://maxkb.test/admin/api",
    "workspace_id": "default",
    "knowledge_id": "pending",
    "maxkb_token": "test-only-maxkb",
    "mineru_key": "test-only-mineru",
    "batch_size": 10,
}
SSH = {
    "name": "远程文档",
    "host": "files.test",
    "username": "reader",
    "port": 22,
    "remote_path": "/documents",
    "auth": "password",
    "password": "test-only-ssh",
}


def ident():
    return str(uuid.uuid4())


def zipped(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return stream.getvalue()


def office(kind):
    if kind == "excel":
        stream = io.BytesIO()
        book = Workbook()
        book.active.append(["问题", "答案"])
        book.active.append(["What?", "Answer"])
        book.save(stream)
        return stream.getvalue()
    return zipped({"[Content_Types].xml": "<Types/>", "word/document.xml": "<document/>"})


def multipart(request):
    content = ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
    message = BytesParser(policy=policy.default).parsebytes(content + request.read())
    return [
        (
            p.get_param("name", header="content-disposition"),
            p.get_filename(),
            p.get_payload(decode=True),
        )
        for p in message.iter_parts()
    ]


class Upstream:
    def __init__(self):
        self.requests, self.overrides = [], []
        self.knowledge, self.tags, self.documents = {}, {}, {}
        self.document_state, self.tag_failure = "2nn2", False
        self.create_outcome = "success"
        self.splits = 0

    def factory(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle), trust_env=False)

    @staticmethod
    def response(data, code=200):
        return httpx.Response(200, json={"code": code, "data": data})

    def handle(self, request):
        self.requests.append(request)
        for override in self.overrides:
            response = override(request)
            if response is not None:
                return response
        path, method = request.url.path, request.method
        if request.url.host == "mineru.test":
            if path == "/v1/health":
                return httpx.Response(200, json={"features": {"output_formats": ["zip"]}})
            if path == "/v1/uploads" and method == "POST":
                return httpx.Response(
                    200, json={"id": "upload", "status": "completed", "file": {"id": "source"}}
                )
            if path.startswith("/v1/parse/jobs"):
                return httpx.Response(
                    200,
                    json={
                        "job_id": "parse",
                        "status": "completed",
                        "files": [
                            {"status": "completed", "output_files": {"zip": {"file_id": "result"}}}
                        ],
                    },
                )
            if path == "/v1/files/result/content":
                return httpx.Response(200, content=zipped({"markdown.md": "# Title\n\nText"}))
        if path.endswith("/model_list"):
            return self.response([{"id": "model", "name": "测试向量模型", "status": "SUCCESS"}])
        if path.endswith("/KNOWLEDGE/folder"):
            return self.response(
                [
                    {
                        "id": "default",
                        "name": "根目录",
                        "parent_id": None,
                        "children": [
                            {
                                "id": "folder",
                                "name": "部门目录",
                                "parent_id": "default",
                                "children": [],
                            }
                        ],
                    }
                ]
            )
        if path.endswith("/knowledge") and method == "GET":
            return self.response(list(self.knowledge.values()))
        if path.endswith("/knowledge/base") and method == "POST":
            payload = json.loads(request.content)
            key = "kb-" + str(len(self.knowledge) + 1)
            self.knowledge[key] = {**payload, "id": key, "workspace_id": "default", "type": 0}
            return self.response(self.knowledge[key])
        if "/knowledge/" in path:
            knowledge_id, _, action = path.split("/knowledge/", 1)[1].partition("/")
            if not action and method == "GET":
                return self.response(
                    self.knowledge.get(knowledge_id), 200 if knowledge_id in self.knowledge else 404
                )
            if action == "tags":
                tags = self.tags.setdefault(knowledge_id, {})
                if method == "POST":
                    for tag in json.loads(request.content):
                        pair = (tag["key"], tag["value"])
                        tags.setdefault(pair, f"tag-{knowledge_id}-{len(tags)}")
                    return self.response(None)
                grouped = {}
                for (key, value), tag_id in tags.items():
                    grouped.setdefault(key, []).append({"id": tag_id, "value": value})
                return self.response([{"key": k, "values": v} for k, v in grouped.items()])
            if action == "document/split":
                self.splits += 1
                _, filename, content = next(p for p in multipart(request) if p[0] == "file")
                if filename.endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        filename = next(n for n in archive.namelist() if n.endswith(".md"))
                return self.response(
                    [
                        {
                            "name": Path(filename).name,
                            "source_file_id": f"oss-{self.splits}",
                            "content": [{"title": "Title", "content": "Text"}],
                        }
                    ]
                )
            if action in {"document/batch_create", "document/table", "document/qa"}:
                if action == "document/batch_create":
                    payload = json.loads(request.content)
                else:
                    _, name, _ = next(p for p in multipart(request) if p[1])
                    payload = [{"name": name, "source_file_id": "excel-source", "paragraphs": [{}]}]
                result = []
                for item in payload:
                    key = f"document-{len(self.documents) + 1}"
                    document = {
                        "id": key,
                        "name": item["name"],
                        "knowledge_id": knowledge_id,
                        "meta": {"source_file_id": item["source_file_id"]},
                        "status": self.document_state,
                        "paragraph_count": max(1, len(item.get("paragraphs", []))),
                    }
                    self.documents[key] = document
                    result.append(document)
                if self.create_outcome == "lost":
                    raise httpx.ReadTimeout("response lost after create", request=request)
                return self.response(result)
            if action.endswith("/tags") or action == "document/batch_add_tag":
                return self.response(None, 500 if self.tag_failure else 200)
            document_id = action.removeprefix("document/")
            if document_id in self.documents:
                if method == "DELETE":
                    del self.documents[document_id]
                    return self.response(True)
                return self.response({**self.documents[document_id], "status": self.document_state})
            if action.endswith("/100"):
                return self.response(
                    {"records": list(self.documents.values()), "total": len(self.documents)}
                )
        raise AssertionError(f"Unexpected upstream request: {method} {request.url}")


class SFTP:
    def __init__(self, root):
        self.root = root
        self.reads, self.connections = [], 0
        self.fail_read_at = None
        self.fail_listing = False

    def path(self, remote):
        assert remote == "/documents" or remote.startswith("/documents/")
        return self.root / posixpath.relpath(remote, "/documents")

    def normalize(self, remote):
        return remote

    @staticmethod
    def attrs(path):
        info = path.lstat()
        return SimpleNamespace(
            filename=path.name,
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_mtime=int(info.st_mtime),
        )

    def stat(self, remote):
        return self.attrs(self.path(remote))

    lstat = stat

    def listdir_iter(self, remote, read_aheads=1):
        if self.fail_listing:
            raise PermissionError("listing denied")
        return iter(self.attrs(p) for p in self.path(remote).iterdir())

    @contextmanager
    def open(self, remote, mode):
        owner = self
        with self.path(remote).open(mode) as stream:

            class File:
                def stat(self):
                    return owner.stat(remote)

                def read(self, size):
                    owner.reads.append((remote, stream.tell(), size))
                    return stream.read(size)

                def readv(self, blocks, **kwargs):
                    for offset, length in blocks:
                        if owner.fail_read_at is not None and offset >= owner.fail_read_at:
                            owner.fail_read_at = None
                            raise EOFError("simulated disconnect")
                        owner.reads.append((remote, offset, length))
                        stream.seek(offset)
                        yield stream.read(length)

            yield File()

    def mkdir(self, remote):
        self.path(remote).mkdir()

    def rename(self, source, target):
        destination = self.path(target)
        if destination.exists():
            raise FileExistsError(target)
        self.path(source).rename(destination)

    @contextmanager
    def factory(self, config):
        self.connections += 1
        yield self


def plan(paths, **options):
    folders = sorted({path.split("/", 1)[0] if "/" in path else "" for path in paths})
    return {
        "root": "",
        "embedding_model_id": "model",
        "groups": [{"folder": folder, "tags": []} for folder in folders],
        **options,
    }


class Workbench:
    def __init__(self, client, upstream, remote, mapped, config):
        self.client, self.upstream, self.remote, self.mapped = client, upstream, remote, mapped
        self.config = config
        self.store, self.workers, self.sources = (
            client.app.state.store,
            client.app.state.workers,
            client.app.state.sources,
        )

    def queue(self, paths=("A/a.pdf",), *, contents=None, config=None, knowledge_plan=None):
        contents = contents or {path: PDF for path in paths}
        body = {
            "id": ident(),
            "config": {**self.config, **(config or {})},
            "files": [{"path": p, "bytes": len(contents[p])} for p in paths],
            "knowledge_plan": knowledge_plan or plan(paths),
        }
        response = self.client.post("/api/ingestion/queues", json=body)
        assert response.status_code == 201, response.text
        queue = response.json()
        for wave in queue["batches"]:
            files = self.store.snapshot(wave["id"])["files"]
            result = self.client.post(
                f"/api/ingestion/queues/{queue['id']}/batches/{wave['id']}",
                files=[
                    ("files", (f["relative_path"], contents[f["relative_path"]])) for f in files
                ],
            )
            assert result.status_code == 201, result.text
        return body, self.store.queue_snapshot(queue["id"])

    def provision(self):
        from app.ingestion.knowledge import KnowledgeProvisioner

        worker = KnowledgeProvisioner(self.workers)
        while worker.run_one():
            pass

    def prepare(self):
        while row := self.store.claim():
            self.workers.execute(row, False)
        while row := self.store.claim(splitting=True):
            self.workers.execute(row, False, phase="split")
        return row

    def import_wave(self, wave_id):
        files = self.store.snapshot(wave_id)["files"]
        ids = [f["id"] for f in files if f["status"] == "review"]
        response = self.client.post(f"/api/ingestion/batches/{wave_id}/import", json={"file_ids": ids})
        assert response.status_code == 202, response.text
        while row := self.store.claim(True):
            self.workers.execute(row, True)
        while row := self.store.claim_monitor():
            with self.upstream.factory() as client:
                self.workers.monitor(row, client)
            if self.store.file(row["id"])["status"] == "import_monitoring":
                break

    def ssh_source(self):
        sid = ident()
        response = self.client.put(f"/api/ingestion/ssh-sources/{sid}", json=SSH)
        assert response.status_code == 200, response.text
        return sid

    def source_job(self, sid, kind):
        jid = ident()
        response = self.client.post(f"/api/ingestion/ssh-sources/{sid}/{kind}", json={"id": jid})
        assert response.status_code == 202, response.text
        assert self.sources.run_one("scan" if kind == "scan" else "sync")
        return self.client.get(f"/api/ingestion/source-jobs/{jid}").json()
