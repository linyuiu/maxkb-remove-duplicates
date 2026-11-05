"""Match retained MinerU images to rewritten MaxKB references for local preview."""

import hashlib
import json
import re
import threading
import uuid
from collections import OrderedDict
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .pipeline import PipelineError

IMAGE = re.compile(
    r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>|!\[[^\]]*\]\(([^)]+)\)",
    re.IGNORECASE,
)
OSS = re.compile(r"(?:^|/)oss/file/([A-Za-z0-9_-]{1,128})/?$")
MAX_IMAGE_BYTES = 10 * 1024**2
INDEX_VERSION = 3
MAX_INDEX_TEXT_BYTES = 8 * 1024**2


class PreviewBusy(PipelineError):
    pass


def image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def normalized_document(text: str) -> tuple[str, list[str]]:
    sources = []

    def replace(match):
        sources.append(unescape(match[1] or match[2]))
        # Preserve each image's position while ignoring its rewritten URL.
        return " MINERUIMAGEPLACEHOLDER "

    text = IMAGE.sub(replace, text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\W+", "", unescape(text)), sources


def local_image_map(parsed: Path, paragraphs: list[dict]) -> dict[str, str]:
    """Only map images when the complete text and image positions agree.

    MaxKB replaces local URLs with OSS IDs. Comparing filenames or ZIP order
    would be unreliable; compare the complete normalized documents instead.
    If filtering removed content or references changed, leave the inline image
    unassociated; all retained images remain available in the local gallery.
    """
    markdown = list(parsed.rglob("*.md"))
    if len(markdown) != 1:
        return {}
    source = markdown[0]
    if not source.resolve().is_relative_to(parsed.resolve()):
        return {}
    before, local = normalized_document(source.read_text(encoding="utf-8"))
    after, remote = normalized_document(
        "\n".join(p.get("title", "") + "\n" + p["content"] for p in paragraphs)
    )
    if before != after or len(local) != len(remote):
        return {}
    mapping = {}
    for path, reference in zip(local, remote, strict=True):
        try:
            local_url, remote_url = urlsplit(path), urlsplit(reference)
        except ValueError:
            continue
        if local_url.scheme or local_url.netloc or remote_url.scheme or remote_url.netloc:
            continue
        match = OSS.search(remote_url.path)
        if not match:
            continue
        target = (source.parent / unquote(local_url.path)).resolve()
        if not target.is_relative_to(parsed.resolve()) or not target.is_file():
            continue
        relative = target.relative_to(parsed.resolve()).as_posix()
        if match[1] in mapping and mapping[match[1]] != relative:
            return {}  # A rewritten ID must identify exactly one local image.
        mapping[match[1]] = relative
    return mapping


class PreviewIndex:
    """Persist lightweight indexes and keep at most 64 in memory, never image bytes.

    Build on the first preview of a prepared file. Concurrent requests for that
    file share the build; image requests only look up an indexed path. A resplit
    changes prepared_at and invalidates the old index without a DB migration.
    """

    def __init__(self, store, capacity: int = 64):
        self.store, self.capacity = store, capacity
        self.cache = OrderedDict()
        self.cache_lock = threading.Lock()
        self.build_locks = [threading.Lock() for _ in range(16)]
        self.build_slots = threading.BoundedSemaphore(2)

    def get(self, row) -> dict:
        if row["kind"] != "pdf":
            return self.build(self.store.mineru_directory(row["id"]) / "parsed", [], None)
        key = (row["id"], row["prepared_at"])
        with self.build_locks[hash(row["id"]) % len(self.build_locks)]:
            with self.cache_lock:
                if key in self.cache:
                    self.cache.move_to_end(key)
                    return self.cache[key]
            directory = self.store.mineru_directory(row["id"])
            path = directory / "preview-images" / "image-map.json"
            index = None
            try:
                saved = json.loads(path.read_text())
                if (
                    saved.get("version") == INDEX_VERSION
                    and saved.get("prepared_at") == row["prepared_at"]
                    and isinstance(saved.get("assets"), dict)
                    and isinstance(saved.get("images"), dict)
                    and isinstance(saved.get("types"), dict)
                ):
                    index = saved
            except (OSError, ValueError):
                pass
            if index is None:
                if not self.build_slots.acquire(blocking=False):
                    raise PreviewBusy("本地图片索引正在准备，请稍后重试预览")
                try:
                    with self.store.connect() as db:
                        text_bytes = db.execute(
                            "SELECT COALESCE(SUM(octet_length(title) + "
                            "octet_length(content)),0) FROM paragraphs WHERE file_id=?",
                            (row["id"],),
                        ).fetchone()[0]
                    limited = text_bytes > MAX_INDEX_TEXT_BYTES
                    paragraphs = (
                        [] if limited else self.store.preview(row["id"], 0, row["paragraph_count"])
                    )
                    index = self.build(directory / "parsed", paragraphs, row["prepared_at"])
                    index["inline_limited"] = limited
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        temporary.write_text(json.dumps(index, ensure_ascii=False))
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
                finally:
                    self.build_slots.release()
            with self.cache_lock:
                self.cache[key] = index
                self.cache.move_to_end(key)
                while len(self.cache) > self.capacity:
                    self.cache.popitem(last=False)
            return index

    @staticmethod
    def build(parsed: Path, paragraphs: list[dict], prepared_at) -> dict:
        assets = {}
        for target in sorted(parsed.rglob("*")):
            if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                continue
            resolved = target.resolve()
            if not resolved.is_relative_to(parsed.resolve()) or not resolved.is_file():
                continue
            size = resolved.stat().st_size
            if size > MAX_IMAGE_BYTES:
                continue
            with resolved.open("rb") as image:
                kind = image_media_type(image.read(32))
            if not kind:
                continue
            relative = resolved.relative_to(parsed.resolve()).as_posix()
            asset_id = hashlib.sha256(relative.encode()).hexdigest()[:32]
            assets[asset_id] = {"path": relative, "name": target.name, "type": kind, "bytes": size}
        available = {asset["path"] for asset in assets.values()}
        images = {
            key: value
            for key, value in (local_image_map(parsed, paragraphs) if paragraphs else {}).items()
            if value in available
        }
        _, references = normalized_document("\n".join(p["content"] for p in paragraphs))
        ids = set()
        for source in references:
            try:
                url = urlsplit(source)
            except ValueError:
                continue
            if not url.scheme and not url.netloc and (match := OSS.search(url.path)):
                ids.add(match[1])
        return {
            "version": INDEX_VERSION,
            "prepared_at": prepared_at,
            "images": images,
            "assets": assets,
            "types": {asset["path"]: asset["type"] for asset in assets.values()},
            "unmapped": len(ids - images.keys()),
        }
