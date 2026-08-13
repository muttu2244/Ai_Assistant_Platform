from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from src.chat_ui.config import LANDING_LOGO_PATH, TEMPLATE_DIR


router = APIRouter(tags=["ui"])


@router.get("/", response_class=HTMLResponse)
async def root() -> FileResponse:
	return FileResponse(TEMPLATE_DIR / "index.html")


@router.get("/brand-logo")
async def brand_logo() -> FileResponse:
	return FileResponse(
		LANDING_LOGO_PATH,
		headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"},
	)
