"""Serve the built SPA directly through FastAPI, after all API routes."""
import os
from pathlib import Path, PurePosixPath

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from .config import PROJECT_ROOT


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        parts = PurePosixPath(path).parts
        if (parts and parts[0] == "api") or ".." in parts or any(p.startswith(".") for p in parts if p != "."):
            raise HTTPException(404)
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            # Only page routes get the SPA fallback. Missing JS/CSS/images remain 404.
            if exc.status_code != 404 or PurePosixPath(path).suffix or (parts and parts[0] == "assets"):
                raise
            return FileResponse(Path(self.directory) / "index.html", media_type="text/html")


def install_frontend(app, directory=None):
    explicit = directory is not None or bool(os.environ.get("FRONTEND_DIST_DIR"))
    directory = Path(directory or os.environ.get("FRONTEND_DIST_DIR") or PROJECT_ROOT / "frontend" / "dist")
    if not (directory / "index.html").is_file():
        if explicit:
            raise RuntimeError("前端构建文件不存在，请检查 FRONTEND_DIST_DIR 或构建前端")
        return  # Source development can run Vite separately.
    app.mount("/", FrontendFiles(directory=directory, html=True), name="frontend")
