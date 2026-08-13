from __future__ import annotations

from fastapi import APIRouter

from src.chat_ui.schemas import ChatRequest
from src.services.chat_context_service import build_uploaded_context
from src.services.qa_service import handle_chat


router = APIRouter(tags=["chat"])


@router.post("/api/chat")
async def chat(payload: ChatRequest) -> dict[str, object]:
	uploaded_context = ""
	uploaded_sources: list[str] = []
	if payload.include_uploaded_context:
		uploaded_context, uploaded_sources = build_uploaded_context(payload.session_id)
	return await handle_chat(payload, uploaded_context, uploaded_sources)
