"""Document routing and bounded local workbook preview; MaxKB owns final Excel parsing."""

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .pipeline import PipelineError

EXTENSIONS = {".pdf": "pdf", ".docx": "word", ".doc": "word", ".xlsx": "excel", ".xls": "excel"}


def file_kind(name: str) -> str:
    return EXTENSIONS.get(Path(name).suffix.lower(), "")


def validate_source(path: Path) -> None:
    """Check container format, bounded expansion and required Office parts without extracting."""
    ext = path.suffix.lower()
    with path.open("rb") as stream:
        header = stream.read(1024)
    if ext == ".pdf":
        if b"%PDF-" not in header:
            raise PipelineError("文件缺少 PDF 文件头")
    elif ext in {".doc", ".xls"}:
        if not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise PipelineError("文件不是有效的 Office 二进制文档")
    elif ext in {".docx", ".xlsx"}:
        try:
            with ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(e.file_size for e in entries) > 512 * 1024**2:
                    raise PipelineError("Office 文件解压后过大，停止处理")
                part = "word/document.xml" if ext == ".docx" else "xl/workbook.xml"
                if (
                    part not in archive.namelist()
                    or "[Content_Types].xml" not in archive.namelist()
                ):
                    raise PipelineError("文件缺少必要的 Office 文档内容")
                if any(e.flag_bits & 1 for e in entries):
                    raise PipelineError("不支持加密 Office 文档")
        except BadZipFile as exc:
            raise PipelineError("Office 文件容器损坏") from exc
    else:
        raise PipelineError("仅支持 PDF、DOC/DOCX、XLS/XLSX")


def workbook_preview(path: Path, sheet: int = 0, offset: int = 0, limit: int = 20) -> dict:
    """A source-cell preview, never a promise about MaxKB's generated paragraphs."""
    validate_source(path)
    # Preview is optional: large Excel files can still be imported by MaxKB.
    if path.stat().st_size > 20 * 1024**2:
        raise PipelineError("本地预览限 20 MiB 工作簿；可在本地检查原文件后确认入库")
    if path.suffix.lower() == ".xlsx":
        with ZipFile(path) as archive:
            if sum(e.file_size for e in archive.infolist()) > 64 * 1024**2:
                raise PipelineError("工作簿解压后超过预览限制；请在本地检查原文件后确认入库")
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            names = book.sheetnames
            if sheet >= len(names):
                raise PipelineError("工作表不存在")
            ws = book.worksheets[sheet]
            rows = [
                [cell_text(v) for v in row]
                for row in ws.iter_rows(
                    min_row=offset + 1,
                    max_row=offset + limit,
                    max_col=min(ws.max_column or 30, 30),
                    values_only=True,
                )
            ]
            return {
                "sheets": names,
                "sheet": sheet,
                "offset": offset,
                "total_rows": ws.max_row,
                "rows": rows,
                "column_limit": 30,
            }
        finally:
            book.close()
    if path.suffix.lower() != ".xls":
        raise PipelineError("不是 Excel 文件")
    import xlrd

    book = xlrd.open_workbook(str(path), on_demand=True)
    try:
        names = book.sheet_names()
        if sheet >= len(names):
            raise PipelineError("工作表不存在")
        ws = book.sheet_by_index(sheet)
        return {
            "sheets": names,
            "sheet": sheet,
            "offset": offset,
            "total_rows": ws.nrows,
            "rows": [
                [cell_text(ws.cell_value(r, c)) for c in range(min(ws.ncols, 30))]
                for r in range(offset, min(offset + limit, ws.nrows))
            ],
            "column_limit": 30,
        }
    finally:
        book.release_resources()


def cell_text(value) -> str:
    return "" if value is None else str(value)[:2000]
