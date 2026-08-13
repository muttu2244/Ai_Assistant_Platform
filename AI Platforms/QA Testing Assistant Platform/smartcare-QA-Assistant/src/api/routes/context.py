from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from src.services.chat_context_service import (
	build_uploaded_context,
	context_db_connection,
	delete_context_document,
	fetch_context_docs,
	max_context_chars,
	max_context_chars_per_file,
	max_files_per_session,
	read_upload_text,
	trim_context,
)


router = APIRouter(tags=["context"])


@router.post("/api/context/upload")
async def upload_context_files(files: list[UploadFile] = File(...), session_id: str | None = Form(None)) -> dict[str, object]:
	active_session_id = (session_id or "").strip() or str(uuid4())
	existing_docs = fetch_context_docs(active_session_id)
	if len(existing_docs) >= max_files_per_session():
		raise HTTPException(status_code=400, detail=f"Session already has max {max_files_per_session()} files")
	uploaded_documents: list[dict[str, object]] = []
	remaining_slots = max(0, max_files_per_session() - len(existing_docs))
	for upload in files[:remaining_slots]:
		text, size_bytes = await read_upload_text(upload)
		text = trim_context(text, max_chars=max_context_chars_per_file())
		document_id = str(uuid4())
		from src.chat_ui.config import UPLOAD_CONTEXT_DIR
		storage_path = UPLOAD_CONTEXT_DIR / f"{document_id}.txt"
		storage_path.write_text(text, encoding="utf-8")
		uploaded_at = datetime.now(timezone.utc).isoformat()
		with context_db_connection() as conn:
			conn.execute(
				"""
				INSERT INTO uploaded_context_documents
				(document_id, session_id, filename, storage_path, size_bytes, char_count, uploaded_at)
				VALUES (?, ?, ?, ?, ?, ?, ?)
				""",
				(document_id, active_session_id, upload.filename or "uploaded_file", str(storage_path), int(size_bytes), len(text), uploaded_at),
			)
		uploaded_documents.append({"document_id": document_id, "filename": upload.filename or "uploaded_file", "chars": len(text), "size_bytes": size_bytes})
		docs_after_upload = fetch_context_docs(active_session_id)
		total_chars = sum(int(row["char_count"]) for row in docs_after_upload)
		while total_chars > max_context_chars() and docs_after_upload:
			oldest = docs_after_upload.pop(0)
			total_chars -= int(oldest["char_count"])
			delete_context_document(str(oldest["document_id"]))
	retained_docs = fetch_context_docs(active_session_id)
	retained_ids = {str(row["document_id"]) for row in retained_docs}
	retained_uploads = [doc for doc in uploaded_documents if str(doc["document_id"]) in retained_ids]
	return {"session_id": active_session_id, "uploaded_documents": retained_uploads, "total_documents_in_session": len(retained_docs), "total_context_chars": sum(int(row["char_count"]) for row in retained_docs)}


@router.delete("/api/context/{session_id}/{document_id}")
async def delete_context_file(session_id: str, document_id: str) -> dict[str, object]:
	docs = fetch_context_docs(session_id)
	if not docs:
		raise HTTPException(status_code=404, detail="Session context not found")
	doc_ids = {str(row["document_id"]) for row in docs}
	if document_id not in doc_ids:
		raise HTTPException(status_code=404, detail="Document not found in session context")
	delete_context_document(document_id)
	remaining = fetch_context_docs(session_id)
	return {"session_id": session_id, "document_id": document_id, "deleted": True, "total_documents_in_session": len(remaining), "total_context_chars": sum(int(row["char_count"]) for row in remaining)}


@router.delete("/api/context/{session_id}")
async def clear_context_files(session_id: str) -> dict[str, object]:
	docs = fetch_context_docs(session_id)
	if not docs:
		raise HTTPException(status_code=404, detail="Session context not found")
	for row in docs:
		delete_context_document(str(row["document_id"]))
	return {"session_id": session_id, "cleared": True, "total_documents_in_session": 0, "total_context_chars": 0}
