"""SmartCare QA Assistant web app + API.

Provides a local, demo-friendly UI with meaningful QA workflows:
- Test case / work-item lookup
- Coverage gaps
- Sprint summary
- AI pipeline actions
- Chat assistant with button + chat-based test case generation
"""

from __future__ import annotations

import asyncio
import csv
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import io
import logging
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from tempfile import NamedTemporaryFile
from typing import Literal
import xml.etree.ElementTree as ET
import zipfile

from fastapi import FastAPI, Query, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from uuid import uuid4
from src.api.routes.chat import router as chat_router
from src.api.routes.context import router as context_router
from src.api.routes.predictive import router as predictive_router
from src.api.routes.qa import router as qa_router
from src.api.routes.system import router as system_router
from src.agents.ado_fetcher.ado_client import AdoClient
from src.agents.phi_sanitizer.presidio_analyzer import PhiSanitizer
from src.agents.ai_engine.foundry_client import FoundryClient
from src.common.config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[2]
LANDING_LOGO_PATH = ROOT_DIR / "Streamline_Logo_Gradient.jpg"
UPLOAD_CONTEXT_DIR = ROOT_DIR / ".chat_context_uploads"
UPLOAD_CONTEXT_DB_PATH = ROOT_DIR / ".chat_context_store.sqlite3"

ALLOWED_UPLOAD_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".docx", ".pdf"}
MAX_FILE_SIZE_BYTES = 5_000_000  # 5 MB per file
MAX_FILES_PER_SESSION = 5
MAX_CONTEXT_CHARS = 150_000
MAX_CONTEXT_CHARS_PER_FILE = max(20_000, MAX_CONTEXT_CHARS // max(1, MAX_FILES_PER_SESSION))

DEFAULT_TICKET_EXPORT_CSV = ROOT_DIR / "poc_ado_query_results.csv"
DEFAULT_FEATURE_MODULE_CSV = ROOT_DIR / "msp_feature_module_mapping.csv"
DEFAULT_MODULE_SUMMARY_CSV = ROOT_DIR / "bug_ticket_feature_module_recurrence_summary.csv"
DEFAULT_FUNCTIONALITY_SUMMARY_CSV = ROOT_DIR / "bug_ticket_functionality_summary.csv"
DEFAULT_RECURRENCE_OUTPUT_CSV = ROOT_DIR / "probable_recurrence_candidates.csv"
DEFAULT_RECENT_WINDOW_DAYS = 60
DEFAULT_MSP_SHEET_NAME = "6.0_1-AprilMSP_2026"
DEFAULT_MSP_TARGET_CATEGORY = "Engineering Improvement Initiatives- NBL(I)"

# In-memory progress tracker for long-running predictive E2E runs.
PREDICTIVE_RUN_STATUS: dict[str, dict[str, object]] = {}


def _set_predictive_run_status(run_id: str, state: str, step: str, message: str) -> None:
	PREDICTIVE_RUN_STATUS[run_id] = {
		"run_id": run_id,
		"state": state,
		"step": step,
		"message": message,
		"updated_at": datetime.now(timezone.utc).isoformat(),
	}


def _context_db_connection() -> sqlite3.Connection:
	conn = sqlite3.connect(str(UPLOAD_CONTEXT_DB_PATH))
	conn.row_factory = sqlite3.Row
	return conn


def _init_context_store() -> None:
	UPLOAD_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
	with _context_db_connection() as conn:
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


def _fetch_context_docs(session_id: str) -> list[sqlite3.Row]:
	with _context_db_connection() as conn:
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


def _delete_context_document(document_id: str) -> None:
	with _context_db_connection() as conn:
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

@asynccontextmanager
async def lifespan(app: FastAPI):
	_init_context_store()
	yield
	# Clean shutdown: cancel any lingering tasks so Ctrl+C is immediate
	import asyncio
	tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
	for task in tasks:
		task.cancel()


app = FastAPI(
	title=settings.app_name,
	version="0.2.0",
	description="SmartCare QA Assistant with local interactive UI and API endpoints.",
	lifespan=lifespan,
)

app.include_router(system_router)
app.include_router(qa_router)
app.include_router(predictive_router)
app.include_router(context_router)
app.include_router(chat_router)


class GenerateTestCasesRequest(BaseModel):
	feature_text: str = Field(min_length=3)
	work_item_id: int | None = None
	mode: Literal["mock", "live"] = "mock"


class ChatRequest(BaseModel):
	message: str = Field(min_length=1)
	work_item_id: int | None = None
	session_id: str | None = None
	include_uploaded_context: bool = True


def _ado_is_configured() -> bool:
	return settings.has_values(settings.ado_org_url, settings.ado_project, settings.ado_pat)


def _strip_html(text: str | None) -> str:
	"""Remove HTML tags and decode basic entities for clean display."""
	import re
	if not text:
		return ""
	clean = re.sub(r"<[^>]+>", " ", text)
	clean = clean.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")
	clean = re.sub(r"\s+", " ", clean).strip()
	return clean

def _is_allowed_upload(filename: str | None) -> bool:
    if not filename:
        return False
    return Path(filename).suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS

def _sanitize_uploaded_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _trim_context(text: str, max_chars: int = 12000) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " ...[truncated]"


def _extract_docx_text(raw: bytes, filename: str) -> str:
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


def _extract_pdf_text(raw: bytes, filename: str) -> str:
	try:
		from pypdf import PdfReader
	except Exception as exc:
		raise HTTPException(
			status_code=500,
			detail="PDF upload requires pypdf package. Install it and retry.",
		) from exc

	try:
		reader = PdfReader(io.BytesIO(raw))
		page_texts = []
		for page in reader.pages:
			page_texts.append(page.extract_text() or "")
		return "\n".join(page_texts)
	except Exception as exc:
		raise HTTPException(status_code=400, detail=f"{filename} is not a readable PDF file") from exc

async def _read_upload_text(upload_file: UploadFile) -> tuple[str, int]:
	filename = upload_file.filename or "uploaded_file"
	suffix = Path(filename).suffix.lower()
	if not _is_allowed_upload(filename):
		raise HTTPException(status_code=400, detail=f"Unsupported file type for {filename}")

	raw = await upload_file.read()
	size_bytes = len(raw)
	if size_bytes == 0:
		raise HTTPException(status_code=400, detail=f"{filename} is empty")
	if size_bytes > MAX_FILE_SIZE_BYTES:
		raise HTTPException(
			status_code=400,
			detail=f"{filename} exceeds max size of {MAX_FILE_SIZE_BYTES} bytes",
		)

	if suffix == ".docx":
		text = _extract_docx_text(raw, filename)
	elif suffix == ".pdf":
		text = _extract_pdf_text(raw, filename)
	else:
		text = raw.decode("utf-8", errors="ignore")

	text = _sanitize_uploaded_text(text)
	if not text:
		raise HTTPException(status_code=400, detail=f"{filename} has no readable text content")

	return text, size_bytes

def _build_uploaded_context(session_id: str | None, max_docs: int = 5, max_chars: int | None = None) -> tuple[str, list[str]]:
	if not session_id:
		return "", []

	if max_chars is None:
		max_chars = min(MAX_CONTEXT_CHARS, 60_000)

	docs = _fetch_context_docs(session_id)
	if not docs:
		return "", []

	selected = docs[-max_docs:]
	parts: list[str] = []
	source_names: list[str] = []
	manifest_names = [str(doc["filename"] or "uploaded_file") for doc in selected]
	manifest = "Attached Sources: " + ", ".join(manifest_names)

	# Reserve room for source manifest and separators, then split remaining budget across files.
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
		text = _trim_context(text, max_chars=per_doc_budget)
		if not text:
			continue
		source_names.append(filename)
		parts.append(f"Source: {filename}\nContent:\n{text}")

	if not parts:
		return "", []

	combined = manifest + "\n\n" + "\n\n---\n\n".join(parts)
	combined = _trim_context(combined, max_chars=max_chars)
	return combined, source_names

_NUMBER_WORDS = {
	"one": 1,
	"two": 2,
	"three": 3,
	"four": 4,
	"five": 5,
	"six": 6,
	"seven": 7,
	"eight": 8,
	"nine": 9,
	"ten": 10,
}


def _is_testcase_generation_intent(message: str) -> bool:
	lower = message.lower()
	has_target = any(token in lower for token in ("test case", "testcase", "test cases", "qa pack"))
	has_action = any(token in lower for token in ("generate", "create", "build", "write", "prepare", "draft"))
	return has_target and has_action


def _extract_requested_count(message: str, default: int = 3) -> int:
	match = re.search(r"\b(\d{1,2})\s+(?:test\s*cases?|cases?)\b", message, flags=re.IGNORECASE)
	if match:
		return max(1, min(int(match.group(1)), 20))
	word_match = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:test\s*cases?|cases?)\b", message, flags=re.IGNORECASE)
	if word_match:
		return _NUMBER_WORDS[word_match.group(1).lower()]
	return default


def _extract_work_item_id(message: str) -> int | None:
	patterns = [
		r"\bwork\s*item(?:\s*id)?\s*[:#-]?\s*(\d{3,9})\b",
		r"\bworkitem(?:\s*id)?\s*[:#-]?\s*(\d{3,9})\b",
		r"\bstory\s*[:#-]?\s*(\d{3,9})\b",
		r"\bstory\s*id\s*[:#-]?\s*(\d{3,9})\b",
		r"\b(?:ticket|bug|item|ado)\s*[:#-]?\s*(\d{3,9})\b",
		r"\bwi\s*[:#-]?\s*(\d{3,9})\b",
		r"\bid\s*[:#-]?\s*(\d{3,9})\b",
	]
	for pattern in patterns:
		match = re.search(pattern, message, flags=re.IGNORECASE)
		if match:
			return int(match.group(1))

	# Fallback: if a generation prompt contains exactly one standalone 5-9 digit number,
	# treat it as the likely ADO work item ID.
	standalone_ids = re.findall(r"\b(\d{5,9})\b", message)
	if len(standalone_ids) == 1:
		return int(standalone_ids[0])
	return None


def _extract_feature_query(message: str) -> str | None:
	clean = message
	clean = re.sub(r"\b(generate|create|build|write|prepare|draft)\b", " ", clean, flags=re.IGNORECASE)
	clean = re.sub(r"\b\d{1,2}\s+(?:test\s*cases?|cases?)\b", " ", clean, flags=re.IGNORECASE)
	clean = re.sub(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:test\s*cases?|cases?)\b", " ", clean, flags=re.IGNORECASE)
	clean = re.sub(r"\b(test\s*cases?|testcase|qa\s*pack)\b", " ", clean, flags=re.IGNORECASE)
	clean = re.sub(r"\b(work\s*item|story|wi|id)\s*[:#-]?\s*\d{3,9}\b", " ", clean, flags=re.IGNORECASE)
	clean = re.sub(r"\b(workitem|ticket|bug|item|ado)\s*[:#-]?\s*\d{3,9}\b", " ", clean, flags=re.IGNORECASE)
	clean = re.sub(r"\b\d{5,9}\b", " ", clean, flags=re.IGNORECASE)
	clean = re.sub(r"\b(for|of|on|about)\b", " ", clean, flags=re.IGNORECASE)
	clean = re.sub(r"\s+", " ", clean).strip(" .:-")
	return clean or None


def _candidate_score(item: object, feature_query: str) -> int:
	terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_\-]+", feature_query or "") if len(t) >= 3]
	if not terms:
		return 0
	title = getattr(item, "title", "") or ""
	description = getattr(item, "description", "") or ""
	area_path = getattr(item, "area_path", "") or ""
	acceptance = getattr(item, "acceptance_criteria", "") or ""
	title_l = title.lower()
	desc_l = description.lower()
	area_l = area_path.lower()
	acceptance_l = acceptance.lower()

	score = 0
	for term in terms:
		if term in title_l:
			score += 4
		if term in desc_l:
			score += 2
		if term in acceptance_l:
			score += 2
		if term in area_l:
			score += 1
	return score


def _count_generated_cases(text: str) -> int:
	if not text:
		return 0
	tc_matches = re.findall(r"(?im)^\s*(?:test\s*case\s*\d+|tc[-_ ]?\d+)\b", text)
	if tc_matches:
		return len(tc_matches)
	section_matches = re.findall(r"(?im)^\s*\d+\.\s+", text)
	return len(section_matches)


def _is_linked_testcase_table_request(message: str) -> bool:
	lower = (message or "").lower()
	table_requested = "table" in lower or "tabular" in lower
	tc_requested = (
		"linked test" in lower
		or "testcase" in lower
		or "test case" in lower
	)
	extract_requested = any(token in lower for token in ("extract", "list", "show", "print", "details"))
	return table_requested and tc_requested and extract_requested


def _table_cell(value: str) -> str:
	clean = (value or "").strip()
	if not clean:
		return "Not provided"
	return clean.replace("|", "\\|").replace("\n", "<br>")


def _build_linked_testcase_markdown_table(rows: list[dict[str, str]]) -> str:
	if not rows:
		return "No linked test cases were found for this work item."

	headers = [
		"Test Case ID",
		"Description",
		"Pre-Conditions",
		"Test Steps",
		"Expected Result",
	]
	lines = [
		"| " + " | ".join(headers) + " |",
		"| --- | --- | --- | --- | --- |",
	]
	for row in rows:
		lines.append(
			"| "
			+ " | ".join(
				[
					_table_cell(row.get("test_case_id", "")),
					_table_cell(row.get("description", "")),
					_table_cell(row.get("pre_conditions", "")),
					_table_cell(row.get("test_steps", "")),
					_table_cell(row.get("expected_result", "")),
				]
			)
			+ " |"
		)
	return "\n".join(lines)


def _prepend_response_meta_header(message_text: str, meta: dict[str, object]) -> str:
	"""Add a compact provenance header to assistant text for grounded responses."""
	if not meta:
		return message_text
	header_parts: list[str] = []
	source = meta.get("source")
	if source:
		header_parts.append(f"Source: {source}")
	work_item_id = meta.get("work_item_id")
	if work_item_id:
		header_parts.append(f"Work Item: {work_item_id}")
	selection_mode = meta.get("selection_mode")
	if selection_mode:
		header_parts.append(f"Selection: {selection_mode}")
	presidio_check = meta.get("presidio_check")
	if presidio_check:
		header_parts.append(f"Presidio: {presidio_check}")
	sanitization_mode = meta.get("sanitization_mode")
	if sanitization_mode:
		header_parts.append(f"Mode: {sanitization_mode}")
	if not header_parts:
		return message_text
	header = " | ".join(header_parts)
	return f"[{header}]\n\n{message_text}"


_ADO_GROUNDED_CHAT_TOKENS = (
	"ado",
	"azure devops",
	"board",
	"work item",
	"ticket",
	"bug",
	"defect",
	"story",
	"coverage",
	"coverage gap",
	"requirement",
	"acceptance criteria",
	"test case",
	"test cases",
	"qa",
	"automation",
)


def _is_ado_grounded_chat_intent(message: str) -> bool:
	lower = message.lower()
	if _extract_work_item_id(message) is not None:
		return True
	return any(token in lower for token in _ADO_GROUNDED_CHAT_TOKENS)


async def _generate_chat_from_ado_context(
	message: str,
	work_item_id: int | None,
	feature_query: str | None,
) -> dict[str, object]:
	if not _ado_is_configured():
		return {
			"assistant_message": "ADO is not configured. Provide ADO_ORG_URL, ADO_PROJECT, and ADO_PAT to enable grounded QA chat.",
			"source": "ado_unavailable",
			"grounded": False,
			"presidio_protected": False,
			"sanitization_mode": "unknown",
			"response_meta": {
				"source": "ado_unavailable",
				"grounded": False,
				"presidio_check": "disabled",
				"sanitization_mode": "unknown",
			},
		}

	ado = AdoClient()
	phi = PhiSanitizer()
	phi.reset_tracking()
	selected_id = work_item_id
	selection_mode = "exact_id" if work_item_id else "search_match"

	if not selected_id:
		if not feature_query:
			return {
				"assistant_message": "Please provide a work item ID or a feature/module name so I can retrieve the correct ADO context before answering.",
				"source": "ado_live_search",
				"grounded": False,
				"presidio_protected": False,
				"sanitization_mode": phi.sanitization_mode,
				"needs_clarification": True,
				"response_meta": {
					"source": "ado_live_search",
					"grounded": False,
					"presidio_check": "not_run",
					"sanitization_mode": phi.sanitization_mode,
				},
			}
		candidates = await ado.search_work_items_by_text(feature_query, top_n=5)
		if not candidates:
			return {
				"assistant_message": f"I could not find any ADO items matching '{feature_query}'. Please provide a work item ID.",
				"source": "ado_live_search",
				"grounded": False,
				"presidio_protected": False,
				"sanitization_mode": phi.sanitization_mode,
				"needs_clarification": True,
				"response_meta": {
					"source": "ado_live_search",
					"grounded": False,
					"presidio_check": "not_run",
					"sanitization_mode": phi.sanitization_mode,
				},
			}
		ranked = sorted(candidates, key=lambda item: _candidate_score(item, feature_query), reverse=True)
		selected_id = ranked[0].id
		if len(ranked) > 1 and _candidate_score(ranked[0], feature_query) < (_candidate_score(ranked[1], feature_query) + 3):
			selection_mode = "search_match_ambiguous"

	full_context = await ado.get_work_item_full_context(selected_id)
	base_item = full_context["work_item"]
	safe_item = await phi.sanitize_work_item(base_item)

	safe_related_titles: list[str] = []
	for rel in full_context.get("related_items", [])[:8]:
		safe_rel = await phi.sanitize_work_item(rel)
		safe_related_titles.append(
			f"- {safe_rel.id}: {_strip_html(safe_rel.title)} ({safe_rel.work_item_type}, {safe_rel.state})"
		)

	safe_linked_cases: list[str] = []
	table_rows: list[dict[str, str]] = []
	for tc in full_context.get("linked_test_cases", [])[:8]:
		safe_tc = await phi.sanitize_test_case(tc)
		step_actions: list[str] = []
		expected_outcomes: list[str] = []
		for index, step in enumerate(safe_tc.steps[:8], start=1):
			action = _strip_html(step.get("action", ""))
			expected = _strip_html(step.get("expected", ""))
			if action:
				step_actions.append(f"{index}. {action}")
			if expected:
				expected_outcomes.append(f"{index}. {expected}")

		pre_conditions = "Not explicitly available in linked ADO test case metadata"
		table_rows.append(
			{
				"test_case_id": f"TC-{safe_tc.id}",
				"description": _strip_html(safe_tc.title),
				"pre_conditions": pre_conditions,
				"test_steps": "\n".join(step_actions) if step_actions else "Not provided",
				"expected_result": "\n".join(expected_outcomes) if expected_outcomes else "Not provided",
			}
		)
		safe_linked_cases.append(
			f"- TC {safe_tc.id}: {_strip_html(safe_tc.title)} [{safe_tc.state}] automated={safe_tc.automated} steps={len(safe_tc.steps)}"
		)

	context_sections = [
		f"Work Item ID: {safe_item.id}",
		f"Title: {_strip_html(safe_item.title)}",
		f"Type: {safe_item.work_item_type}",
		f"State: {safe_item.state}",
		f"Area Path: {safe_item.area_path}",
		f"Iteration Path: {safe_item.iteration_path}",
		"Description:\n" + (_strip_html(safe_item.description) or "Not provided"),
		"Acceptance Criteria:\n" + (_strip_html(safe_item.acceptance_criteria) or "Not provided"),
	]
	if safe_related_titles:
		context_sections.append("Related Work Items:\n" + "\n".join(safe_related_titles))
	if safe_linked_cases:
		context_sections.append("Existing Linked Test Cases:\n" + "\n".join(safe_linked_cases))
	if table_rows:
		details_lines = []
		for row in table_rows:
			details_lines.append(
				"TC-ID: {id}\nTitle: {title}\nPre-Conditions: {pre}\nTest Steps:\n{steps}\nExpected Result:\n{expected}".format(
					id=row["test_case_id"],
					title=row["description"],
					pre=row["pre_conditions"],
					steps=row["test_steps"],
					expected=row["expected_result"],
				)
			)
		context_sections.append("Linked Test Case Detailed Rows:\n" + "\n\n".join(details_lines))
	ado_context = "\n\n".join(context_sections)

	response_meta = {
		"source": "ado_live",
		"grounded": True,
		"work_item_id": safe_item.id,
		"selection_mode": selection_mode,
		"presidio_check": "enabled",
		"sanitization_mode": phi.sanitization_mode,
	}

	if _is_linked_testcase_table_request(message):
		table_text = _build_linked_testcase_markdown_table(table_rows)
		safe_table_text = await phi.sanitize_output(table_text)
		response_with_meta = _prepend_response_meta_header(safe_table_text, response_meta)
		return {
			"assistant_message": response_with_meta,
			"source": "ado_live",
			"grounded": True,
			"used_work_item_id": safe_item.id,
			"used_work_item_title": _strip_html(safe_item.title),
			"selection_mode": selection_mode,
			"presidio_protected": True,
			"sanitization_mode": phi.sanitization_mode,
			"response_meta": response_meta,
		}

	lower = message.lower()
	if "automation" in lower or "script" in lower or "selenium" in lower:
		feature = "automation_script"
	elif "user guide" in lower or "guide" in lower or "documentation" in lower:
		feature = "user_guide"
	else:
		feature = "default"

	grounded_prompt = (
		"Answer the user's QA or development-support request using the grounded ADO context. "
		"CRITICAL: The context provided is ALREADY SANITIZED with synthetic/fake test data. "
		"All real PHI has been replaced with deterministic fake values. These are completely safe to display. "
		"When presenting masked/sanitized fields in tables or text, use ONLY these formats:\n"
		"  - Patient/Person names: Format as [NRP_XXXXXXXX] where X is a hex character (e.g., [NRP_C4L33F8A])\n"
		"  - Social Security Numbers: Format as XXX-XX-0000 (never show actual numbers, always this pattern)\n"
		"  - Phone Numbers: Format as 000-555-0000 (never show actual numbers, always this pattern)\n"
		"  - Email Addresses: Show the actual email from context as-is (e.g., rchen@nexushealth.com)\n"
		"  - Dates: Format as MM/DD/YYYY pattern\n"
		"  - Locations: Show in brackets like [Location] or use generic pattern\n"
		"NEVER: Use '[REDACTED]' labels, show partial masks like 'C****, asterisks, or X placeholders mixed with numbers.\n"
		"Use only the formats above. Be consistent. If the context is insufficient, say what is missing. "
		"If the user asks for table/tabular output, return a markdown table with explicit headers and one row per item. "
		f"User request: {message}"
	)

	client = FoundryClient()
	response = await client.generate(
		user_prompt=grounded_prompt,
		feature=feature,
		conversation_history=None,
		context=ado_context,
	)
	safe_response = await phi.sanitize_output(response)
	response_with_meta = _prepend_response_meta_header(safe_response, response_meta)
	return {
		"assistant_message": response_with_meta,
		"source": "ado_live",
		"grounded": True,
		"used_work_item_id": safe_item.id,
		"used_work_item_title": _strip_html(safe_item.title),
		"selection_mode": selection_mode,
		"presidio_protected": True,
		"sanitization_mode": phi.sanitization_mode,
		"response_meta": response_meta,
	}


async def _generate_test_cases_from_ado_context(
	message: str,
	work_item_id: int | None,
	feature_query: str | None,
	requested_count: int,
) -> dict[str, object]:
	if not settings.retrieval_first_testcase:
		client = FoundryClient()
		response = await client.generate(user_prompt=message, feature="test_case")
		return {"assistant_message": response, "source": "llm_direct"}

	if not _ado_is_configured():
		return {
			"assistant_message": "ADO is not configured. Provide ADO_ORG_URL, ADO_PROJECT, and ADO_PAT to generate ADO-grounded test cases.",
			"source": "ado_unavailable",
		}

	ado = AdoClient()
	phi = PhiSanitizer()
	selected_id = work_item_id
	selection_mode = "exact_id" if work_item_id else "search_match"

	if not selected_id:
		if not feature_query:
			return {
				"assistant_message": "Please provide a feature name or a work item ID so I can fetch the correct ADO story before generating test cases.",
				"source": "ado_live_search",
				"needs_clarification": True,
			}
		candidates = await ado.search_work_items_by_text(feature_query, top_n=5)
		if not candidates:
			return {
				"assistant_message": f"I could not find any ADO items matching '{feature_query}'. Please provide a work item ID.",
				"source": "ado_live_search",
				"needs_clarification": True,
			}
		ranked = sorted(candidates, key=lambda item: _candidate_score(item, feature_query), reverse=True)
		if len(ranked) > 1 and _candidate_score(ranked[0], feature_query) >= (_candidate_score(ranked[1], feature_query) + 3):
			selected_id = ranked[0].id
		elif len(ranked) > 1:
			candidate_rows = []
			for item in ranked[:5]:
				safe_title = _strip_html(item.title)
				candidate_rows.append({
					"id": item.id,
					"title": safe_title,
					"state": item.state,
					"area_path": item.area_path,
				})
			return {
				"assistant_message": "I found multiple matching ADO items. Please choose one work item ID and ask again.",
				"source": "ado_live_search",
				"needs_clarification": True,
				"candidate_work_items": candidate_rows,
			}
		else:
			selected_id = ranked[0].id

	full_context = await ado.get_work_item_full_context(selected_id)
	base_item = full_context["work_item"]
	safe_item = await phi.sanitize_work_item(base_item)
	safe_related_titles: list[str] = []
	for rel in full_context.get("related_items", [])[:8]:
		safe_rel = await phi.sanitize_work_item(rel)
		safe_related_titles.append(f"- {safe_rel.id}: {_strip_html(safe_rel.title)} ({safe_rel.work_item_type}, {safe_rel.state})")

	safe_linked_cases: list[str] = []
	for tc in full_context.get("linked_test_cases", [])[:8]:
		safe_tc = await phi.sanitize_test_case(tc)
		safe_linked_cases.append(f"- TC {safe_tc.id}: {_strip_html(safe_tc.title)} [{safe_tc.state}]")

	context_sections = [
		f"Work Item ID: {safe_item.id}",
		f"Title: {_strip_html(safe_item.title)}",
		f"Type: {safe_item.work_item_type}",
		f"State: {safe_item.state}",
		f"Area Path: {safe_item.area_path}",
		f"Iteration Path: {safe_item.iteration_path}",
		"Description:\n" + (_strip_html(safe_item.description) or "Not provided"),
		"Acceptance Criteria:\n" + (_strip_html(safe_item.acceptance_criteria) or "Not provided"),
	]
	if safe_related_titles:
		context_sections.append("Related Work Items:\n" + "\n".join(safe_related_titles))
	if safe_linked_cases:
		context_sections.append("Existing Linked Test Cases:\n" + "\n".join(safe_linked_cases))
	ado_context = "\n\n".join(context_sections)

	generation_prompt = (
		f"Generate exactly {requested_count} test cases grounded in the ADO context. "
		"Do not generate generic cases unrelated to the feature. "
		"For each case, use this format:\n"
		"Test Case <number>\n"
		"Title: ...\n"
		"Preconditions: ...\n"
		"Priority: ...\n"
		"Tags: ...\n"
		"Steps:\n"
		"1) Action: ... | Expected: ...\n"
		"2) Action: ... | Expected: ...\n"
		"Traceability: Cite which acceptance criterion or description behavior it validates.\n\n"
		f"User request: {message}"
	)

	client = FoundryClient()
	response = await client.generate(
		user_prompt=generation_prompt,
		feature="test_case",
		conversation_history=None,
		context=ado_context,
	)

	produced_count = _count_generated_cases(response)
	if produced_count and produced_count != requested_count:
		correction_prompt = (
			f"Regenerate and return exactly {requested_count} test cases. "
			f"Your previous response contained {produced_count}. Keep the same format and keep outputs grounded to context."
		)
		response = await client.generate(
			user_prompt=correction_prompt,
			feature="test_case",
			conversation_history=[
				{"role": "user", "content": generation_prompt},
				{"role": "assistant", "content": response},
			],
			context=ado_context,
		)

	safe_response = await phi.sanitize_output(response)
	response_meta = {
		"source": "ado_live",
		"grounded": True,
		"work_item_id": safe_item.id,
		"selection_mode": selection_mode,
		"presidio_check": "enabled",
		"sanitization_mode": phi.sanitization_mode,
	}
	response_with_meta = _prepend_response_meta_header(safe_response, response_meta)
	return {
		"assistant_message": response_with_meta,
		"source": "ado_live",
		"used_work_item_id": safe_item.id,
		"used_work_item_title": _strip_html(safe_item.title),
		"selection_mode": selection_mode,
		"sanitized": True,
		"presidio_protected": True,
		"sanitization_mode": phi.sanitization_mode,
		"response_meta": response_meta,
	}


def _build_mock_test_pack(feature_text: str, work_item_id: int | None = None) -> dict[str, object]:
	label = feature_text.strip() or "Feature"
	prefix = f"WI-{work_item_id}" if work_item_id else "ADHOC"
	test_cases = [
		{
			"id": f"{prefix}-TC-001",
			"title": f"{label}: happy path validation",
			"priority": 1,
			"tags": ["smoke", "regression"],
			"steps": [
				{"action": "Open the module", "expected": "Module is accessible"},
				{"action": "Submit valid data", "expected": "Submission succeeds"},
			],
		},
		{
			"id": f"{prefix}-TC-002",
			"title": f"{label}: invalid input handling",
			"priority": 2,
			"tags": ["negative", "regression"],
			"steps": [
				{"action": "Submit invalid data", "expected": "Validation message is shown"},
				{"action": "Retry submit", "expected": "No bad data is persisted"},
			],
		},
		{
			"id": f"{prefix}-TC-003",
			"title": f"{label}: edge and boundary behavior",
			"priority": 3,
			"tags": ["edge", "boundary"],
			"steps": [
				{"action": "Use boundary values", "expected": "System remains stable"},
				{"action": "Inspect response", "expected": "No unhandled errors"},
			],
		},
	]
	return {
		"summary": f"Generated {len(test_cases)} test cases for '{label}' in mock mode.",
		"test_cases": test_cases,
		"metrics": {
			"total_test_cases": len(test_cases),
			"critical_tests": 1,
			"high_tests": 1,
			"medium_tests": 1,
			"estimated_minutes_saved": 75,
		},
		"compliance": {"hipaa_safe": True, "phi_sanitized": True},
	}


def _mock_coverage_gaps(module: str | None) -> dict[str, object]:
	module_label = module or "SmartCare"
	all_stories = [
		{"id": 881717, "title": "Patient intake validation", "state": "Active"},
		{"id": 881716, "title": "Billing consent form signature", "state": "Active"},
		{"id": 881715, "title": "Billing eligibility check API retry", "state": "New"},
		{"id": 881714, "title": "Claims draft autosave", "state": "New"},
		{"id": 881713, "title": "Role-based access denial", "state": "Active"},
		{"id": 881712, "title": "Patient registration form validation", "state": "New"},
		{"id": 881711, "title": "Claims submission error handling", "state": "Active"},
		{"id": 881710, "title": "Member login MFA verification", "state": "Active"},
	]
	if module and module.strip():
		stories = [s for s in all_stories if module.lower() in s["title"].lower()]
		if not stories:
			stories = all_stories[:3]
	else:
		stories = all_stories
	return {
		"module": module_label,
		"total_stories": len(stories),
		"covered": 0,
		"no_test_cases": len(stories),
		"coverage_percent": 0,
		"stories_without_tests": stories,
		"source": "mock",
		"presidio_protected": True,
		"sanitization_mode": "mock",
	}


def _mock_coverage_for_item(work_item_id: int) -> dict[str, object]:
	return {
		"source": "mock",
		"scope": "work_item",
		"presidio_protected": True,
		"sanitization_mode": "mock",
		"work_item": {
			"id": work_item_id,
			"title": "Mock work item for coverage demo",
			"description": "ADO is not configured locally. Showing mock coverage details.",
			"state": "Active",
			"type": "User Story",
			"area_path": "SmartCare\\Demo",
			"iteration_path": "Sprint Demo",
		},
		"total_stories": 1,
		"covered": 0,
		"no_test_cases": 1,
		"coverage_percent": 0,
		"stories_without_tests": [
			{"id": work_item_id, "title": "Mock work item for coverage demo", "state": "Active"}
		],
		"linked_test_cases": [],
	}


def _mock_sprint_summary() -> dict[str, object]:
	items = [
		{"id": 675745, "title": "Member care plan parameterized string fix", "type": "User Story", "state": "Active"},
		{"id": 675746, "title": "Provider mapping regression", "type": "Bug", "state": "Committed"},
		{"id": 675747, "title": "Policy acknowledgment automation", "type": "Task", "state": "New"},
		{"id": 675748, "title": "Coverage import performance validation", "type": "User Story", "state": "Active"},
	]
	return {
		"sprint": "Current Sprint",
		"total_items": len(items),
		"by_state": {
			"New": 1,
			"Active": 2,
			"Committed": 1,
		},
		"items": items,
		"source": "mock",
	}


def _normalize_key(value: str) -> str:
	return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _safe_int(value: object, default: int = 0) -> int:
	try:
		return int(float(str(value)))
	except Exception:
		return default


def _split_ticket_ids(value: str) -> list[str]:
	if not value:
		return []
	return [chunk.strip() for chunk in str(value).split(";") if chunk.strip()]


def _read_csv_rows_from_path(path: Path) -> list[dict[str, str]]:
	if not path.exists():
		return []
	with path.open("r", encoding="utf-8-sig", newline="") as handle:
		reader = csv.DictReader(handle)
		return [{str(k): str(v or "").strip() for k, v in row.items()} for row in reader]


async def _read_csv_rows_from_upload(upload: UploadFile | None) -> list[dict[str, str]]:
	if upload is None:
		return []
	filename = upload.filename or "uploaded.csv"
	if Path(filename).suffix.lower() not in {".csv", ".txt"}:
		raise HTTPException(
			status_code=400,
			detail=f"{filename} must be a CSV file (.csv).",
		)
	raw = await upload.read()
	if not raw:
		return []

	# Decode with BOM-safe UTF-8 first; fallback keeps endpoint resilient.
	try:
		text = raw.decode("utf-8-sig")
	except UnicodeDecodeError:
		text = raw.decode("latin-1", errors="ignore")

	# Normalize line endings to reduce parser failures on mixed newline files.
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	if not text.strip():
		return []

	# Heuristic delimiter detection from header line.
	header_line = text.split("\n", 1)[0]
	delimiter_candidates = [",", ";", "\t", "|"]
	delimiter = max(delimiter_candidates, key=lambda candidate: header_line.count(candidate))

	try:
		reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
		return [{str(k): str(v or "").strip() for k, v in row.items()} for row in reader]
	except csv.Error as exc:
		raise HTTPException(
			status_code=400,
			detail=(
				f"Unable to parse {filename} as CSV. "
				"Please save it as a standard CSV with quoted values when fields contain new lines."
			),
		) from exc


def _get_value(row: dict[str, str], candidates: tuple[str, ...]) -> str:
	normalized = {_normalize_key(key): value for key, value in row.items()}
	for candidate in candidates:
		value = normalized.get(_normalize_key(candidate), "")
		if value:
			return value
	return ""


def _risk_band(count: int) -> str:
	if count >= 50:
		return "High"
	if count >= 15:
		return "Medium"
	if count > 0:
		return "Low"
	return "None"


def _write_dict_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
	with path.open("w", encoding="utf-8-sig", newline="") as handle:
		writer = csv.DictWriter(handle, fieldnames=fieldnames)
		writer.writeheader()
		writer.writerows(rows)


def _parse_changed_age_days(changed_date: str) -> int | None:
	if not changed_date:
		return None
	text = changed_date.strip()
	if not text:
		return None
	text = text.replace("Z", "+00:00")
	try:
		dt = datetime.fromisoformat(text)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
		age_days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
		return max(0, age_days)
	except Exception:
		return None


def _run_python_command(command: list[str], stage: str) -> str:
	try:
		proc = subprocess.run(command, capture_output=True, text=True, cwd=str(ROOT_DIR), check=False)
	except Exception as exc:
		raise HTTPException(status_code=500, detail=f"{stage} failed to start: {exc}") from exc

	if proc.returncode != 0:
		stderr = (proc.stderr or "").strip()
		stdout = (proc.stdout or "").strip()
		combined = "\n".join(part for part in (stderr, stdout) if part)
		if not combined:
			combined = f"exit code {proc.returncode}"
		# Preserve the tail where Python tracebacks usually end to expose root cause.
		message = combined[-2000:]
		raise HTTPException(status_code=500, detail=f"{stage} failed (exit {proc.returncode}): {message}")

	return (proc.stdout or "").strip()


def _derive_modified_functions_from_feature_rows(feature_rows: list[dict[str, str]]) -> list[dict[str, str]]:
	seen: set[str] = set()
	derived: list[dict[str, str]] = []
	for row in feature_rows:
		functionality = _get_value(row, ("functionality", "feature_functionality")).strip()
		if not functionality:
			continue
		key = _normalize_key(functionality)
		if not key or key in seen:
			continue
		seen.add(key)
		derived.append({"modified_functionality": functionality})
	return derived


def _compute_predictive_outputs(
	module_summary_rows: list[dict[str, str]],
	functionality_summary_rows: list[dict[str, str]],
	feature_module_rows: list[dict[str, str]],
	dependency_rows: list[dict[str, str]],
	modified_function_rows: list[dict[str, str]],
	ticket_rows: list[dict[str, str]],
	output_csv_path: Path,
	recent_window_days: int = DEFAULT_RECENT_WINDOW_DAYS,
	top_score_levels: int = 50,
) -> dict[str, object]:
	# Keep feature counts from module summary, but derive module ticket counts from
	# functionality assignments so both UI sections use the same ticket basis.
	module_feature_counts: dict[str, int] = {}
	for row in module_summary_rows:
		module_name = _get_value(row, ("module_name", "module"))
		if not module_name:
			continue
		module_feature_counts[module_name] = _safe_int(_get_value(row, ("feature_count",)))

	functionality_to_ids: dict[str, list[str]] = {}
	functionality_to_module: dict[str, str] = {}
	functionality_to_count: dict[str, int] = {}
	module_to_unique_ticket_ids: dict[str, set[str]] = {}
	for row in functionality_summary_rows:
		func = _get_value(row, ("functionality", "feature_functionality"))
		module_name = _get_value(row, ("module_name", "module"))
		ids = _split_ticket_ids(_get_value(row, ("bug_cust_ticket_ids", "bug_ticket_ids")))
		count = _safe_int(_get_value(row, ("bug_cust_ticket_count", "bug_ticket_count", "count")), default=len(ids))
		if not func:
			continue
		functionality_to_ids[func] = ids
		functionality_to_count[func] = count
		functionality_to_module[func] = module_name
		if module_name:
			module_to_unique_ticket_ids.setdefault(module_name, set()).update(ids)

	module_cards = []
	for module_name in sorted(set(module_feature_counts) | set(module_to_unique_ticket_ids)):
		module_cards.append(
			{
				"module_name": module_name,
				"ticket_count": len(module_to_unique_ticket_ids.get(module_name, set())),
				"feature_count": module_feature_counts.get(module_name, 0),
			}
		)
	module_cards.sort(key=lambda item: item["ticket_count"], reverse=True)

	functionality_to_features: dict[str, set[str]] = {}
	for row in feature_module_rows:
		func = _get_value(row, ("functionality", "feature_functionality"))
		feature_name = _get_value(row, ("title", "feature_name"))
		if not func:
			continue
		functionality_to_features.setdefault(func, set())
		if feature_name:
			functionality_to_features[func].add(feature_name)

	functionality_lookup: dict[str, str] = {
		_normalize_key(func): func
		for func in functionality_to_ids
		if _normalize_key(func)
	}

	def _resolve_functionality_name(name: str) -> str:
		key = _normalize_key(name)
		if not key:
			return ""
		return functionality_lookup.get(key, name.strip())

	dependency_map: dict[str, set[str]] = {}
	for row in dependency_rows:
		source = _get_value(
			row,
			(
				"source_functionality",
				"functionality",
				"function",
				"from_function",
				"from",
				"function_code",
				"source_function",
			),
		)
		target = _get_value(
			row,
			(
				"depends_on_functionality",
				"dependent_functionality",
				"depends_on",
				"to_function",
				"to",
				"depend_code",
				"dependent_code",
				"target_function",
			),
		)
		source = _resolve_functionality_name(source)
		target = _resolve_functionality_name(target)
		if source and target:
			dependency_map.setdefault(source, set()).add(target)

	modified_functions: set[str] = set()
	for row in modified_function_rows:
		val = _get_value(
			row,
			(
				"modified_functionality",
				"modified_function",
				"functionality",
				"function",
			),
		)
		if val:
			modified_functions.add(_resolve_functionality_name(val))

	if not modified_functions:
		# Fallback to top active functionalities from summary when no explicit MSP change list is provided.
		ranked = sorted(
			functionality_to_count.items(),
			key=lambda item: item[1],
			reverse=True,
		)
		modified_functions = {name for name, _ in ranked[:8]}

	ticket_meta: dict[str, dict[str, str]] = {}
	for row in ticket_rows:
		ticket_id = _get_value(row, ("id", "ticket_id", "bug_ticket_id")).strip()
		if not ticket_id:
			continue
		ticket_meta[ticket_id] = {
			"customer_priority": _get_value(row, ("customer_priority", "priority", "customer priority")),
			"changed_date": _get_value(row, ("changed_date", "system.changeddate")),
			"work_item_type": _get_value(row, ("work_item_type", "type", "system.workitemtype")),
			"title": _get_value(row, ("title", "system.title")),
			"state": _get_value(row, ("state", "system.state")),
			"area_path": _get_value(row, ("area_path", "system.areapath")),
		}

	def _priority_points(priority: str) -> tuple[int, str, int]:
		value = (priority or "").strip().lower()
		if value == "on fire":
			return 25, "PRIORITY_ON_FIRE", 3
		if value == "urgent":
			return 20, "PRIORITY_URGENT", 2
		if value == "high":
			return 15, "PRIORITY_HIGH", 1
		return 0, "PRIORITY_OTHER", 0

	def _recency_points(changed_date: str) -> tuple[int, str, int]:
		age_days = _parse_changed_age_days(changed_date)
		if age_days is None:
			return 0, "NO_RECENCY_DATA", 9999
		if age_days <= recent_window_days:
			return 15, f"RECENT_0_{recent_window_days}D", age_days
		if age_days <= recent_window_days * 2:
			return 8, f"RECENT_{recent_window_days + 1}_{recent_window_days * 2}D", age_days
		return 2, f"OLDER_{recent_window_days * 2}D_PLUS", age_days

	ticket_candidates: dict[str, dict[str, object]] = {}
	for modified_func in sorted(modified_functions):
		contexts: list[tuple[str, str, int]] = [(modified_func, "direct_change", 0)]
		for dependent in sorted(dependency_map.get(modified_func, set())):
			contexts.append((dependent, "dependent_change", 1))

		for candidate_func, relationship_type, dependency_distance in contexts:
			for ticket_id in functionality_to_ids.get(candidate_func, []):
				meta = ticket_meta.get(ticket_id, {})
				priority_points, priority_code, priority_rank = _priority_points(str(meta.get("customer_priority", "")))
				recency_points, recency_code, age_days = _recency_points(str(meta.get("changed_date", "")))
				match_points = 50 if dependency_distance == 0 else 35
				distance_penalty = dependency_distance * 7
				raw_score = max(0, min(100, match_points + priority_points + recency_points - distance_penalty))
				reason_codes = ["DIRECT_MATCH" if dependency_distance == 0 else f"DEP_{dependency_distance}_HOP", priority_code, recency_code]

				row = {
					"ticket_id": ticket_id,
					"module_name": functionality_to_module.get(candidate_func, functionality_to_module.get(modified_func, "")),
					"feature_name": "; ".join(sorted(functionality_to_features.get(candidate_func, set()))),
					"modified_functionality": modified_func,
					"dependent_functionality": candidate_func,
					"relationship_type": relationship_type,
					"dependency_distance": dependency_distance,
					"impact_score_raw": raw_score,
					"impact_reason": "; ".join(reason_codes),
					"customer_priority": str(meta.get("customer_priority", "")),
					"changed_date": str(meta.get("changed_date", "")),
					"work_item_type": str(meta.get("work_item_type", "")),
					"title": str(meta.get("title", "")),
					"state": str(meta.get("state", "")),
					"area_path": str(meta.get("area_path", "")),
					"priority_rank": priority_rank,
					"age_days": age_days,
				}

				existing = ticket_candidates.get(ticket_id)
				if existing is None:
					ticket_candidates[ticket_id] = row
					continue

				existing_score = int(existing.get("impact_score_raw", 0))
				if raw_score > existing_score:
					ticket_candidates[ticket_id] = row
					continue
				if raw_score == existing_score:
					existing_distance = int(existing.get("dependency_distance", 99))
					if dependency_distance < existing_distance:
						ticket_candidates[ticket_id] = row

	scored_rows = list(ticket_candidates.values())
	scored_rows.sort(
		key=lambda item: (
			-int(item.get("impact_score_raw", 0)),
			int(item.get("dependency_distance", 99)),
			-int(item.get("priority_rank", 0)),
			int(item.get("age_days", 9999)) if isinstance(item.get("age_days", 9999), int) else 9999,
			str(item.get("ticket_id", "")),
		)
	)

	last_raw: int | None = None
	continuous_score = 100
	for row in scored_rows:
		raw = int(row.get("impact_score_raw", 0))
		if last_raw is None:
			continuous_score = 100
		elif raw < last_raw:
			continuous_score = max(1, continuous_score - 1)
		row["impact_score"] = continuous_score
		last_raw = raw
		if continuous_score >= 85:
			row["risk_level"] = "High"
		elif continuous_score >= 70:
			row["risk_level"] = "Medium"
		elif continuous_score > 0:
			row["risk_level"] = "Low"
		else:
			row["risk_level"] = "None"

	detailed_rows = [
		{
			"ticket_id": str(row.get("ticket_id", "")),
			"impact_score": str(row.get("impact_score", "")),
			"impact_score_raw": str(row.get("impact_score_raw", "")),
			"dependency_distance": str(row.get("dependency_distance", "")),
			"impact_reason": str(row.get("impact_reason", "")),
			"risk_level": str(row.get("risk_level", "")),
			"relationship_type": str(row.get("relationship_type", "")),
			"module_name": str(row.get("module_name", "")),
			"feature_name": str(row.get("feature_name", "")),
			"modified_functionality": str(row.get("modified_functionality", "")),
			"dependent_functionality": str(row.get("dependent_functionality", "")),
			"customer_priority": str(row.get("customer_priority", "")),
			"changed_date": str(row.get("changed_date", "")),
			"work_item_type": str(row.get("work_item_type", "")),
			"state": str(row.get("state", "")),
			"title": str(row.get("title", "")),
			"area_path": str(row.get("area_path", "")),
		}
		for row in scored_rows
	]

	_write_dict_csv(
		output_csv_path,
		[
			"ticket_id",
			"impact_score",
			"impact_score_raw",
			"dependency_distance",
			"impact_reason",
			"risk_level",
			"relationship_type",
			"module_name",
			"feature_name",
			"modified_functionality",
			"dependent_functionality",
			"customer_priority",
			"changed_date",
			"work_item_type",
			"state",
			"title",
			"area_path",
		],
		detailed_rows,
	)

	# Group by score AND function context to avoid mixing unrelated tickets under one label.
	score_context_groups: dict[tuple[int, str, str, str], list[dict[str, object]]] = {}
	for row in scored_rows:
		score = int(row.get("impact_score", 0))
		group_key = (
			score,
			str(row.get("modified_functionality", "")),
			str(row.get("dependent_functionality", "")),
			str(row.get("relationship_type", "")),
		)
		score_context_groups.setdefault(group_key, []).append(row)

	ordered_groups = sorted(
		score_context_groups.items(),
		key=lambda item: (
			-item[0][0],
			-len(item[1]),
			item[0][2].lower(),
			item[0][1].lower(),
		),
	)

	grouped_rows_all: list[dict[str, str]] = []
	for (score, modified_func, dependent_func, relationship_type), group in ordered_groups:
		ticket_ids = [str(item.get("ticket_id", "")) for item in group if str(item.get("ticket_id", ""))]
		modules = sorted({str(item.get("module_name", "")) for item in group if str(item.get("module_name", ""))})
		features = sorted({str(item.get("feature_name", "")) for item in group if str(item.get("feature_name", ""))})
		reasons = sorted({str(item.get("impact_reason", "")) for item in group if str(item.get("impact_reason", ""))})
		min_distance = min(int(item.get("dependency_distance", 99)) for item in group)
		risk_rank = {"High": 3, "Medium": 2, "Low": 1, "None": 0}
		risk_level = sorted(
			(str(item.get("risk_level", "None")) for item in group),
			key=lambda value: risk_rank.get(value, 0),
			reverse=True,
		)[0]
		preview = ticket_ids[:5]
		more = max(0, len(ticket_ids) - len(preview))
		preview_text = "; ".join(preview)
		grouped_rows_all.append(
			{
				"impact_score": str(score),
				"ticket_count": str(len(ticket_ids)),
				"ticket_ids_preview": preview_text,
				"ticket_ids_all": "; ".join(ticket_ids),
				"ticket_more_count": str(more),
				"module_name": modules[0] if modules else "",
				"feature_name": "; ".join(features[:2]),
				"modified_functionality": modified_func,
				"dependent_functionality": dependent_func,
				"relationship_type": relationship_type,
				"dependency_distance": str(min_distance),
				"impact_reason": reasons[0] if reasons else "",
				"risk_level": risk_level,
			}
		)

	max_client_score_levels = 500
	grouped_rows = grouped_rows_all[:top_score_levels]
	grouped_rows_client = grouped_rows_all[:max_client_score_levels]

	risk_counts = {
		"high": sum(1 for row in scored_rows if str(row.get("risk_level")) == "High"),
		"medium": sum(1 for row in scored_rows if str(row.get("risk_level")) == "Medium"),
		"low": sum(1 for row in scored_rows if str(row.get("risk_level")) == "Low"),
	}

	functionality_cards = []
	for func, count in sorted(functionality_to_count.items(), key=lambda item: item[1], reverse=True):
		functionality_cards.append({
			"functionality": func,
			"module_name": functionality_to_module.get(func, ""),
			"ticket_count": count,
		})

	return {
		"cards": {
			"total_modules": len(module_cards),
			"total_functionalities": len(functionality_to_count),
			"total_modified_functions": len(modified_functions),
			"candidate_rows": len(scored_rows),
			"high_risk_rows": risk_counts["high"],
		},
		"top_modules": module_cards,
		"top_functionalities": functionality_cards,
		"risk_counts": risk_counts,
		"recurrence_candidates": grouped_rows,
		"recurrence_candidates_all": grouped_rows_client,
		"available_score_levels": len(grouped_rows_client),
		"total_score_levels": len(grouped_rows_all),
		"outputs": {
			"recurrence_csv": str(output_csv_path),
			"module_summary_csv": str(DEFAULT_MODULE_SUMMARY_CSV),
			"functionality_summary_csv": str(DEFAULT_FUNCTIONALITY_SUMMARY_CSV),
		},
	}


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
	return _ui_html()


@app.get("/brand-logo")
async def brand_logo() -> FileResponse:
	return FileResponse(
		LANDING_LOGO_PATH,
		headers={
			"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
			"Pragma": "no-cache",
			"Expires": "0",
		},
	)


@app.post("/api/predictive/ml-score-candidates")
async def ml_score_candidates(request: dict) -> dict:
	"""Score candidate groups using individual ticket data from the ADO export CSV.

	Expects: {"candidates": [{"candidate_idx": 0, "ticket_ids": "694923; 701234", "module_name": "...", ...}]}
	Returns: {"scores": [{"candidate_idx": 0, "max_risk_score": 0.41, "predicted_label": 0, "tickets_scored": 2}]}
	"""
	import csv as _csv
	from src.services.ml_inference_service import get_ml_inference_service

	candidates = request.get("candidates") or []
	if not candidates:
		return {"scores": []}

	# Load ticket lookup from CSV (id → row dict)
	ticket_lookup: dict[str, dict] = {}
	if DEFAULT_TICKET_EXPORT_CSV.exists():
		try:
			with DEFAULT_TICKET_EXPORT_CSV.open(newline="", encoding="utf-8-sig") as f:
				for row in _csv.DictReader(f):
					tid = str(row.get("id") or row.get("work_item_id") or "").strip()
					if tid:
						ticket_lookup[tid] = row
		except Exception:
			pass

	try:
		svc = get_ml_inference_service()
	except FileNotFoundError:
		return {"scores": [], "error": "ML model not available"}
	except Exception:
		return {"scores": [], "error": "Failed to load ML model"}

	results = []
	for cand in candidates:
		idx = cand.get("candidate_idx", 0)
		raw_ids = str(cand.get("ticket_ids") or "")
		ticket_ids = [t.strip() for t in raw_ids.replace(";", ",").split(",") if t.strip()]

		# Build per-ticket inputs from CSV lookup
		ticket_inputs = []
		for tid in ticket_ids:
			row = ticket_lookup.get(tid)
			if row:
				ticket_inputs.append({
					"ticket_id": tid,
					"title": row.get("title") or row.get("work_item_title") or None,
					"changed_date": row.get("changed_date") or row.get("created_date") or None,
					"customer_priority": row.get("customer_priority") or row.get("priority") or None,
					"work_item_type": row.get("work_item_type") or row.get("type") or None,
					"module_name": row.get("module") or row.get("module_name") or cand.get("module_name") or None,
					"modified_functionality": cand.get("modified_functionality") or None,
				})

		# Fallback to aggregate candidate data if no tickets found in CSV
		if not ticket_inputs:
			ticket_inputs = [{
				"ticket_id": str(idx),
				"module_name": cand.get("module_name") or None,
				"modified_functionality": cand.get("modified_functionality") or None,
				"customer_priority": cand.get("customer_priority") or None,
				"work_item_type": "Bug",
			}]

		try:
			scored = svc.score_tickets(ticket_inputs)
			max_score = max(s["risk_score"] for s in scored)
			threshold = scored[0]["threshold_used"] if scored else 0.42
			results.append({
				"candidate_idx": idx,
				"max_risk_score": round(max_score, 6),
				"predicted_label": int(max_score >= threshold),
				"threshold_used": threshold,
				"model_version": scored[0]["model_version"] if scored else "unknown",
				"tickets_scored": len(ticket_inputs),
			})
		except Exception:
			results.append({
				"candidate_idx": idx,
				"max_risk_score": 0.0,
				"predicted_label": 0,
				"threshold_used": 0.42,
				"model_version": "unknown",
				"tickets_scored": 0,
			})

	return {"scores": results}


def _ui_html() -> str:
	return r"""<!doctype html>
<html lang="en">
<head>
	<meta charset="utf-8"/>
	<meta name="viewport" content="width=device-width, initial-scale=1"/>
	<title>SmartCare QA AI Assistant</title>
	<style>
		:root{
			--bg:#f4f6fb;
			--bg-grad-a:#f7fafc;
			--bg-grad-b:#eef4ff;
			--panel:#ffffff;
			--panel-soft:#f7f9ff;
			--line:#dbe4f1;
			--text:#1d2738;
			--muted:#637289;
			--brand:#1f6feb;
			--brand-strong:#184fb4;
			--accent:#ff8c42;
			--accent-2:#f65f71;
			--ok:#18a57a;
			--shadow:0 18px 40px rgba(24, 44, 79, 0.12);
			--radius-lg:20px;
			--radius-md:14px;
			--radius-sm:10px;
		}

		*{box-sizing:border-box;}
		body{
			margin:0;
			font-family:"Segoe UI",Tahoma,Geneva,Verdana,sans-serif;
			color:var(--text);
			background:
				radial-gradient(circle at 8% 8%, rgba(31,111,235,0.10), transparent 42%),
				radial-gradient(circle at 88% 14%, rgba(246,95,113,0.10), transparent 36%),
				linear-gradient(165deg,var(--bg-grad-a),var(--bg-grad-b));
			min-height:100vh;
		}

		.shell{max-width:1400px;margin:0 auto;padding:34px 20px 90px;}
		.screen{display:none;}
		.screen.active{display:block;}

		.top-strip{
			display:flex;
			justify-content:space-between;
			align-items:center;
			margin-bottom:18px;
			color:var(--muted);
			font-size:14px;
		}
		.top-brand{display:flex;align-items:center;gap:10px;font-weight:600;}
		.orb{width:12px;height:12px;border-radius:50%;background:linear-gradient(135deg,var(--brand),var(--accent));box-shadow:0 0 0 5px rgba(31,111,235,0.10);}

		.card{
			background:var(--panel);
			border:1px solid var(--line);
			border-radius:var(--radius-lg);
			box-shadow:var(--shadow);
			overflow:hidden;
		}

		.card-head{
			background:linear-gradient(90deg,#f8fbff,#f8f4ff);
			border-bottom:1px solid var(--line);
			padding:18px 24px;
			display:flex;
			align-items:center;
			justify-content:space-between;
			gap:12px;
		}

		.brand-lockup{display:flex;align-items:center;gap:12px;}
		.brand-logo{display:block;width:min(100%,560px);height:auto;}
		.brand-icon{
			width:38px;
			height:38px;
			border-radius:12px;
			background:linear-gradient(150deg,var(--accent-2),var(--accent));
			color:#fff;
			display:flex;
			align-items:center;
			justify-content:center;
			font-size:19px;
			font-weight:700;
		}
		.brand-title{margin:0;font-size:30px;font-weight:700;letter-spacing:0.2px;}
		.brand-sub{margin:2px 0 0;color:var(--muted);font-size:16px;}

		.ghost-btn{
			border:1px solid var(--line);
			background:#fff;
			color:var(--muted);
			border-radius:999px;
			padding:8px 14px;
			font-weight:600;
			cursor:pointer;
		}
		.ghost-btn:hover{border-color:#bccce5;color:#45566f;}

		.home-body{padding:40px 30px 34px;text-align:center;}
		.home-title{margin:0;font-size:56px;line-height:1.05;letter-spacing:-1px;}
		.home-title .grad{
			background:linear-gradient(120deg,var(--brand),var(--accent-2));
			-webkit-background-clip:text;
			-webkit-text-fill-color:transparent;
			background-clip:text;
			color:transparent;
		}
		.home-copy{max-width:750px;margin:14px auto 0;color:var(--muted);font-size:24px;line-height:1.45;}

		.mode-switch{
			margin:30px auto 0;
			display:inline-grid;
			grid-template-columns:1fr 1fr 1fr;
			background:var(--panel-soft);
			border:1px solid var(--line);
			border-radius:12px;
			overflow:hidden;
		}

		.mode-btn{
			border:none;
			background:transparent;
			color:var(--muted);
			padding:13px 30px;
			font-size:27px;
			font-weight:600;
			cursor:pointer;
			min-width:280px;
		}
		.mode-btn.active{background:linear-gradient(120deg,var(--brand),var(--brand-strong));color:#fff;}

		.hero-art{
			margin:34px auto 0;
			width:min(840px,100%);
			background:linear-gradient(180deg,#ffffff,#f8fbff);
			border:1px solid var(--line);
			border-radius:22px;
			padding:30px;
		}

		.blob{
			margin:0 auto;
			width:min(560px,100%);
			aspect-ratio:16/7;
			border-radius:999px;
			background:
				radial-gradient(circle at 22% 45%, rgba(31,111,235,0.22), transparent 28%),
				radial-gradient(circle at 70% 30%, rgba(246,95,113,0.24), transparent 28%),
				linear-gradient(140deg, #eef4ff, #fff4f6);
			border:1px solid var(--line);
			position:relative;
			overflow:hidden;
		}
		.blob:before{
			content:"";
			position:absolute;
			left:10%;
			right:10%;
			bottom:14%;
			height:20%;
			border-radius:14px;
			background:linear-gradient(90deg,#dde8fc,#fde3e8);
		}
		.blob:after{
			content:"AI QA Copilot";
			position:absolute;
			left:50%;
			top:48%;
			transform:translate(-50%,-50%);
			font-size:34px;
			font-weight:700;
			color:#5a6882;
			letter-spacing:0.5px;
		}

		.frame{padding:18px;}
		.panel-grid{display:grid;grid-template-columns:1fr;gap:16px;padding:20px;}
		.bar{
			display:flex;
			align-items:center;
			justify-content:space-between;
			padding:11px 16px;
			border:1px solid var(--line);
			border-radius:12px;
			background:linear-gradient(90deg,#fbfdff,#f6f9ff);
			color:var(--muted);
			font-size:14px;
		}
		.bar-title{display:flex;align-items:center;gap:10px;color:#2f3a4f;font-size:24px;font-weight:700;}
		.left-link{border:none;background:transparent;color:var(--muted);font-size:28px;cursor:pointer;line-height:1;}

		.field-wrap{padding:6px 4px 0;display:grid;gap:8px;justify-items:center;}
		.field-wrap label{font-size:40px;font-weight:600;}
		.input{
			width:min(760px,100%);
			border:1px solid var(--line);
			border-radius:10px;
			padding:14px 16px;
			font-size:34px;
			color:var(--text);
			background:#fff;
			outline:none;
		}
		.input:focus{border-color:#9eb8ea;box-shadow:0 0 0 3px rgba(31,111,235,0.10);}

		.upload-row{
			border:1px solid var(--line);
			border-radius:10px;
			background:#fff;
			padding:12px 14px;
			color:#90a0b8;
			font-size:34px;
			display:flex;
			align-items:center;
			gap:10px;
		}
		.action-grid{display:grid;grid-template-columns:repeat(3,minmax(190px,1fr));gap:16px;}

		.action-btn{
			border:none;
			border-radius:12px;
			padding:20px 16px;
			color:#fff;
			font-size:35px;
			font-weight:700;
			cursor:pointer;
			box-shadow:0 10px 18px rgba(20,34,58,0.15);
			line-height:1.18;
			min-height:126px;
		}
		.action-btn.primary{background:linear-gradient(130deg,#f65f71,#f9735f);}
		.action-btn.warn{background:linear-gradient(130deg,#ff8c42,#ff6c5c);}
		.action-btn.alt{background:linear-gradient(130deg,#ff9a4d,#ff7272);}
		.action-btn:disabled{opacity:0.55;cursor:default;}

		.struct-shell{display:grid;gap:14px;}
		.struct-grid{display:grid;grid-template-columns:repeat(4,minmax(170px,1fr));gap:12px;}
		.struct-field{display:flex;flex-direction:column;gap:6px;}
		.struct-field label{font-size:12px;font-weight:700;color:#4d5f79;text-transform:uppercase;letter-spacing:0.4px;}
		.struct-field input,.struct-field select{
			border:1px solid var(--line);
			border-radius:10px;
			padding:10px 12px;
			font-size:14px;
			background:#fff;
			color:#2f3a4f;
		}
		.struct-field input:focus,.struct-field select:focus{outline:none;border-color:#9eb8ea;box-shadow:0 0 0 3px rgba(31,111,235,0.10);}
		.struct-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
		.struct-run{border:none;border-radius:10px;padding:11px 18px;font-weight:700;font-size:14px;color:#fff;background:linear-gradient(120deg,#0ca06f,#0d7f5a);cursor:pointer;}
		.struct-run:disabled{background:linear-gradient(120deg,#b0bec5,#90a4ae);cursor:not-allowed;opacity:0.6;}
		.struct-run.alt{background:linear-gradient(120deg,#0ea5e9,#2563eb);}
		.struct-run.chat{background:linear-gradient(120deg,#f59e0b,#d97706);}
		.struct-hint{font-size:12px;color:#677a95;}
		.kpi-grid{display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:12px;}
		.kpi-card{border-radius:12px;padding:12px 14px;color:#fff;box-shadow:0 8px 18px rgba(20,34,58,0.15);}
		.kpi-card h4{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.9;}
		.kpi-card .val{margin-top:6px;font-size:26px;font-weight:800;line-height:1;}
		.kpi-a{background:linear-gradient(120deg,#2b6df5,#1949ba);}
		.kpi-b{background:linear-gradient(120deg,#00a88f,#0a7f6d);}
		.kpi-c{background:linear-gradient(120deg,#4caf50,#2e7d32);}
		.kpi-d{background:linear-gradient(120deg,#ffc107,#ff9800);}
		.kpi-e{background:linear-gradient(120deg,#ff5252,#c41c3b);}
		.kpi-f{background:linear-gradient(120deg,#ff6b6b,#ee5a52);}
		.report-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
		.report-box{border:1px solid var(--line);border-radius:12px;background:#fff;padding:12px;}
		.report-title{margin:0 0 8px;font-size:13px;font-weight:700;color:#4a5f7e;text-transform:uppercase;letter-spacing:0.4px;}
		.bar-row{display:grid;grid-template-columns:1fr 60px;gap:8px;align-items:center;margin:7px 0;}
		.bar-track{height:12px;background:#edf2fb;border-radius:999px;overflow:hidden;}
		.bar-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#2b6df5,#7a46c5);}
		.pill{display:inline-flex;align-items:center;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:700;border:1px solid #d8c8ef;background:#f7efff;color:#5e2d87;}
		.table-wrap{max-height:280px;overflow:auto;border:1px solid var(--line);border-radius:10px;background:#fff;}
		.table-wrap-full{overflow:auto;border:1px solid var(--line);border-radius:10px;background:#fff;}
		.table{width:100%;border-collapse:collapse;font-size:12px;}
		.table th,.table td{border-bottom:1px solid #edf1f9;padding:8px 9px;vertical-align:top;text-align:left;}
		.table th{position:sticky;top:0;background:#f7f9ff;color:#4b5f7f;z-index:1;}
		.row-risk-high{background:#fff1f2;}
		.row-risk-medium{background:#fff8ef;}
		.badge-risk{display:inline-flex;align-items:center;padding:2px 7px;border-radius:999px;font-size:11px;font-weight:700;}
		.badge-risk-high{background:#ffe4e6;color:#b91c1c;border:1px solid #fca5a5;}
		.badge-risk-medium{background:#fef3c7;color:#92400e;border:1px solid #fcd34d;}
		.badge-risk-low{background:#dcfce7;color:#166534;border:1px solid #86efac;}
		.row-risk-low{background:#f4fff8;}

		.result-box{
			border:1px solid var(--line);
			border-radius:12px;
			background:#fff;
			padding:16px;
			min-height:220px;
			overflow:auto;
		}
		.result-title{font-size:40px;font-weight:600;margin-bottom:8px;color:#546279;}
		.result-content{font-size:33px;white-space:pre-wrap;line-height:1.45;color:#2f3a4f;}
		.presidio-card{
			display:grid;
			grid-template-columns:repeat(3,minmax(120px,1fr));
			gap:10px;
			border:1px solid var(--line);
			border-radius:12px;
			background:linear-gradient(180deg,#fbfdff,#f7faff);
			padding:12px;
		}
		.presidio-item{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 12px;}
		.presidio-label{font-size:12px;color:#70829d;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;}
		.presidio-value{font-size:16px;font-weight:700;color:#334156;margin-top:4px;}
		.presidio-value.ok{color:#148f6b;}
		.presidio-value.warn{color:#b2552b;}

		.chat-shell{
			background:#fff;
			border:1px solid var(--line);
			border-radius:18px;
			box-shadow:var(--shadow);
			min-height:80vh;
			display:grid;
			grid-template-columns:280px 1fr;
			overflow:hidden;
		}

		.chat-nav{
			background:linear-gradient(180deg,#f7f9ff,#eef3fb);
			border-right:1px solid var(--line);
			padding:18px;
			display:flex;
			flex-direction:column;
			gap:12px;
		}
		.chat-search-wrap{
			position:relative;
		}
		.chat-search-icon{
			position:absolute;
			left:10px;
			top:50%;
			transform:translateY(-50%);
			font-size:13px;
			color:#7a8da9;
			pointer-events:none;
		}
		.chat-search-input{
			width:100%;
			height:38px;
			border:1px solid var(--line);
			border-radius:10px;
			background:#fff;
			padding:0 12px 0 30px;
			font-size:13px;
			color:#334156;
		}
		.chat-search-input:focus{
			outline:none;
			border-color:#9fb8e8;
			box-shadow:0 0 0 3px rgba(31,111,235,0.12);
		}
		.new-chat{
			border:none;
			border-radius:12px;
			background:linear-gradient(120deg,var(--brand),var(--brand-strong));
			color:#fff;
			font-size:15px;
			font-weight:700;
			padding:11px 14px;
			text-align:left;
			cursor:pointer;
		}
		.chat-item{
			border:1px solid var(--line);
			border-radius:10px;
			padding:10px;
			background:#fff;
			color:#4f5f76;
			font-size:13px;
			cursor:pointer;
		}
		.chat-item.active{border-color:#9fb8e8;background:#eef4ff;color:#2f4e84;}
		.chat-section-title{font-size:12px;font-weight:700;color:#6b7c95;text-transform:uppercase;letter-spacing:0.7px;margin-top:2px;}
		.chat-history{display:grid;gap:8px;max-height:40vh;overflow:auto;padding-right:2px;}

		.chat-main{display:flex;flex-direction:column;min-height:70vh;}
		.chat-top{
			border-bottom:1px solid var(--line);
			padding:14px 20px;
			display:flex;
			justify-content:space-between;
			align-items:center;
			background:#fff;
		}
		.chat-top h2{margin:0;font-size:19px;}
		.chat-status{font-size:12px;color:var(--ok);font-weight:700;}

		.chat-feed{
			flex:1;
			overflow:auto;
			padding:26px 22px;
			background:
				radial-gradient(circle at 30% 0%, rgba(31,111,235,0.06), transparent 36%),
				radial-gradient(circle at 80% 0%, rgba(255,140,66,0.08), transparent 34%),
				#fcfdff;
		}

		.bubble{max-width:840px;margin:0 auto 16px;display:flex;gap:12px;align-items:flex-start;}
		.avatar{
			width:34px;height:34px;border-radius:9px;display:flex;align-items:center;justify-content:center;
			font-size:14px;font-weight:700;flex:0 0 auto;
		}
		.avatar.user{background:#e7f0ff;color:#2856a6;}
		.avatar.assistant{background:#ffece2;color:#aa4f2e;}
		.bubble-card{
			background:#fff;
			border:1px solid var(--line);
			border-radius:12px;
			padding:12px 14px;
			line-height:1.5;
			white-space:pre-wrap;
			color:#2f3a4f;
			width:100%;
		}
		.chat-markdown-table-wrap{overflow:auto;margin:8px 0;}
		.chat-markdown-table{
			width:100%;
			border-collapse:collapse;
			font-size:13px;
			min-width:680px;
		}
		.chat-markdown-table th,
		.chat-markdown-table td{
			border:1px solid var(--line);
			padding:8px 10px;
			vertical-align:top;
			white-space:normal;
		}
		.chat-markdown-table th{background:#f5f8ff;color:#405575;text-align:left;}
		.chat-text-block{margin:0 0 8px;white-space:pre-wrap;}
		.chat-meta{
			display:flex;
			flex-wrap:wrap;
			gap:6px;
			margin-bottom:10px;
		}
		.chat-meta-pill{
			display:inline-flex;
			align-items:center;
			padding:4px 8px;
			border-radius:999px;
			background:#f2f6fd;
			border:1px solid var(--line);
			font-size:11px;
			font-weight:700;
			color:#53657f;
			white-space:normal;
		}

		.chat-input-wrap{
			border-top:1px solid var(--line);
			background:#fff;
			padding:14px 18px 16px;
		}
		.composer{
			border:1px solid var(--line);
			border-radius:14px;
			background:#fff;
			display:flex;
			gap:10px;
			align-items:flex-end;
			padding:10px;
		}
		.composer textarea{
			border:none;
			resize:none;
			outline:none;
			min-height:44px;
			max-height:180px;
			width:100%;
			font:inherit;
			color:var(--text);
			line-height:1.4;
		}
		.send{
			border:none;
			border-radius:10px;
			padding:10px 14px;
			background:linear-gradient(120deg,var(--brand),var(--brand-strong));
			color:#fff;
			font-weight:700;
			cursor:pointer;
		}

		.hidden{display:none;}
		.muted{color:var(--muted);}
		.loading{opacity:0.75;pointer-events:none;}
		footer.watermark{position:fixed;bottom:10px;right:14px;font-size:11px;color:rgba(38,60,97,0.45);background:rgba(255,255,255,0.75);padding:4px 8px;border-radius:4px;z-index:9999;user-select:none;pointer-events:none;}

		/* Layout theme inspired by the requested automation suite structure */
		body{background:#ede9f4;}
		.shell{max-width:none;margin:0;padding:0 0 40px;}
		.top-strip{display:none;}
		.logo-bar{
			width:100%;
			height:auto;
			background:#6b0042;
			border-bottom:none;
			display:flex;
			align-items:center;
			justify-content:flex-start;
			padding:0 20px;
		}
		.logo-bar img{
			width:auto;
			height:auto;
			max-width:100%;
			object-fit:contain;
			display:block;
		}
		.screen{padding:20px;}
		.chat-shell{
			min-height:72vh;
			border:2px solid #6e2f95;
			border-radius:12px;
			box-shadow:none;
			grid-template-columns:320px 1fr;
		}
		.chat-nav{
			background:#f8f3fc;
			border-right:2px solid #dcc7ea;
			padding:14px 12px;
		}
		.chat-search-input{border:1px solid #ccb4df;}
		.chat-search-input:focus{border-color:#8e5bb3;box-shadow:0 0 0 3px rgba(120,57,169,0.16);}
		.new-chat{background:#0ca06f;font-size:15px;}
		.chat-section-title{color:#5f3d7c;font-size:11px;}
		.chat-item{border:1px solid #d9c6e8;}
		.chat-item.active{border-color:#8e5bb3;background:#efe4f8;color:#3f1f5d;}
		.chat-main{background:#f4f0f8;min-height:72vh;padding:16px;}
		.chat-top{
			border:2px solid #6e2f95;
			border-radius:10px;
			background:#fff;
			padding:12px 16px;
			margin-bottom:14px;
		}
		.chat-top h2{color:#3e1d5a;font-size:30px;}
		.chat-status{color:#6f3f96;font-size:13px;}
		.chat-feed{
			background:#ffffff;
			border:1px solid #d7c5e7;
			border-radius:10px;
			padding:18px;
		}
		.chat-input-wrap{
			margin-top:12px;
			border:1px solid #d7c5e7;
			border-radius:10px;
			background:#fff;
		}
		.send{background:linear-gradient(120deg,#6c1f99,#5a1884);}

		@media (max-width:1024px){
			.chat-top h2{font-size:24px;}
			.home-title{font-size:44px;}
			.home-copy{font-size:20px;}
			.mode-btn{font-size:22px;min-width:220px;}
			.field-wrap label{font-size:28px;}
			.input,.upload-row,.result-title,.result-content,.action-btn{font-size:24px;}
			.action-btn{min-height:106px;}
		}

		@media (max-width:900px){
			.logo-bar{max-width:100%;}
			.logo-bar img{max-width:100%;height:auto;}
			.action-grid{grid-template-columns:1fr;}
			.chat-shell{grid-template-columns:1fr;}
			.chat-nav{border-right:none;border-bottom:1px solid #dcc7ea;}
			.shell{padding:18px 10px 80px;}
			.card-head{padding:14px;}
			.home-body{padding:28px 16px;}
			.brand-title{font-size:24px;}
			.brand-sub{font-size:14px;}
			.home-title{font-size:36px;}
			.mode-switch{display:grid;width:100%;}
			.mode-btn{font-size:18px;min-width:auto;padding:12px 14px;}
			.input{font-size:20px;}
			.upload-row{font-size:20px;}
			.action-btn{font-size:24px;}
			.result-title{font-size:28px;}
			.result-content{font-size:20px;}
			.struct-grid{grid-template-columns:1fr 1fr;}
			.kpi-grid{grid-template-columns:1fr 1fr;}
			.report-grid{grid-template-columns:1fr;}
		}
	</style>
</head>
<body>
	<main class="shell">
		<div class="logo-bar">
			<img src="/brand-logo" alt="Streamline Healthcare"/>
		</div>
		<section id="homeView" class="screen active">
			<article class="card">
				<div class="card-head">
					<div class="brand-lockup"></div>
					<div></div>
				</div>

				<div class="home-body">
					<h2 class="home-title"><span class="grad">QA AI Assistant</span></h2>
					<p class="home-copy">Welcome to the AI-powered QA Assistant for smarter, faster, and automated QA testing.</p>

					<div class="mode-switch">
						<button class="mode-btn active" id="goFreeTextHome" onclick="showView('freetext')">Free Text View</button>
						<button class="mode-btn" id="goKeywordHome" disabled style="opacity:0.55;cursor:not-allowed;" title="Temporarily disabled">Keyword View</button>
						<button class="mode-btn" id="goStructuredHome" onclick="showView('structured')">Defect Predictive Analysis</button>
					</div>

					<div class="hero-art">
						<div class="blob"></div>
					</div>
				</div>
			</article>
		</section>

		<section id="structuredView" class="screen">
			<article class="card frame">
				<div class="bar">
					<div style="display:flex;align-items:center;gap:10px;">
						<button class="left-link" onclick="showView('home')">&#8249;</button>
						<span class="muted" style="font-weight:700;">Home</span>
					</div>
					<div class="bar-title"><span class="brand-icon" style="width:30px;height:30px;border-radius:8px;font-size:13px;">AI</span>Predictive QA Flow</div>
					<button class="ghost-btn" disabled style="opacity:0.55;cursor:not-allowed;" title="Temporarily disabled">Keyword View &#8250;</button>
				</div>

				<div class="panel-grid struct-shell">
					<div class="struct-grid">
						<div class="struct-field"><label>Release Name</label><input id="pfRelease" value="MSP Default Run"/></div>
						<div class="struct-field"><label>Days Window</label><input id="pfDays" type="number" min="1" value="180"/></div>
						<div class="struct-field"><label>Work Item Scope</label><input id="pfScope" value="Bug,Customer Ticket"/></div>
						<div class="struct-field"><label>Priority Scope</label><input id="pfPriority" value="On Fire,Urgent,High"/></div>
						<div class="struct-field"><label>Match Mode</label><select id="pfMatch"><option value="strict" selected>Strict</option><option value="fuzzy">Fuzzy</option></select></div>
						<div class="struct-field"><label>Include Dependencies</label><select id="pfDeps"><option value="true" selected>Yes</option><option value="false">No</option></select></div>
						<div class="struct-field"><label>MSP Workbook (.xlsx)</label><input id="pfMspFile" type="file" accept=".xlsx,.xlsm,.xls"/></div>
						<div class="struct-field"><label>MSP Sheet Name</label><input id="pfMspSheet" value="6.0_1-AprilMSP_2026"/></div>
						<div class="struct-field"><label>MSP Target Category</label><input id="pfMspCategory" value="Engineering Improvement Initiatives- NBL(I)"/></div>
						<div class="struct-field"><label>Refresh ADO Export</label><select id="pfRefreshAdo"><option value="true" selected>Yes</option><option value="false">No (use existing CSV)</option></select></div>
						<div class="struct-field"><label>Apply Query Update</label><select id="pfApplyQuery"><option value="false" selected>No</option><option value="true">Yes</option></select></div>
						<div class="struct-field"><label>Modified Functions CSV</label><input id="pfModifiedFile" type="file" accept=".csv"/></div>
						<div class="struct-field"><label>Dependency Metrics CSV</label><input id="pfDependencyFile" type="file" accept=".csv"/></div>
					</div>

					<div class="struct-actions">
						<button id="pfRunE2EBtn" class="struct-run" onclick="runPredictiveE2E()">Run End-to-End (MSP -> Final)</button>
						<button id="pfRunBtn" class="struct-run alt" onclick="runPredictiveDefaults()">Run Quick (Existing CSVs)</button>
						<button class="struct-run chat" onclick="showView('freetext')">Open Chat Assistant</button>
						<span id="pfStatus" class="struct-hint">Ready. Uses default CSVs if files are not uploaded.</span>
					</div>

					<div id="pfKpis" class="kpi-grid">
						<div class="kpi-card kpi-a"><h4>Total Modules</h4><div class="val" id="kpiModules">-</div></div>
						<div class="kpi-card kpi-b"><h4>Functionalities</h4><div class="val" id="kpiFuncs">-</div></div>
						<div class="kpi-card kpi-c"><h4>Low Risk</h4><div class="val" id="kpiLowRisk">-</div></div>
						<div class="kpi-card kpi-d"><h4>Medium Risk</h4><div class="val" id="kpiMediumRisk">-</div></div>
						<div class="kpi-card kpi-e"><h4>High Risk</h4><div class="val" id="kpiHighRisk">-</div></div>
						<div class="kpi-card kpi-f"><h4>Total Tickets</h4><div class="val" id="kpiTotalTickets">-</div></div>
					</div>

					<div class="report-grid">
						<div class="report-box">
							<p class="report-title">Modules by Tickets (Full List)</p>
							<div id="pfTopModules"></div>
						</div>
						<div class="report-box">
							<p class="report-title">Functionalities by Tickets (Full List)</p>
							<div id="pfTopFuncs"></div>
						</div>
					</div>

					<div class="report-box">
						<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
							<p class="report-title" id="pfTopScoresTitle">Probable Recurrence Candidates</p>
							<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
								<label for="pfTopLevels" class="muted" style="font-size:12px;font-weight:700;">Score Levels</label>
								<input id="pfTopLevels" type="text" list="pfTopLevelsList" value="50" style="width:86px;"/>
								<datalist id="pfTopLevelsList"><option value="10"></option><option value="25"></option><option value="50"></option><option value="75"></option><option value="100"></option><option value="150"></option><option value="200"></option></datalist>
								<label for="pfModuleFilter" class="muted" style="font-size:12px;font-weight:700;margin-left:8px;">Module</label>
								<select id="pfModuleFilter" style="min-width:170px;"><option value="">All Modules</option></select>
								<label for="pfFunctionFilter" class="muted" style="font-size:12px;font-weight:700;margin-left:8px;">Function</label>
								<select id="pfFunctionFilter" style="min-width:220px;"><option value="">All Functions</option></select>
								<label for="pfAiRiskFilter" class="muted" style="font-size:12px;font-weight:700;margin-left:8px;">AI Risk</label>
								<select id="pfAiRiskFilter" style="min-width:120px;"><option value="">All</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select>
							</div>
						</div>
						<div class="table-wrap-full"><table class="table"><thead><tr><th>Score</th><th>Tickets</th><th>Ticket IDs</th><th id="pfCandidateRelationshipHead">Relationship</th><th id="pfCandidateHopHead">Dependency Hop</th><th>Module</th><th>Modified Function</th><th>Reason</th><th style="display:none">Risk</th><th title="AI model reopen risk score">AI Risk</th></tr></thead><tbody id="pfCandidateRows"><tr><td colspan="10" class="muted">Run the flow to populate results.</td></tr></tbody></table></div>
						<div id="pfPagination" style="display:none;align-items:center;justify-content:space-between;margin-top:10px;gap:8px;flex-wrap:wrap;">
							<button id="pfPagePrev" class="struct-run" style="padding:6px 14px;font-size:13px;" onclick="changePredictivePage(-1)" disabled>&#8249; Prev</button>
							<span id="pfPageInfo" class="muted" style="font-size:13px;font-weight:600;"></span>
							<button id="pfPageNext" class="struct-run" style="padding:6px 14px;font-size:13px;" onclick="changePredictivePage(1)">Next &#8250;</button>
						</div>
						<div style="margin-top:10px;text-align:right;">
							<button class="struct-run" style="padding:6px 16px;font-size:13px;background:linear-gradient(120deg,#0ca06f,#0d7f5a);" onclick="downloadCandidatesCSV()">&#8595; Download CSV</button>
						</div>
					</div>

					<div class="struct-hint" id="pfOutputHint"></div>
				</div>
			</article>
		</section>

		<section id="keywordView" class="screen">
			<article class="card frame">
				<div class="bar">
					<div style="display:flex;align-items:center;gap:10px;">
						<button class="left-link" onclick="showView('home')">&#8249;</button>
						<span class="muted" style="font-weight:700;">Home</span>
					</div>
					<div class="bar-title"><span class="brand-icon" style="width:30px;height:30px;border-radius:8px;font-size:13px;">AI</span>QA AI Assistant</div>
					<button class="ghost-btn" onclick="showView('freetext')">Chat View</button>
				</div>

				<div class="panel-grid">
					<div class="field-wrap">
						<label for="ticketInput">Enter Ticket IDs:</label>
						<input id="ticketInput" class="input" placeholder="TCK-123, TCK-456, TCK-789"/>
					</div>

					<div class="upload-row">&#128206; File</div>

					<div class="presidio-card">
						<div class="presidio-item">
							<div class="presidio-label">Presidio Check</div>
							<div id="keywordPresidio" class="presidio-value">Unknown</div>
						</div>
						<div class="presidio-item">
							<div class="presidio-label">Sanitization Mode</div>
							<div id="keywordSanitizationMode" class="presidio-value">Unknown</div>
						</div>
						<div class="presidio-item">
							<div class="presidio-label">Source</div>
							<div id="keywordSource" class="presidio-value">Not run</div>
						</div>
					</div>

					<div class="action-grid">
						<button id="btnGenerate" class="action-btn primary" onclick="runKeywordAction('generate')">Generate<br/>Test Cases</button>
						<button id="btnMissing" class="action-btn warn" onclick="runKeywordAction('missing')">Find Missing<br/>Test Cases</button>
						<button id="btnNegative" class="action-btn alt" onclick="runKeywordAction('negative')">Generate<br/>Negative &amp; Edge Cases</button>
					</div>

					<section class="result-box">
						<div class="result-title">Result:</div>
						<div id="keywordResult" class="result-content">Run an action to see generated QA output.</div>
					</section>
				</div>
			</article>
		</section>

		<section id="freetextView" class="screen">
			<article class="chat-shell">
				<aside class="chat-nav">
					<div class="chat-search-wrap">
						<span class="chat-search-icon" aria-hidden="true">&#128269;</span>
						<input id="chatHistorySearch" class="chat-search-input" type="text" placeholder="Search chats" oninput="renderChatHistory()"/>
					</div>
					<button class="new-chat" onclick="startNewChatSession()">+ New QA Chat</button>
					<div class="chat-section-title">Chat Sessions</div>
					<div id="chatHistory" class="chat-history"></div>
					<button class="ghost-btn" onclick="showView('home')">Back to Home</button>
				</aside>

				<div class="chat-main">
					<div class="chat-top">
						<div class="chat-status">Live Assistant</div>
					</div>

					<div id="chatFeed" class="chat-feed"></div>

					<div class="chat-input-wrap">
						<div class="composer">
							<button id="attachBtn" class="send" type="button" onclick="triggerContextUpload()" title="Attach files">+</button>
							<input id="contextFileInput" type="file" accept=".txt,.md,.csv,.json,.docx,.pdf" multiple style="display:none" onchange="handleContextFilesSelected(event)"/>
							<textarea id="chatInput" placeholder="Message QA AI Assistant..."></textarea>
							<button id="sendBtn" class="send" onclick="sendChat()">Send</button>
						</div>
						<div style="margin-top:8px;display:flex;align-items:center;justify-content:space-between;gap:10px;">
							<div id="attachedFiles" class="muted" style="font-size:12px;flex:1;"></div>
							<button id="clearAttachedBtn" class="ghost-btn" type="button" style="display:none;padding:4px 10px;font-size:12px;" onclick="clearAllAttachedFiles()">Clear all</button>
						</div>
					</div>
				</div>
			</article>
		</section>
	</main>

	<script>
		function showView(view) {
			document.getElementById('homeView').classList.toggle('active', view === 'home');
			document.getElementById('keywordView').classList.toggle('active', view === 'keyword');
			document.getElementById('structuredView').classList.toggle('active', view === 'structured');
			document.getElementById('freetextView').classList.toggle('active', view === 'freetext');
			document.getElementById('goFreeTextHome').classList.toggle('active', view === 'freetext');
			document.getElementById('goKeywordHome').classList.toggle('active', view === 'keyword');
			document.getElementById('goStructuredHome').classList.toggle('active', view === 'structured');
		}

		var predictiveCandidatesAll = [];
		var predictiveMlScores = {};
		var predictiveLastRunMeta = null;
		var predictiveLastRunType = '';
		var predictiveCurrentPage = 0;
		var predictivePageSize = 10;

		function getTopScoreLevelsValue() {
			var raw = String((document.getElementById('pfTopLevels') || {}).value || '50').trim();
			var parsed = Number(raw);
			if (!Number.isFinite(parsed)) {
				parsed = 50;
			}
			parsed = Math.max(1, Math.min(500, Math.floor(parsed)));
			return parsed;
		}

		function setPredictiveBusyCursor(isBusy) {
			document.body.style.cursor = isBusy ? 'progress' : '';
		}

		function stageLabel(step) {
			var key = String(step || '').toLowerCase();
			var labels = {
				'validation': 'Validating Inputs',
				'msp_feature_module_mapper': 'MSP Mapping',
				'ado_export': 'ADO Export',
				'combine_mapping': 'Combine Mapping',
				'load_inputs': 'Load Inputs',
				'predictive_scoring': 'Predictive Scoring',
				'finalizing': 'Finalizing',
				'completed': 'Completed',
				'failed': 'Failed'
			};
			return labels[key] || (step || 'Running');
		}

		function formatRunProgressText(run) {
			if (!run || typeof run !== 'object') {
				return 'Running pipeline...';
			}
			var state = String(run.state || 'running').toLowerCase();
			var step = stageLabel(run.step);
			var msg = String(run.message || '').trim();
			if (state === 'failed') {
				return 'Failed at ' + step + (msg ? ': ' + msg : '');
			}
			if (state === 'completed') {
				return 'Completed: ' + (msg || 'Pipeline completed successfully');
			}
			return '[Running] ' + step + (msg ? ' - ' + msg : '');
		}

		function startRunStatusPolling(runId, statusEl) {
			if (!runId || !statusEl) {
				return null;
			}
			var timer = setInterval(async function() {
				try {
					var res = await fetch('/api/predictive/run-status?run_id=' + encodeURIComponent(runId));
					if (!res.ok) {
						return;
					}
					var run = await res.json();
					statusEl.textContent = formatRunProgressText(run);
				} catch (_) {
					// Ignore transient polling errors while the main request is still running.
				}
			}, 1200);
			return timer;
		}

		function renderModuleBars(rows) {
			if (!Array.isArray(rows) || !rows.length) {
				return '<div class="muted">No module data.</div>';
			}
			var maxVal = rows.reduce(function(max, item){ return Math.max(max, Number(item.ticket_count || 0)); }, 1);
			return rows.map(function(item){
				var pct = Math.round((Number(item.ticket_count || 0) / maxVal) * 100);
				return '<div class="bar-row"><div><div style="font-size:12px;font-weight:700;color:#334763;">' + (item.module_name || '-') + '</div><div class="bar-track"><div class="bar-fill" style="width:' + pct + '%"></div></div></div><div style="text-align:right;font-weight:700;color:#334763;">' + String(item.ticket_count || 0) + '</div></div>';
			}).join('');
		}

		function renderFuncBars(rows) {
			if (!Array.isArray(rows) || !rows.length) {
				return '<div class="muted">No functionality data.</div>';
			}
			var maxVal = rows.reduce(function(max, item){ return Math.max(max, Number(item.ticket_count || 0)); }, 1);
			return rows.map(function(item){
				var pct = Math.round((Number(item.ticket_count || 0) / maxVal) * 100);
				return '<div class="bar-row"><div><div style="font-size:12px;font-weight:700;color:#334763;">' + (item.functionality || '-') + '</div><div style="font-size:11px;color:#6d809c;">' + (item.module_name || '') + '</div><div class="bar-track"><div class="bar-fill" style="width:' + pct + '%;background:linear-gradient(90deg,#ff8c42,#ef4e6c);"></div></div></div><div style="text-align:right;font-weight:700;color:#334763;">' + String(item.ticket_count || 0) + '</div></div>';
			}).join('');
		}

		function shouldShowDependencyColumns(rows) {
			if (!Array.isArray(rows) || !rows.length) {
				return true;
			}
			return rows.some(function(item) {
				var relationship = String(item.relationship_type || '').toLowerCase();
				var distance = Number(item.dependency_distance || 0);
				return relationship !== 'direct_change' || distance !== 0;
			});
		}

		function setCandidateDependencyColumnsVisible(isVisible) {
			var relationHead = document.getElementById('pfCandidateRelationshipHead');
			var hopHead = document.getElementById('pfCandidateHopHead');
			if (relationHead) {
				relationHead.style.display = isVisible ? '' : 'none';
			}
			if (hopHead) {
				hopHead.style.display = isVisible ? '' : 'none';
			}
		}

		function renderCandidateRows(rows) {
			var showDependencyColumns = shouldShowDependencyColumns(rows);
			setCandidateDependencyColumnsVisible(showDependencyColumns);
			if (!Array.isArray(rows) || !rows.length) {
				return '<tr><td colspan="' + String(showDependencyColumns ? 10 : 8) + '" class="muted">No recurrence candidates found.</td></tr>';
			}
			return rows.map(function(item, idx){
				var risk = String(item.risk_level || 'None');
				var cls = risk === 'High' ? 'row-risk-high' : (risk === 'Medium' ? 'row-risk-medium' : (risk === 'Low' ? 'row-risk-low' : ''));
				var preview = String(item.ticket_ids_preview || '-');
				var allIds = String(item.ticket_ids_all || preview);
				var moreCount = Number(item.ticket_more_count || 0);
				var ticketCell = preview;
				if (moreCount > 0) {
					var expandedId = 'tickets-expanded-' + idx;
					var toggleId = 'tickets-toggle-' + idx;
					ticketCell += ' <a href="#" id="' + toggleId + '" onclick="return toggleTicketIds(\'' + expandedId + '\', this)">+' + String(moreCount) + ' more</a>';
					ticketCell += '<div id="' + expandedId + '" style="display:none;margin-top:4px;font-size:11px;color:#51607a;">' + allIds + '</div>';
				}
				var relationCell = showDependencyColumns ? '<td><span class="pill">' + (item.relationship_type || '-') + '</span></td><td>' + String(item.dependency_distance || '-') + '</td>' : '';
				var originalIdx = predictiveCandidatesAll.indexOf(item);
				var mlScore = predictiveMlScores[String(originalIdx)];
				var aiRiskCell;
				if (!mlScore) {
					aiRiskCell = '<td><span class="muted">-</span></td>';
				} else {
					var score = Number(mlScore.risk_score || 0);
					var pct = Math.round(score * 100);
					var badgeCls, badgeLabel;
					if (mlScore.predicted_label === 1) {
						badgeCls = 'badge-risk-high'; badgeLabel = 'High';
					} else if (score >= 0.30) {
						badgeCls = 'badge-risk-medium'; badgeLabel = 'Med';
					} else {
						badgeCls = 'badge-risk-low'; badgeLabel = 'Low';
					}
					aiRiskCell = '<td><span class="badge-risk ' + badgeCls + '">' + badgeLabel + '</span></td>';
				}
				return '<tr class="' + cls + '"><td>' + String(item.impact_score || '-') + '</td><td>' + String(item.ticket_count || 0) + '</td><td>' + ticketCell + '</td>' + relationCell + '<td>' + (item.module_name || '-') + '</td><td>' + (item.modified_functionality || '-') + '</td><td>' + (item.impact_reason || '-') + '</td><td style="display:none">' + risk + '</td>' + aiRiskCell + '</tr>';
			}).join('');
		}

		function getPredictiveFilterValues() {
			var moduleFilter = String((document.getElementById('pfModuleFilter') || {}).value || '').trim().toLowerCase();
			var functionFilter = String((document.getElementById('pfFunctionFilter') || {}).value || '').trim().toLowerCase();
			var aiRiskFilter = String((document.getElementById('pfAiRiskFilter') || {}).value || '').trim().toLowerCase();
			return {
				moduleFilter: moduleFilter,
				functionFilter: functionFilter,
				aiRiskFilter: aiRiskFilter,
			};
		}

		function getFilteredPredictiveCandidates() {
			if (!Array.isArray(predictiveCandidatesAll) || !predictiveCandidatesAll.length) {
				return [];
			}
			var filters = getPredictiveFilterValues();
			return predictiveCandidatesAll.filter(function(item, idx) {
				var moduleName = String(item.module_name || '').trim().toLowerCase();
				var modifiedFunction = String(item.modified_functionality || '').trim().toLowerCase();
				if (filters.moduleFilter && moduleName !== filters.moduleFilter) {
					return false;
				}
				if (filters.functionFilter && modifiedFunction !== filters.functionFilter) {
					return false;
				}
				if (filters.aiRiskFilter) {
					var mlScore = predictiveMlScores[String(idx)];
					var aiRiskLevel = 'low';
					if (mlScore) {
						var score = Number(mlScore.risk_score || 0);
						var label = Number(mlScore.predicted_label || 0);
						if (label === 1) { aiRiskLevel = 'high'; }
						else if (score >= 0.30) { aiRiskLevel = 'medium'; }
					}
					if (aiRiskLevel !== filters.aiRiskFilter) { return false; }
				}
				return true;
			});
		}

		function repopulatePredictiveFilterDropdowns() {
			var moduleEl = document.getElementById('pfModuleFilter');
			var functionEl = document.getElementById('pfFunctionFilter');
			if (!moduleEl || !functionEl) {
				return;
			}

			var prevModule = String(moduleEl.value || '');
			var prevFunction = String(functionEl.value || '');

			var moduleNames = Array.from(new Set(
				predictiveCandidatesAll
					.map(function(item) { return String(item.module_name || '').trim(); })
					.filter(function(value) { return !!value; })
			)).sort(function(a, b) { return a.localeCompare(b); });

			moduleEl.innerHTML = '<option value="">All Modules</option>' + moduleNames.map(function(name) {
				return '<option value="' + name.replace(/"/g, '&quot;') + '">' + name + '</option>';
			}).join('');

			if (prevModule && moduleNames.indexOf(prevModule) !== -1) {
				moduleEl.value = prevModule;
			} else {
				moduleEl.value = '';
			}

			repopulatePredictiveFunctionDropdown(prevFunction);
		}

		function repopulatePredictiveFunctionDropdown(previousValue) {
			var moduleEl = document.getElementById('pfModuleFilter');
			var functionEl = document.getElementById('pfFunctionFilter');
			if (!moduleEl || !functionEl) {
				return;
			}

			var selectedModule = String(moduleEl.value || '').trim().toLowerCase();
			var functionNames = Array.from(new Set(
				predictiveCandidatesAll
					.filter(function(item) {
						if (!selectedModule) {
							return true;
						}
						var moduleName = String(item.module_name || '').trim().toLowerCase();
						return moduleName === selectedModule;
					})
					.map(function(item) { return String(item.modified_functionality || '').trim(); })
					.filter(function(value) { return !!value; })
			)).sort(function(a, b) { return a.localeCompare(b); });

			functionEl.innerHTML = '<option value="">All Functions</option>' + functionNames.map(function(name) {
				return '<option value="' + name.replace(/"/g, '&quot;') + '">' + name + '</option>';
			}).join('');

			if (previousValue && functionNames.indexOf(previousValue) !== -1) {
				functionEl.value = previousValue;
			} else {
				functionEl.value = '';
			}
		}

		function updatePredictiveStatusFromCurrentView() {
			var statusEl = document.getElementById('pfStatus');
			if (!statusEl || !predictiveLastRunMeta) {
				return;
			}
			var filteredRows = getFilteredPredictiveCandidates();
			var selectedLevels = getTopScoreLevelsValue();
			var shownRows = Math.min(selectedLevels, filteredRows.length || 0);
			var moduleLabel = String((document.getElementById('pfModuleFilter') || {}).value || '').trim() || 'All';
			var functionLabel = String((document.getElementById('pfFunctionFilter') || {}).value || '').trim() || 'All';
			if (predictiveLastRunType === 'e2e') {
				statusEl.textContent = 'Completed: ' + (predictiveLastRunMeta.ran_at || 'now') + ' | release: ' + (predictiveLastRunMeta.release_name || 'MSP End-to-End Run') + ' | Viewing Levels: ' + String(selectedLevels) + ' | Available Levels: ' + String(filteredRows.length || 0) + ' | Returned Rows: ' + String(shownRows) + ' | Module: ' + moduleLabel + ' | Function: ' + functionLabel;
				return;
			}
			statusEl.textContent = 'Completed: ' + (predictiveLastRunMeta.ran_at || 'now') + ' | Match mode: ' + (predictiveLastRunMeta.match_mode || 'strict') + ' | Viewing Levels: ' + String(selectedLevels) + ' | Available Levels: ' + String(filteredRows.length || 0) + ' | Returned Rows: ' + String(shownRows) + ' | Module: ' + moduleLabel + ' | Function: ' + functionLabel;
		}

		function applyPredictiveTopLevels() {
			if (!Array.isArray(predictiveCandidatesAll) || !predictiveCandidatesAll.length) {
				document.getElementById('pfCandidateRows').innerHTML = renderCandidateRows([]);
				document.getElementById('kpiTotalTickets').textContent = '0';
				document.getElementById('kpiLowRisk').textContent = '0';
				document.getElementById('kpiMediumRisk').textContent = '0';
				document.getElementById('kpiHighRisk').textContent = '0';
				document.getElementById('pfPagination').style.display = 'none';
				updatePredictiveStatusFromCurrentView();
				return;
			}
			var selectedLevels = getTopScoreLevelsValue();
			var filteredRows = getFilteredPredictiveCandidates();
			var rowsToRender = filteredRows.slice(0, selectedLevels);

			// Pagination
			var totalRows = rowsToRender.length;
			var totalPages = Math.max(1, Math.ceil(totalRows / predictivePageSize));
			if (predictiveCurrentPage >= totalPages) { predictiveCurrentPage = totalPages - 1; }
			if (predictiveCurrentPage < 0) { predictiveCurrentPage = 0; }
			var pageStart = predictiveCurrentPage * predictivePageSize;
			var pageEnd = Math.min(pageStart + predictivePageSize, totalRows);
			var pageRows = rowsToRender.slice(pageStart, pageEnd);

			document.getElementById('pfCandidateRows').innerHTML = renderCandidateRows(pageRows);

			// Pagination controls
			var paginationEl = document.getElementById('pfPagination');
			paginationEl.style.display = totalRows > predictivePageSize ? 'flex' : 'none';
			document.getElementById('pfPageInfo').textContent = 'Page ' + (predictiveCurrentPage + 1) + ' of ' + totalPages + '  (' + totalRows + ' rows)';
			document.getElementById('pfPagePrev').disabled = predictiveCurrentPage === 0;
			document.getElementById('pfPageNext').disabled = predictiveCurrentPage >= totalPages - 1;

			// KPI calculations across ALL filtered rows (not just current page)
			var totalTickets = 0, lowRiskTickets = 0, mediumRiskTickets = 0, highRiskTickets = 0;
			rowsToRender.forEach(function(row) {
				var ticketCount = Number(row.ticket_count || 0);
				totalTickets += ticketCount;
				var candidateIdx = predictiveCandidatesAll.indexOf(row);
				if (candidateIdx >= 0 && predictiveMlScores[String(candidateIdx)]) {
					var score = Number(predictiveMlScores[String(candidateIdx)].risk_score || 0);
					var label = Number(predictiveMlScores[String(candidateIdx)].predicted_label || 0);
					if (label === 1) {
						highRiskTickets += ticketCount;
					} else if (score >= 0.30 && score < 0.42) {
						mediumRiskTickets += ticketCount;
					} else if (score < 0.30) {
						lowRiskTickets += ticketCount;
					}
				}
			});
			document.getElementById('kpiTotalTickets').textContent = String(totalTickets);
			document.getElementById('kpiLowRisk').textContent = String(lowRiskTickets);
			document.getElementById('kpiMediumRisk').textContent = String(mediumRiskTickets);
			document.getElementById('kpiHighRisk').textContent = String(highRiskTickets);
			updatePredictiveStatusFromCurrentView();
		}

		function changePredictivePage(delta) {
			predictiveCurrentPage += delta;
			applyPredictiveTopLevels();
		}

		function downloadCandidatesCSV() {
			var selectedLevels = getTopScoreLevelsValue();
			var filteredRows = getFilteredPredictiveCandidates().slice(0, selectedLevels);
			if (!filteredRows.length) { return; }
			var headers = ['Score','Tickets','Ticket IDs','Module','Modified Function','Relationship','Dependency Hop','Reason','AI Risk'];
			var lines = [headers.join(',')];
			filteredRows.forEach(function(row) {
				var originalIdx = predictiveCandidatesAll.indexOf(row);
				var mlScore = predictiveMlScores[String(originalIdx)];
				var aiRisk = '-';
				if (mlScore) {
					var score = Number(mlScore.risk_score || 0);
					aiRisk = mlScore.predicted_label === 1 ? 'High' : (score >= 0.30 ? 'Med' : 'Low');
				}
				var cols = [
					row.impact_score || '',
					row.ticket_count || '',
					'"' + (row.ticket_ids_all || row.ticket_ids_preview || '').replace(/"/g, '""') + '"',
					'"' + (row.module_name || '').replace(/"/g, '""') + '"',
					'"' + (row.modified_functionality || '').replace(/"/g, '""') + '"',
					row.relationship_type || '',
					row.dependency_distance || '',
					'"' + (row.impact_reason || '').replace(/"/g, '""') + '"',
					aiRisk
				];
				lines.push(cols.join(','));
			});
			var blob = new Blob([lines.join('\n')], { type: 'text/csv' });
			var url = URL.createObjectURL(blob);
			var a = document.createElement('a');
			a.href = url;
			a.download = 'recurrence_candidates_summary.csv';
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			URL.revokeObjectURL(url);
		}

		async function fetchAndMergeMLScores(candidates) {
			if (!Array.isArray(candidates) || !candidates.length) return;
			var payload = candidates.map(function(item, idx) {
				return {
					candidate_idx: idx,
					ticket_ids: String(item.ticket_ids_all || item.ticket_ids_preview || ''),
					module_name: item.module_name || null,
					modified_functionality: item.modified_functionality || null,
					customer_priority: (item.risk_level === 'High') ? 'high' : (item.risk_level === 'Medium') ? 'medium' : 'low'
				};
			});
			try {
				var res = await fetch('/api/predictive/ml-score-candidates', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ candidates: payload })
				});
				if (!res.ok) return;
				var data = await res.json();
				var newScores = {};
				(data.scores || []).forEach(function(s) {
					newScores[String(s.candidate_idx)] = {
						risk_score: s.max_risk_score,
						predicted_label: s.predicted_label,
						threshold_used: s.threshold_used,
						model_version: s.model_version
					};
				});
				predictiveMlScores = newScores;
				var lowRiskTickets = 0, mediumRiskTickets = 0, highRiskTickets = 0;
				Object.keys(newScores).forEach(function(key) {
					var idx = parseInt(key);
					var candidate = predictiveCandidatesAll[idx];
					if (!candidate) return;
					var ticketCount = Number(candidate.ticket_count || 0);
					var score = Number(newScores[key].risk_score || 0);
					var label = Number(newScores[key].predicted_label || 0);
					if (label === 1) {
						highRiskTickets += ticketCount;
					} else if (score >= 0.30 && score < 0.42) {
						mediumRiskTickets += ticketCount;
					} else if (score < 0.30) {
						lowRiskTickets += ticketCount;
					}
				});
				document.getElementById('kpiLowRisk').textContent = String(lowRiskTickets);
				document.getElementById('kpiMediumRisk').textContent = String(mediumRiskTickets);
				document.getElementById('kpiHighRisk').textContent = String(highRiskTickets);
				applyPredictiveTopLevels();
			} catch (err) {
				console.warn('ML scoring unavailable:', err);
			}
		}

		function toggleTicketIds(containerId, linkEl) {
			var el = document.getElementById(containerId);
			if (!el) {
				return false;
			}
			var isHidden = (el.style.display === 'none' || !el.style.display);
			if (isHidden) {
				el.style.display = 'block';
				if (linkEl) { linkEl.textContent = 'show less'; }
			} else {
				el.style.display = 'none';
				if (linkEl) {
					var text = linkEl.textContent || '';
					if (text.toLowerCase() === 'show less') {
						var hiddenCount = (el.textContent || '').split(';').length;
						var previewCount = 5;
						var more = Math.max(0, hiddenCount - previewCount);
						linkEl.textContent = '+' + String(more) + ' more';
					}
				}
			}
			return false;
		}

		async function runPredictiveDefaults() {
			var statusEl = document.getElementById('pfStatus');
			var runBtn = document.getElementById('pfRunBtn');
			var runE2EBtn = document.getElementById('pfRunE2EBtn');
			statusEl.textContent = 'Running default predictive flow...';
			runBtn.disabled = true;
			if (runE2EBtn) { runE2EBtn.disabled = true; }

			try {
				var formData = new FormData();
				var topLevels = getTopScoreLevelsValue();
				formData.append('release_name', document.getElementById('pfRelease').value || 'MSP Default Run');
				formData.append('days', document.getElementById('pfDays').value || '180');
				formData.append('top_score_levels', String(topLevels));
				formData.append('work_item_scope', document.getElementById('pfScope').value || 'Bug,Customer Ticket');
				formData.append('priority_scope', document.getElementById('pfPriority').value || 'On Fire,Urgent,High');
				formData.append('match_mode', document.getElementById('pfMatch').value || 'strict');
				formData.append('include_dependencies', String(document.getElementById('pfDeps').value === 'true'));

				var modifiedFile = document.getElementById('pfModifiedFile').files[0];
				if (modifiedFile) {
					formData.append('modified_functions_csv', modifiedFile);
				}
				var dependencyFile = document.getElementById('pfDependencyFile').files[0];
				if (dependencyFile) {
					formData.append('dependency_metrics_csv', dependencyFile);
				}

				var res = await fetch('/api/predictive/run-defaults', {
					method: 'POST',
					body: formData,
				});
				var data = await res.json();
				if (!res.ok) {
					throw new Error(data.detail || 'Predictive flow failed');
				}

				var cards = data.cards || {};
				document.getElementById('kpiModules').textContent = String(cards.total_modules || 0);
				document.getElementById('kpiFuncs').textContent = String(cards.total_functionalities || 0);
			document.getElementById('kpiLowRisk').textContent = '0';

				document.getElementById('pfTopModules').innerHTML = renderModuleBars(data.top_modules || []);
				document.getElementById('pfTopFuncs').innerHTML = renderFuncBars(data.top_functionalities || []);

				var runMeta = data.run_meta || {};
				document.getElementById('pfTopLevels').value = String(Number(runMeta.top_score_levels || topLevels));
				predictiveCandidatesAll = Array.isArray(data.recurrence_candidates_all) ? data.recurrence_candidates_all : (data.recurrence_candidates || []);
				predictiveLastRunMeta = runMeta;
				predictiveLastRunType = 'default';
				predictiveMlScores = {};
				repopulatePredictiveFilterDropdowns();
				applyPredictiveTopLevels();
				fetchAndMergeMLScores(predictiveCandidatesAll);
				var outputs = data.outputs || {};
				document.getElementById('pfOutputHint').textContent = 'Output CSVs: ' + [outputs.recurrence_csv, outputs.module_summary_csv, outputs.functionality_summary_csv].filter(Boolean).join(' | ');
			} catch (err) {
				statusEl.textContent = 'Error: ' + String(err);
			} finally {
				runBtn.disabled = false;
				if (runE2EBtn) { runE2EBtn.disabled = false; }
			}
		}

		async function runPredictiveE2E() {
			var statusEl = document.getElementById('pfStatus');
			var runBtn = document.getElementById('pfRunBtn');
			var runE2EBtn = document.getElementById('pfRunE2EBtn');
			var mspInput = document.getElementById('pfMspFile');
			var mspFile = mspInput.files[0];
			if (!mspFile) {
				statusEl.textContent = 'Please select MSP workbook (.xlsx/.xlsm/.xls). Opening file picker...';
				if (mspInput) {
					mspInput.click();
				}
				return;
			}

			statusEl.textContent = '[Running] Initializing end-to-end pipeline...';
			runBtn.disabled = true;
			runE2EBtn.disabled = true;
			setPredictiveBusyCursor(true);
			var runId = 'e2e-' + String(Date.now()) + '-' + String(Math.floor(Math.random() * 100000));
			var statusPollTimer = null;

			try {
				var formData = new FormData();
				var topLevels = getTopScoreLevelsValue();
				formData.append('release_name', document.getElementById('pfRelease').value || 'MSP End-to-End Run');
				formData.append('days', document.getElementById('pfDays').value || '180');
				formData.append('top_score_levels', String(topLevels));
				formData.append('work_item_scope', document.getElementById('pfScope').value || 'Bug,Customer Ticket');
				formData.append('priority_scope', document.getElementById('pfPriority').value || 'On Fire,Urgent,High');
				formData.append('match_mode', document.getElementById('pfMatch').value || 'strict');
				formData.append('include_dependencies', String(document.getElementById('pfDeps').value === 'true'));
				formData.append('refresh_ado_export', String(document.getElementById('pfRefreshAdo').value === 'true'));
				formData.append('apply_query_update', String(document.getElementById('pfApplyQuery').value === 'true'));
				formData.append('msp_sheet_name', document.getElementById('pfMspSheet').value || '6.0_1-AprilMSP_2026');
				formData.append('msp_target_category', document.getElementById('pfMspCategory').value || 'Engineering Improvement Initiatives- NBL(I)');
				formData.append('run_id', runId);
				formData.append('msp_workbook', mspFile);

				var modifiedFile = document.getElementById('pfModifiedFile').files[0];
				if (modifiedFile) {
					formData.append('modified_functions_csv', modifiedFile);
				}
				var dependencyFile = document.getElementById('pfDependencyFile').files[0];
				if (dependencyFile) {
					formData.append('dependency_metrics_csv', dependencyFile);
				}

				statusPollTimer = startRunStatusPolling(runId, statusEl);

				var res = await fetch('/api/predictive/run-e2e', {
					method: 'POST',
					body: formData,
				});
				var data = await res.json();
				if (!res.ok) {
					throw new Error(data.detail || 'End-to-end predictive flow failed');
				}

				var cards = data.cards || {};
				document.getElementById('kpiModules').textContent = String(cards.total_modules || 0);
				document.getElementById('kpiFuncs').textContent = String(cards.total_functionalities || 0);
				document.getElementById('kpiLowRisk').textContent = '0';
				document.getElementById('kpiMediumRisk').textContent = '0';
				document.getElementById('kpiHighRisk').textContent = String(cards.high_risk_rows || 0);
				document.getElementById('kpiTotalTickets').textContent = '0';

				document.getElementById('pfTopModules').innerHTML = renderModuleBars(data.top_modules || []);
				document.getElementById('pfTopFuncs').innerHTML = renderFuncBars(data.top_functionalities || []);

				var runMeta = data.run_meta || {};
				document.getElementById('pfTopLevels').value = String(Number(runMeta.top_score_levels || topLevels));
				predictiveCandidatesAll = Array.isArray(data.recurrence_candidates_all) ? data.recurrence_candidates_all : (data.recurrence_candidates || []);
				predictiveLastRunMeta = runMeta;
				predictiveLastRunType = 'e2e';
				predictiveMlScores = {};
				repopulatePredictiveFilterDropdowns();
				applyPredictiveTopLevels();
				fetchAndMergeMLScores(predictiveCandidatesAll);
				var outputs = data.outputs || {};
				document.getElementById('pfOutputHint').textContent = 'Output CSVs: ' + [
					outputs.ticket_export_csv,
					outputs.feature_module_csv,
					outputs.module_summary_csv,
					outputs.functionality_summary_csv,
					outputs.recurrence_csv
				].filter(Boolean).join(' | ');
			} catch (err) {
				statusEl.textContent = 'Error: ' + String(err);
			} finally {
				if (statusPollTimer) {
					clearInterval(statusPollTimer);
				}
				setPredictiveBusyCursor(false);
				runBtn.disabled = false;
				runE2EBtn.disabled = false;
			}
		}
		/*
		function parseTicketIds() {
			var raw = (document.getElementById('ticketInput').value || '').trim();
			if (!raw) {
				return [];
			}
			return raw.split(',').map(function(part){ return part.trim(); }).filter(Boolean);
		}
		*/		
		function parseNumericWorkItemIds(ticketIds) {
			return (ticketIds || []).map(function(ticket) {
				var match = String(ticket).match(/(\d{3,9})/);
				return match ? Number(match[1]) : null;
			}).filter(function(value) {
				return Number.isInteger(value) && value > 0;
			});
		}

		function setKeywordResult(text) {
			var host = document.getElementById('keywordResult');
			host.innerHTML = '';
			renderMessageBody(host, text || 'No output generated.');
		}

		function setKeywordLoading(isLoading) {
			['btnGenerate', 'btnMissing', 'btnNegative'].forEach(function(id){
				var node = document.getElementById(id);
				node.disabled = isLoading;
			});
			document.getElementById('keywordView').classList.toggle('loading', isLoading);
		}

		function setKeywordPresidioStatus(isProtected, sanitizationMode, source) {
			var presidio = document.getElementById('keywordPresidio');
			var mode = document.getElementById('keywordSanitizationMode');
			var src = document.getElementById('keywordSource');

			presidio.classList.remove('ok', 'warn');
			if (isProtected === true) {
				presidio.textContent = 'Enabled';
				presidio.classList.add('ok');
			} else if (isProtected === false) {
				presidio.textContent = 'Disabled';
				presidio.classList.add('warn');
			} else {
				presidio.textContent = 'Unknown';
			}

			mode.textContent = sanitizationMode || 'Unknown';
			src.textContent = source || 'Unknown';
		}

		async function fetchCoverageForMultipleIds(workItemIds) {
			var uniqueIds = Array.from(new Set((workItemIds || []).filter(function(id) {
				return Number.isInteger(id) && id > 0;
			}))).slice(0, 20);
			if (!uniqueIds.length) {
				return null;
			}

			var responses = await Promise.all(uniqueIds.map(async function(id) {
				var res = await fetch('/api/coverage-gaps?work_item_id=' + encodeURIComponent(id));
				return res.json();
			}));

			var totalStories = 0;
			var totalCovered = 0;
			var totalNoTc = 0;
			var presidioSeen = null;
			var sanitizationModes = [];
			var perItemLines = [];

			responses.forEach(function(itemRes, index) {
				var wi = itemRes.work_item || {};
				var id = wi.id || uniqueIds[index];
				var title = wi.title || 'Unknown work item';
				var coveragePercent = itemRes.coverage_percent || 0;
				var noTc = itemRes.no_test_cases || 0;
				var covered = itemRes.covered || 0;

				totalStories += itemRes.total_stories || 0;
				totalCovered += covered;
				totalNoTc += noTc;

				if (typeof itemRes.presidio_protected === 'boolean') {
					presidioSeen = presidioSeen === null ? itemRes.presidio_protected : (presidioSeen && itemRes.presidio_protected);
				}
				if (itemRes.sanitization_mode) {
					sanitizationModes.push(itemRes.sanitization_mode);
				}

				perItemLines.push(
					'- WI ' + id + ': ' + title + ' | Covered=' + covered + ' | No Test Cases=' + noTc + ' | Coverage=' + coveragePercent + '%'
				);
			});

			var overallCoverage = totalStories ? Math.round((totalCovered / totalStories) * 100) : 0;
			return {
				presidio_protected: presidioSeen,
				sanitization_mode: sanitizationModes.length ? sanitizationModes[0] : 'unknown',
				source: 'ado_live',
				total_stories: totalStories,
				covered: totalCovered,
				no_test_cases: totalNoTc,
				coverage_percent: overallCoverage,
				detail_lines: perItemLines,
			};
		}
		/*
		async function runKeywordAction(action) {
			var ids = parseTicketIds();
			var numericIds = parseNumericWorkItemIds(ids);
			if (!ids.length) {
				setKeywordResult('Please enter at least one ticket ID.');
				return;
			}

			setKeywordLoading(true);
			try {
				if (action === 'generate') {
					var hasSingleExactId = numericIds.length === 1;
					var prompt = hasSingleExactId
						? 'Generate comprehensive QA test cases for work item ID ' + numericIds[0] + '. Include positive, integration, and risk-focused scenarios. Use this exact work item context.'
						: 'Generate comprehensive QA test cases for ticket IDs: ' + ids.join(', ') + '. Include positive, integration, and risk-focused scenarios.';
					var genRes = await fetch('/api/generate-test-cases', {
						method: 'POST',
						headers: {'Content-Type': 'application/json'},
						body: JSON.stringify({
							feature_text: prompt,
							work_item_id: hasSingleExactId ? numericIds[0] : null,
							mode: 'live'
						})
					});
					var genData = await genRes.json();
					setKeywordPresidioStatus(
						genData.sanitized === true ? true : (typeof genData.presidio_protected === 'boolean' ? genData.presidio_protected : null),
						genData.sanitization_mode || (genData.sanitized ? 'sanitized' : null),
						genData.source || 'generate-test-cases'
					);
					setKeywordResult(genData.assistant_message || genData.summary || JSON.stringify(genData, null, 2));
					return;
				}

				if (action === 'missing') {
					var covData;
					if (numericIds.length > 1) {
						covData = await fetchCoverageForMultipleIds(numericIds);
						if (!covData) {
							setKeywordResult('Unable to retrieve coverage for the provided IDs.');
							return;
						}
					} else if (numericIds.length === 1) {
						var singleRes = await fetch('/api/coverage-gaps?work_item_id=' + encodeURIComponent(numericIds[0]));
						covData = await singleRes.json();
					} else {
						var moduleArg = ids.join(' ');
						var covRes = await fetch('/api/coverage-gaps?module=' + encodeURIComponent(moduleArg));
						covData = await covRes.json();
					}
					var summary = [];
					setKeywordPresidioStatus(covData.presidio_protected, covData.sanitization_mode, covData.source || 'coverage-gaps');
					summary.push('Coverage Summary');
					summary.push('Total Stories: ' + (covData.total_stories || 0));
					summary.push('Covered: ' + (covData.covered || 0));
					summary.push('No Test Cases: ' + (covData.no_test_cases || 0));
					summary.push('Coverage %: ' + (covData.coverage_percent || 0) + '%');
					if (covData.detail_lines && covData.detail_lines.length) {
						summary.push('');
						summary.push('Per Work Item');
						covData.detail_lines.forEach(function(line) { summary.push(line); });
					}
					if (covData.generated_coverage_gaps) {
						summary.push('');
						summary.push(covData.generated_coverage_gaps);
					}
					setKeywordResult(summary.join('\n'));
					return;
				}

				var negPrompt = 'Generate negative and edge test cases for these ticket IDs: ' + ids.join(', ') + '. Focus on validation, resilience, and failure handling.';
				var chatRes = await fetch('/api/chat', {
					method: 'POST',
					headers: {'Content-Type': 'application/json'},
					body: JSON.stringify({message: negPrompt})
				});
				var chatData = await chatRes.json();
				setKeywordPresidioStatus(
					typeof chatData.presidio_protected === 'boolean' ? chatData.presidio_protected : null,
					chatData.sanitization_mode || null,
					chatData.source || 'chat'
				);
				setKeywordResult(chatData.assistant_message || JSON.stringify(chatData, null, 2));
			} catch (err) {
				setKeywordPresidioStatus(null, null, 'error');
				setKeywordResult('Request failed: ' + String(err));
			} finally {
				setKeywordLoading(false);
			}
		}
		*/
		var CHAT_STORAGE_KEY = 'smartcare_chat_sessions_v1';
		var CHAT_CONTEXT_STORAGE_KEY = 'smartcare_chat_context_v1';
		var chatSessions = [];
		var currentChatId = null;
		var chatContextBySession = {};

		function newSessionTitle() {
			return 'New QA Chat';
		}

		function truncateSessionTitle(text) {
			var clean = (text || '').replace(/\s+/g, ' ').trim();
			if (!clean) {
				return newSessionTitle();
			}
			return clean.length > 46 ? clean.slice(0, 46) + '...' : clean;
		}

		function createChatSession(initialTitle) {
			return {
				id: String(Date.now()) + '-' + String(Math.floor(Math.random() * 100000)),
				title: initialTitle || newSessionTitle(),
				messages: [],
				createdAt: new Date().toISOString(),
				updatedAt: new Date().toISOString()
			};
		}

		function saveChatSessions() {
			try {
				localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(chatSessions));
			} catch (err) {
				console.warn('Unable to persist chat sessions:', err);
			}
		}

		function loadChatSessions() {
			try {
				var raw = localStorage.getItem(CHAT_STORAGE_KEY);
				if (!raw) {
					return [];
				}
				var parsed = JSON.parse(raw);
				if (!Array.isArray(parsed)) {
					return [];
				}
				return parsed.map(function(session) {
					return {
						id: session.id,
						title: session.title || newSessionTitle(),
						messages: Array.isArray(session.messages) ? session.messages : [],
						createdAt: session.createdAt || new Date().toISOString(),
						updatedAt: session.updatedAt || new Date().toISOString()
					};
				});
			} catch (err) {
				console.warn('Unable to load chat sessions:', err);
				return [];
			}
		}

		function saveChatContextState() {
			try {
				localStorage.setItem(CHAT_CONTEXT_STORAGE_KEY, JSON.stringify(chatContextBySession));
			} catch (err) {
				console.warn('Unable to persist chat context state:', err);
			}
		}

		function loadChatContextState() {
			try {
				var raw = localStorage.getItem(CHAT_CONTEXT_STORAGE_KEY);
				if (!raw) {
					return {};
				}
				var parsed = JSON.parse(raw);
				if (!parsed || typeof parsed !== 'object') {
					return {};
				}
				return parsed;
			} catch (err) {
				console.warn('Unable to load chat context state:', err);
				return {};
			}
		}

		function getCurrentSession() {
			return chatSessions.find(function(session) { return session.id === currentChatId; }) || null;
		}

		function renderChatHistory() {
			var host = document.getElementById('chatHistory');
			var searchEl = document.getElementById('chatHistorySearch');
			var query = searchEl ? String(searchEl.value || '').trim().toLowerCase() : '';
			if (!host) {
				return;
			}
			host.innerHTML = '';
			if (!chatSessions.length) {
				var empty = document.createElement('div');
				empty.className = 'chat-item';
				empty.style.opacity = '0.75';
				empty.textContent = 'No chat sessions yet.';
				host.appendChild(empty);
				return;
			}

			var sortedSessions = chatSessions.slice().sort(function(a, b) {
				return String(b.updatedAt).localeCompare(String(a.updatedAt));
			});

			var visibleSessions = sortedSessions.filter(function(session) {
				if (!query) {
					return true;
				}
				var title = String(session.title || newSessionTitle()).toLowerCase();
				return title.indexOf(query) !== -1;
			});

			if (!visibleSessions.length) {
				var noMatch = document.createElement('div');
				noMatch.className = 'chat-item';
				noMatch.style.opacity = '0.75';
				noMatch.textContent = 'No matching chats.';
				host.appendChild(noMatch);
				return;
			}

			visibleSessions.forEach(function(session) {
				var item = document.createElement('div');
				item.className = 'chat-item' + (session.id === currentChatId ? ' active' : '');
				item.textContent = session.title || newSessionTitle();
				item.title = session.title || newSessionTitle();
				item.onclick = function() { selectChatSession(session.id); };
				host.appendChild(item);
			});
		}

		function renderChatFeed() {
			var feed = document.getElementById('chatFeed');
			feed.innerHTML = '';
			var session = getCurrentSession();
			if (!session) {
				appendMessageToFeed('assistant', 'Select a chat from History or click + New Chat to begin.');
				renderAttachedFiles();
				return;
			}
			if (!session.messages.length) {
				appendMessageToFeed('assistant', 'New chat started. Share your QA request and I will help with test design, coverage gaps, and automation guidance.');
				renderAttachedFiles();
				return;
			}
			session.messages.forEach(function(msg) {
				appendMessageToFeed(msg.role, msg.text, msg.meta || null);
			});
			renderAttachedFiles();
		}

		function getCurrentContextState() {
			if (!currentChatId) {
				return null;
			}
			if (!chatContextBySession[currentChatId]) {
				chatContextBySession[currentChatId] = {
					sessionId: null,
					uploadedDocuments: []
				};
				saveChatContextState();
			}
			return chatContextBySession[currentChatId];
		}

		function renderAttachedFiles() {
			var host = document.getElementById('attachedFiles');
			var clearBtn = document.getElementById('clearAttachedBtn');
			if (!host) {
				return;
			}
			var state = getCurrentContextState();
			if (!state || !state.uploadedDocuments || !state.uploadedDocuments.length) {
				host.innerHTML = '';
				if (clearBtn) {
					clearBtn.style.display = 'none';
				}
				return;
			}
			if (clearBtn) {
				clearBtn.style.display = 'inline-block';
			}

			host.innerHTML = '';
			var label = document.createElement('span');
			label.textContent = 'Attached: ';
			host.appendChild(label);

			state.uploadedDocuments.forEach(function(doc) {
				var chip = document.createElement('span');
				chip.style.display = 'inline-flex';
				chip.style.alignItems = 'center';
				chip.style.gap = '6px';
				chip.style.margin = '0 6px 6px 0';
				chip.style.padding = '2px 8px';
				chip.style.border = '1px solid #d7c5e7';
				chip.style.borderRadius = '999px';
				chip.style.background = '#fff';

				var text = document.createElement('span');
				text.textContent = doc.filename || 'uploaded_file';
				chip.appendChild(text);

				var closeBtn = document.createElement('button');
				closeBtn.type = 'button';
				closeBtn.textContent = '×';
				closeBtn.title = 'Remove from chat context';
				closeBtn.style.border = 'none';
				closeBtn.style.background = 'transparent';
				closeBtn.style.cursor = 'pointer';
				closeBtn.style.fontSize = '14px';
				closeBtn.style.lineHeight = '1';
				closeBtn.onclick = function() {
					removeAttachedDocument(String(doc.document_id || ''));
				};
				chip.appendChild(closeBtn);
				host.appendChild(chip);
			});
		}

		async function removeAttachedDocument(documentId) {
			if (!documentId) {
				return;
			}
			var state = getCurrentContextState();
			if (!state) {
				return;
			}
			if (!confirm('Remove this file from chat context?')) {
				return;
			}

			try {
				if (state.sessionId) {
					var res = await fetch('/api/context/' + encodeURIComponent(state.sessionId) + '/' + encodeURIComponent(documentId), {
						method: 'DELETE'
					});
					if (!res.ok) {
						var data = await res.json();
						throw new Error(data && data.detail ? data.detail : 'Delete failed');
					}
				}

				state.uploadedDocuments = (state.uploadedDocuments || []).filter(function(doc) {
					return String(doc.document_id || '') !== documentId;
				});
				saveChatContextState();
				renderAttachedFiles();
			} catch (err) {
				addChatMessage('assistant', 'Could not remove attached file: ' + String(err), {
					source: 'context_upload_error',
					grounded: false,
					presidio_check: 'unknown',
					sanitization_mode: 'unknown'
				});
			}
		}

		async function clearAllAttachedFiles() {
			var state = getCurrentContextState();
			if (!state || !state.uploadedDocuments || !state.uploadedDocuments.length) {
				return;
			}
			if (!confirm('Remove all attached files from this chat context?')) {
				return;
			}

			try {
				if (state.sessionId) {
					var res = await fetch('/api/context/' + encodeURIComponent(state.sessionId), {
						method: 'DELETE'
					});
					if (!res.ok) {
						var data = await res.json();
						throw new Error(data && data.detail ? data.detail : 'Clear failed');
					}
				}

				state.uploadedDocuments = [];
				state.sessionId = null;
				saveChatContextState();
				renderAttachedFiles();
			} catch (err) {
				addChatMessage('assistant', 'Could not clear attachments: ' + String(err), {
					source: 'context_upload_error',
					grounded: false,
					presidio_check: 'unknown',
					sanitization_mode: 'unknown'
				});
			}
		}

		function triggerContextUpload() {
			var picker = document.getElementById('contextFileInput');
			if (!picker) {
				return;
			}
			picker.click();
		}

		async function handleContextFilesSelected(event) {
			var picker = event && event.target ? event.target : document.getElementById('contextFileInput');
			if (!picker || !picker.files || !picker.files.length) {
				return;
			}

			var session = getCurrentSession();
			if (!session) {
				startNewChatSession();
				session = getCurrentSession();
			}

			var state = getCurrentContextState();
			if (!state) {
				picker.value = '';
				return;
			}

			var attachBtn = document.getElementById('attachBtn');
			if (attachBtn) {
				attachBtn.disabled = true;
				attachBtn.textContent = '...';
			}

			var formData = new FormData();
			Array.from(picker.files).forEach(function(file) {
				formData.append('files', file);
			});
			if (state.sessionId) {
				formData.append('session_id', state.sessionId);
			}

			try {
				var res = await fetch('/api/context/upload', {
					method: 'POST',
					body: formData
				});
				var data = await res.json();
				if (!res.ok) {
					throw new Error(data && data.detail ? data.detail : 'Upload failed');
				}

				state.sessionId = data.session_id || state.sessionId;
				var uploaded = Array.isArray(data.uploaded_documents) ? data.uploaded_documents : [];
				uploaded.forEach(function(doc) {
					state.uploadedDocuments.push(doc);
				});
				saveChatContextState();
				renderAttachedFiles();
				addChatMessage('assistant', 'Uploaded ' + uploaded.length + ' file(s) and attached them to this chat context.', {
					source: 'context_upload',
					grounded: false,
					presidio_check: 'n/a',
					sanitization_mode: 'n/a'
				});
			} catch (err) {
				addChatMessage('assistant', 'File upload failed: ' + String(err), {
					source: 'context_upload_error',
					grounded: false,
					presidio_check: 'unknown',
					sanitization_mode: 'unknown'
				});
			} finally {
				if (attachBtn) {
					attachBtn.disabled = false;
					attachBtn.textContent = '+';
				}
				picker.value = '';
			}
		}

		function selectChatSession(sessionId) {
			currentChatId = sessionId;
			renderChatHistory();
			renderChatFeed();
			showView('freetext');
		}

		function startNewChatSession() {
			var current = getCurrentSession();
			if (current && (!current.messages || current.messages.length === 0)) {
				showView('freetext');
				return;
			}

			var session = createChatSession(newSessionTitle());
			chatSessions.push(session);
			currentChatId = session.id;
			saveChatSessions();
			renderChatHistory();
			renderChatFeed();
			showView('freetext');
		}

		function upsertCurrentMessage(role, text, meta) {
			var session = getCurrentSession();
			if (!session) {
				session = createChatSession(newSessionTitle());
				chatSessions.push(session);
				currentChatId = session.id;
			}

			session.messages.push({
				role: role,
				text: text,
				meta: meta || null,
				at: new Date().toISOString()
			});

			if (role === 'user') {
				var nonDefaultTitle = session.title && session.title !== newSessionTitle();
				if (!nonDefaultTitle || session.messages.filter(function(msg) { return msg.role === 'user'; }).length === 1) {
					session.title = truncateSessionTitle(text);
				}
			}

			session.updatedAt = new Date().toISOString();
			saveChatSessions();
			renderChatHistory();
		}

		function buildChatMeta(meta) {
			if (!meta) {
				return [];
			}
			var pills = [];
			if (meta.source) {
				pills.push('Source: ' + meta.source);
			}
			if (meta.work_item_id) {
				pills.push('Work Item: ' + meta.work_item_id);
			}
			if (meta.selection_mode) {
				pills.push('Selection: ' + meta.selection_mode);
			}
			if (meta.presidio_check) {
				pills.push('Presidio: ' + meta.presidio_check);
			}
			if (meta.sanitization_mode) {
				pills.push('Mode: ' + meta.sanitization_mode);
			}
			if (typeof meta.uploaded_context_used === 'boolean') {
				pills.push('Upload Context: ' + (meta.uploaded_context_used ? 'Yes' : 'No'));
			}
			if (Array.isArray(meta.uploaded_sources) && meta.uploaded_sources.length) {
				pills.push('Sources: ' + meta.uploaded_sources.join(', '));
			}
			return pills;
		}

		function splitMarkdownRow(line) {
			var cells = line.split('|').map(function(cell) { return cell.trim(); });
			if (cells.length && cells[0] === '') {
				cells.shift();
			}
			if (cells.length && cells[cells.length - 1] === '') {
				cells.pop();
			}
			return cells;
		}

		function isMarkdownSeparatorRow(line) {
			var cells = splitMarkdownRow(line);
			if (!cells.length) {
				return false;
			}
			return cells.every(function(cell) {
				return /^:?-{3,}:?$/.test(cell);
			});
		}

		function isTableStart(lines, index) {
			if (index + 1 >= lines.length) {
				return false;
			}
			if (lines[index].indexOf('|') === -1) {
				return false;
			}
			return isMarkdownSeparatorRow(lines[index + 1]);
		}

		function appendTextBlock(host, text) {
			if (!text || !text.trim()) {
				return;
			}
			var block = document.createElement('div');
			block.className = 'chat-text-block';
			block.textContent = text;
			host.appendChild(block);
		}

		function appendMarkdownTable(host, lines, startIndex) {
			var headers = splitMarkdownRow(lines[startIndex]);
			var tableWrap = document.createElement('div');
			tableWrap.className = 'chat-markdown-table-wrap';
			var table = document.createElement('table');
			table.className = 'chat-markdown-table';
			var thead = document.createElement('thead');
			var headerRow = document.createElement('tr');
			headers.forEach(function(headerText) {
				var th = document.createElement('th');
				th.textContent = headerText || 'Column';
				headerRow.appendChild(th);
			});
			thead.appendChild(headerRow);
			table.appendChild(thead);

			var tbody = document.createElement('tbody');
			var cursor = startIndex + 2;
			while (cursor < lines.length && lines[cursor].indexOf('|') !== -1 && lines[cursor].trim() !== '') {
				var rowValues = splitMarkdownRow(lines[cursor]);
				var tr = document.createElement('tr');
				for (var i = 0; i < headers.length; i++) {
					var td = document.createElement('td');
					td.textContent = rowValues[i] || '';
					tr.appendChild(td);
				}
				tbody.appendChild(tr);
				cursor += 1;
			}
			table.appendChild(tbody);
			tableWrap.appendChild(table);
			host.appendChild(tableWrap);
			return cursor;
		}

		function renderMessageBody(host, text) {
			var raw = text || '';
			var lines = raw.split('\n');
			var index = 0;
			var pending = [];
			while (index < lines.length) {
				if (isTableStart(lines, index)) {
					appendTextBlock(host, pending.join('\n'));
					pending = [];
					index = appendMarkdownTable(host, lines, index);
					continue;
				}
				pending.push(lines[index]);
				index += 1;
			}
			appendTextBlock(host, pending.join('\n'));
		}

		function appendMessageToFeed(role, text, meta) {
			var feed = document.getElementById('chatFeed');
			var row = document.createElement('div');
			row.className = 'bubble';

			var avatar = document.createElement('div');
			avatar.className = 'avatar ' + role;
			avatar.textContent = role === 'user' ? 'You' : 'AI';

			var card = document.createElement('div');
			card.className = 'bubble-card';
			if (role === 'assistant') {
				var metaPills = buildChatMeta(meta);
				if (metaPills.length) {
					var metaRow = document.createElement('div');
					metaRow.className = 'chat-meta';
					metaPills.forEach(function(pillText) {
						var pill = document.createElement('span');
						pill.className = 'chat-meta-pill';
						pill.textContent = pillText;
						metaRow.appendChild(pill);
					});
					card.appendChild(metaRow);
				}
			}
			var body = document.createElement('div');
			renderMessageBody(body, text);
			card.appendChild(body);

			row.appendChild(avatar);
			row.appendChild(card);
			feed.appendChild(row);
			feed.scrollTop = feed.scrollHeight;
		}

		function addChatMessage(role, text, meta) {
			upsertCurrentMessage(role, text, meta || null);
			appendMessageToFeed(role, text, meta || null);
		}

		function resetChat() {
			startNewChatSession();
		}

		async function sendChat() {
			var box = document.getElementById('chatInput');
			var text = box.value.trim();
			if (!text) {
				return;
			}

			addChatMessage('user', text);
			box.value = '';

			var sendBtn = document.getElementById('sendBtn');
			sendBtn.disabled = true;
			sendBtn.textContent = 'Sending...';
			var contextState = getCurrentContextState();

			try {
				var res = await fetch('/api/chat', {
					method: 'POST',
					headers: {'Content-Type': 'application/json'},
					body: JSON.stringify({
						message: text,
						session_id: contextState && contextState.sessionId ? contextState.sessionId : null,
						include_uploaded_context: true
					})
				});
				var data = await res.json();
				addChatMessage('assistant', data.assistant_message || 'No response available.', data.response_meta || null);
			} catch (err) {
				addChatMessage('assistant', 'Request failed: ' + String(err), {
					source: 'error',
					presidio_check: 'unknown',
					sanitization_mode: 'unknown'
				});
			} finally {
				sendBtn.disabled = false;
				sendBtn.textContent = 'Send';
			}
		}

		document.getElementById('chatInput').addEventListener('keydown', function(event) {
			if (event.key === 'Enter' && !event.shiftKey) {
				event.preventDefault();
				sendChat();
			}
		});

		document.getElementById('pfTopLevels').addEventListener('input', function() {
			applyPredictiveTopLevels();
		});

		document.getElementById('pfModuleFilter').addEventListener('change', function() {
			predictiveCurrentPage = 0;
			repopulatePredictiveFunctionDropdown('');
			applyPredictiveTopLevels();
		});

		document.getElementById('pfFunctionFilter').addEventListener('change', function() {
			predictiveCurrentPage = 0;
			applyPredictiveTopLevels();
		});

		document.getElementById('pfAiRiskFilter').addEventListener('change', function() {
			predictiveCurrentPage = 0;
			applyPredictiveTopLevels();
		});

		showView('home');
		setKeywordPresidioStatus(null, null, 'not-run');
		chatSessions = loadChatSessions();
		chatContextBySession = loadChatContextState();
		currentChatId = null;
		renderChatHistory();
		renderChatFeed();
	</script>
	<footer class="watermark">Developed by Shivayogi</footer>
</body>
</html>"""


def main() -> None:
	uvicorn.run("src.chat_ui.app:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
	main()
