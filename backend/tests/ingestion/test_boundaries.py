"""Pure boundary tests for untrusted URLs, archives, formats and upstream status data."""

import zipfile

import httpx
import pytest
from .support import PNG, office, zipped

from app.ingestion.formats import file_kind, validate_source, workbook_preview
from app.ingestion.imports import created_documents, task_state
from app.ingestion.main import render_markdown
from app.ingestion.pipeline import (
    MaxKBClient,
    PipelineError,
    Settings,
    bearer,
    build_documents,
    origin,
    prepare_archive,
)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("a.PDF", "pdf"),
        ("a.docx", "word"),
        ("a.xls", "excel"),
        ("a.xlsx", "excel"),
        ("a.txt", ""),
        ("a.pdf.exe", ""),
    ],
)
def test_supported_file_types(name, kind):
    assert file_kind(name) == kind


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://user:pass@example.com", "x", "ftp://host"]
)
def test_unsafe_service_origins_are_rejected(url):
    with pytest.raises(PipelineError):
        origin(url)


@pytest.mark.parametrize("token", ["a\nb", "a\rb", "Bearer abc\nheader"])
def test_credentials_cannot_inject_headers(token):
    with pytest.raises(PipelineError):
        bearer(token)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("2nn2", "success"),
        ("nnn2", "success"),
        ("1nn2", "pending"),
        ("2nn0", "pending"),
        ("2nn1", "pending"),
        ("2nn3", "failed"),
        ("2nn4", "failed"),
        ("2nn5", "failed"),
        ("3nn2", "failed"),
    ],
)
def test_upstream_state_requires_both_embedding_and_tokenization(status, expected):
    assert task_state({"status": status}) == expected


@pytest.mark.parametrize("status", [None, True, 2, "", "done", "9999", "2" * 21])
def test_unknown_status_cannot_confirm_import(status):
    with pytest.raises(PipelineError):
        task_state({"status": status})


@pytest.mark.parametrize("code", [None, True, "200", 500])
def test_business_response_requires_integer_success(code):
    settings = Settings(
        mineru_url="http://mineru.test",
        maxkb_base_url="http://maxkb.test/api",
        workspace_id="default",
        knowledge_id="kb",
        maxkb_token="test",
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"code": code, "data": None})
            )
        ) as client,
        pytest.raises(PipelineError),
    ):
        MaxKBClient(settings, client).tag_document("doc", ["tag"])


@pytest.mark.parametrize(
    "name", ["../escape.md", "/escape.md", "a/../../escape.md", "C:/escape.md"]
)
def test_archive_paths_cannot_escape_destination(tmp_path, name):
    source = tmp_path / "input.zip"
    source.write_bytes(zipped({name: "# content"}))
    with pytest.raises(PipelineError):
        prepare_archive(source, tmp_path / "output.zip", tmp_path / "parsed")
    assert not (tmp_path / "output.zip").exists()


def test_archive_keeps_only_markdown_and_referenced_images(tmp_path):
    source = tmp_path / "input.zip"
    source.write_bytes(
        zipped(
            {
                "markdown.md": "# T\n![image](images/one.png)",
                "images/one.png": PNG,
                "unused.json": "{}",
                "unused.pdf": b"pdf",
            }
        )
    )
    target = tmp_path / "maxkb.zip"
    prepare_archive(source, target, tmp_path / "parsed", markdown_name="manual.md")
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {"manual.md", "images/one.png"}
    assert (tmp_path / "parsed/manual.md").exists()


def test_missing_local_image_blocks_split_archive(tmp_path):
    source = tmp_path / "input.zip"
    source.write_bytes(zipped({"manual.md": "![image](missing.png)"}))
    with pytest.raises(PipelineError, match="缺失"):
        prepare_archive(source, tmp_path / "maxkb.zip", tmp_path / "parsed")


@pytest.mark.parametrize("kind,extension", [("word", ".docx"), ("excel", ".xlsx")])
def test_real_office_container_is_accepted_and_corruption_rejected(tmp_path, kind, extension):
    path = tmp_path / ("file" + extension)
    path.write_bytes(office(kind))
    validate_source(path)
    path.write_bytes(b"not a real office archive")
    with pytest.raises(PipelineError):
        validate_source(path)


def test_workbook_preview_reads_actual_cells(tmp_path):
    path = tmp_path / "table.xlsx"
    path.write_bytes(office("excel"))
    result = workbook_preview(path)
    assert result["rows"][1] == ["What?", "Answer"]
    with pytest.raises(PipelineError):
        workbook_preview(path, sheet=5)


def test_duplicate_split_names_cannot_be_associated_to_imports():
    row = {"name": "a.md", "source_file_id": "oss", "content": [{"content": "text"}]}
    with pytest.raises(PipelineError, match="重复"):
        build_documents({"data": [row, row]})


def test_created_document_names_must_match_expected_import():
    with pytest.raises(PipelineError):
        created_documents(
            {"data": [{"id": "doc", "name": "wrong"}]},
            [{"name": "expected", "source_file_id": "owned-source"}],
        )


def test_markdown_does_not_execute_scripts_or_event_handlers():
    html = render_markdown('<script>alert(1)</script><img src="x" onerror="alert(1)">', "file")
    assert "<script" not in html and "onerror" not in html
