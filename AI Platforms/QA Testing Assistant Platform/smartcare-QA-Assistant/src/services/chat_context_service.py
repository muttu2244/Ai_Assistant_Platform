from __future__ import annotations

import io
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

from src.chat_ui.config import (
	ALLOWED_UPLOAD_EXTENSIONS,
	MAX_CONTEXT_CHARS,
	MAX_CONTEXT_CHARS_PER_FILE,
	MAX_FILE_SIZE_BYTES,
	MAX_FILES_PER_SESSION,
	UPLOAD_CONTEXT_DB_PATH,
	UPLOAD_CONTEXT_DIR,
)


logger = logging.getLogger(__name__)


def context_db_connection() -> sqlite3.Connection:
	conn = sqlite3.connect(str(UPLOAD_CONTEXT_DB_PATH))
	conn.row_factory = sqlite3.Row
	return conn


def init_context_store() -> None:
	UPLOAD_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
	with context_db_connection() as conn:
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS uploaded_context_documents (
				document_id TEXT PRIMARY KEY,
				session_id TEXT NOT NULL,
				filename TEXT NOT NULL,
				storage_path TEXT NOT NULL,
				size_bytes INTEGER NOT NULL,
				char_count INTEGER NOT NULL,
				uploaded_at TEXT NOT NULL
			)
			"""
		)
		conn.execute(
			"""
			CREATE INDEX IF NOT EXISTS idx_uploaded_context_session
			ON uploaded_context_documents (session_id, uploaded_at)
			"""
		)


def fetch_context_docs(session_id: str) -> list[sqlite3.Row]:
	with context_db_connection() as conn:
		rows = conn.execute(
			"""
			SELECT document_id, session_id, filename, storage_path, size_bytes, char_count, uploaded_at
			FROM uploaded_context_documents
			WHERE session_id = ?
			ORDER BY uploaded_at ASC
			""",
			(session_id,),
		).fetchall()
	return rows


def delete_context_document(document_id: str) -> None:
	with context_db_connection() as conn:
		row = conn.execute(
			"SELECT storage_path FROM uploaded_context_documents WHERE document_id = ?",
			(document_id,),
		).fetchone()
		if row is None:
			return
		storage_path = Path(str(row["storage_path"]))
		conn.execute("DELETE FROM uploaded_context_documents WHERE document_id = ?", (document_id,))
	try:
		storage_path.unlink(missing_ok=True)
	except Exception:
		logger.warning("Failed to delete context file for document_id=%s", document_id)


def is_allowed_upload(filename: str | None) -> bool:
	if not filename:
		return False
	return Path(filename).suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS


def sanitize_uploaded_text(text: str) -> str:
	if not text:
		return ""
	text = text.replace("\x00", " ")
	text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", " ", text)
	text = re.sub(r"\s+", " ", text).strip()
	return text


def trim_context(text: str, max_chars: int = 12000) -> str:
	text = (text or "").strip()
	if len(text) <= max_chars:
		return text
	return text[:max_chars].rstrip() + " ...[truncated]"


def extract_docx_text(raw: bytes, filename: str) -> str:
	try:
		with zipfile.ZipFile(io.BytesIO(raw)) as archive:
			xml_bytes = archive.read("word/document.xml")
	except Exception as exc:
		raise HTTPException(status_code=400, detail=f"{filename} is not a readable DOCX file") from exc

	try:
		root = ET.fromstring(xml_bytes)
	except Exception as exc:
		raise HTTPException(status_code=400, detail=f"{filename} contains invalid DOCX XML") from exc

	texts: list[str] = []
	for node in root.iter():
		if node.tag.endswith("}t") and node.text:
			texts.append(node.text)
	return "\n".join(texts)


def extract_pdf_text(raw: bytes, filename: str) -> str:
	try:
		from pypdf import PdfReader
	except Exception as exc:
		raise HTTPException(status_code=500, detail="PDF upload requires pypdf package. Install it and retry.") from exc

	try:
		reader = PdfReader(io.BytesIO(raw))
		page_texts = []
		for page in reader.pages:
			page_texts.append(page.extract_text() or "")
		return "\n".join(page_texts)
	except Exception as exc:
		raise HTTPException(status_code=400, detail=f"{filename} is not a readable PDF file") from exc


async def read_upload_text(upload_file: UploadFile) -> tuple[str, int]:
	filename = upload_file.filename or "uploaded_file"
	suffix = Path(filename).suffix.lower()
	if not is_allowed_upload(filename):
		raise HTTPException(status_code=400, detail=f"Unsupported file type for {filename}")

	raw = await upload_file.read()
	size_bytes = len(raw)
	if size_bytes == 0:
		raise HTTPException(status_code=400, detail=f"{filename} is empty")
	if size_bytes > MAX_FILE_SIZE_BYTES:
		raise HTTPException(status_code=400, detail=f"{filename} exceeds max size of {MAX_FILE_SIZE_BYTES} bytes")

	if suffix == ".docx":
		text = extract_docx_text(raw, filename)
	elif suffix == ".pdf":
		text = extract_pdf_text(raw, filename)
	else:
		text = raw.decode("utf-8", errors="ignore")

	text = sanitize_uploaded_text(text)
	if not text:
		raise HTTPException(status_code=400, detail=f"{filename} has no readable text content")

	return text, size_bytes


def build_uploaded_context(session_id: str | None, max_docs: int = 5, max_chars: int | None = None) -> tuple[str, list[str]]:
	if not session_id:
		return "", []

	if max_chars is None:
		max_chars = min(MAX_CONTEXT_CHARS, 60_000)

	docs = fetch_context_docs(session_id)
	if not docs:
		return "", []

	selected = docs[-max_docs:]
	parts: list[str] = []
	source_names: list[str] = []
	manifest_names = [str(doc["filename"] or "uploaded_file") for doc in selected]
	manifest = "Attached Sources: " + ", ".join(manifest_names)

	separator_overhead = max(0, (len(selected) - 1) * len("\n\n---\n\n"))
	header_overhead = sum(len(f"Source: {name}\nContent:\n") for name in manifest_names)
	reserved = len(manifest) + 8 + separator_overhead + header_overhead
	remaining_budget = max(4000, max_chars - reserved)
	per_doc_budget = max(1200, remaining_budget // max(1, len(selected)))

	for doc in selected:
		filename = str(doc["filename"] or "uploaded_file")
		storage_path = Path(str(doc["storage_path"]))
		if not storage_path.exists():
			continue
		try:
			text = storage_path.read_text(encoding="utf-8")
		except Exception:
			continue
		text = trim_context(text, max_chars=per_doc_budget)
		if not text:
			continue
		source_names.append(filename)
		parts.append(f"Source: {filename}\nContent:\n{text}")

	if not parts:
		return "", []

	combined = manifest + "\n\n" + "\n\n---\n\n".join(parts)
	combined = trim_context(combined, max_chars=max_chars)
	return combined, source_names


def max_files_per_session() -> int:
	return MAX_FILES_PER_SESSION


def max_context_chars() -> int:
	return MAX_CONTEXT_CHARS


def max_context_chars_per_file() -> int:
	return MAX_CONTEXT_CHARS_PER_FILE
