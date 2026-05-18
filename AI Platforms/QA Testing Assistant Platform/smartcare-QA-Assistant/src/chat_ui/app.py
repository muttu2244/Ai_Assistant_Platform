"""SmartCare QA Assistant web app + API.

Provides a local, demo-friendly UI with meaningful QA workflows:
- Test case / work-item lookup
- Coverage gaps
- Sprint summary
- AI pipeline actions
- Chat assistant with button + chat-based test case generation
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from typing import Literal

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from src.agents.ado_fetcher.ado_client import AdoClient
from src.agents.phi_sanitizer.presidio_analyzer import PhiSanitizer
from src.agents.ai_engine.foundry_client import FoundryClient
from src.common.config.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[2]
LANDING_LOGO_PATH = ROOT_DIR / "Streamline_Logo_Gradient.JPG"


@asynccontextmanager
async def lifespan(app: FastAPI):
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


class GenerateTestCasesRequest(BaseModel):
	feature_text: str = Field(min_length=3)
	work_item_id: int | None = None
	mode: Literal["mock", "live"] = "mock"


class ChatRequest(BaseModel):
	message: str = Field(min_length=1)
	work_item_id: int | None = None


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


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
	return _ui_html()


@app.get("/brand-logo")
async def brand_logo() -> FileResponse:
	return FileResponse(LANDING_LOGO_PATH)


@app.get("/api/status")
async def api_status() -> dict[str, object]:
	status = settings.feature_status()
	return {
		"app": settings.app_name,
		"environment": settings.app_env,
		"status": "running",
		"available_now": [
			feature_name
			for feature_name, feature_status in status.items()
			if feature_status["configured"]
		],
	}


@app.get("/health")
async def health() -> dict[str, str]:
	return {
		"status": "ok",
		"app": settings.app_name,
		"environment": settings.app_env,
	}


@app.get("/readiness")
async def readiness() -> dict[str, object]:
	status = settings.feature_status()
	return {
		"summary": {
			"basic_app": status["basic_app"]["configured"],
			"basic_ai_pipeline": status["basic_ai_pipeline"]["configured"],
			"full_pipeline": status["full_pipeline"]["configured"],
		},
		"features": status,
	}


@app.get("/capabilities")
async def capabilities() -> dict[str, object]:
	status = settings.feature_status()
	return {
		"working_now": {
			"app_boot": True,
			"health_endpoint": True,
			"config_readiness_report": True,
			"basic_ai_pipeline_ready": status["basic_ai_pipeline"]["configured"],
			"ado_fetch_ready": status["ado_fetch"]["configured"],
		},
		"needs_more_config": {
			feature_name: feature_status["missing"]
			for feature_name, feature_status in status.items()
			if feature_status["missing"]
		},
	}


@app.get("/api/test-case-lookup")
async def test_case_lookup(work_item_id: int = Query(..., gt=0)) -> dict[str, object]:
	if not _ado_is_configured():
		return {
			"source": "mock",
			"presidio_protected": True,
			"sanitization_mode": "mock",
			"work_item": {
				"id": work_item_id,
				"title": "Mock story for local demo",
				"description": "ADO is not configured locally. Showing sanitized mock data.",
				"state": "Active",
				"type": "User Story",
			},
		}

	client = AdoClient()
	phi = PhiSanitizer()
	try:
		item = await client.get_work_item(work_item_id)
		sanitized = await phi.sanitize_work_item(item)
		dump = sanitized.model_dump()
		dump["description"] = _strip_html(dump.get("description"))
		dump["acceptance_criteria"] = _strip_html(dump.get("acceptance_criteria"))
		return {
			"source": "ado_live",
			"presidio_protected": True,
			"sanitization_mode": phi.sanitization_mode,
			"work_item": dump,
		}
	except Exception as exc:
		return {
			"source": "fallback",
			"presidio_protected": True,
			"sanitization_mode": phi.sanitization_mode,
			"error": str(exc),
			"work_item": {
				"id": work_item_id,
				"title": "Fallback story",
				"description": "Live fetch failed, fallback data shown for demo continuity.",
				"state": "Unknown",
				"type": "User Story",
			},
		}


@app.get("/api/coverage-gaps")
async def coverage_gaps(module: str | None = None, work_item_id: int | None = None) -> dict[str, object]:
	if work_item_id is None and module and module.strip().isdigit():
		work_item_id = int(module.strip())

	if work_item_id is not None:
		if not _ado_is_configured():
			return _mock_coverage_for_item(work_item_id)

		client = AdoClient()
		phi = PhiSanitizer()
		try:
			item = await client.get_work_item(work_item_id)
			test_cases = await client.get_test_cases_for_story(work_item_id)
			sanitized_item = await phi.sanitize_work_item(item)
			dump = sanitized_item.model_dump()
			dump["description"] = _strip_html(dump.get("description"))
			dump["acceptance_criteria"] = _strip_html(dump.get("acceptance_criteria"))

			safe_test_cases = []
			for tc in test_cases[:25]:
				safe_tc = await phi.sanitize_test_case(tc)
				safe_test_cases.append({
					"id": safe_tc.id,
					"title": _strip_html(safe_tc.title),
					"state": safe_tc.state,
				})

			coverage_context_lines = [
				f"Work Item ID: {dump.get('id')}",
				f"Title: {dump.get('title')}",
				f"Type: {dump.get('work_item_type')}",
				f"State: {dump.get('state')}",
				f"Area Path: {dump.get('area_path')}",
				f"Iteration Path: {dump.get('iteration_path')}",
				"Description:\n" + (dump.get("description") or "Not provided"),
				"Acceptance Criteria:\n" + (dump.get("acceptance_criteria") or "Not provided"),
			]
			if safe_test_cases:
				coverage_context_lines.append(
					"Linked Test Cases:\n" + "\n".join(
						[f"- TC-{tc['id']}: {tc['title']} ({tc['state']})" for tc in safe_test_cases]
					)
				)
			else:
				coverage_context_lines.append("Linked Test Cases:\n- None linked")
			coverage_context = "\n\n".join(coverage_context_lines)

			analysis_prompt = (
				"Perform a test coverage gap analysis for this ADO work item. "
				"Return concise QA-focused output with these sections:\n"
				"1) Coverage Verdict (Covered/Partially Covered/Not Covered)\n"
				"2) Key Gaps (bullet list)\n"
				"3) Recommended Missing Test Scenarios (numbered)\n"
				"4) Risk if Not Tested (short)\n"
				"Base this only on provided context and linked test cases."
			)

			generated_coverage_gaps = ""
			try:
				llm = FoundryClient()
				generated_coverage_gaps = await llm.generate(
					user_prompt=analysis_prompt,
					feature="test_case",
					conversation_history=None,
					context=coverage_context,
				)
			except Exception as llm_exc:
				logger.warning("Coverage gap generation failed for work item %s: %s", work_item_id, llm_exc)
				generated_coverage_gaps = (
					"Coverage analysis generation is currently unavailable. "
					"Basic linked-test-case coverage metrics are shown above."
				)

			has_coverage = len(safe_test_cases) > 0
			return {
				"source": "ado_live",
				"scope": "work_item",
				"presidio_protected": True,
				"sanitization_mode": phi.sanitization_mode,
				"work_item": dump,
				"total_stories": 1,
				"covered": 1 if has_coverage else 0,
				"no_test_cases": 0 if has_coverage else 1,
				"coverage_percent": 100 if has_coverage else 0,
				"stories_without_tests": [] if has_coverage else [{
					"id": dump["id"],
					"title": dump["title"],
					"state": dump["state"],
				}],
				"linked_test_cases": safe_test_cases,
				"generated_coverage_gaps": generated_coverage_gaps,
			}
		except Exception as exc:
			return {
				"source": "fallback",
				"scope": "work_item",
				"presidio_protected": True,
				"sanitization_mode": phi.sanitization_mode,
				"error": str(exc),
				**_mock_coverage_for_item(work_item_id),
			}

	module_filter = module.strip() if module and module.strip() else None
	if not _ado_is_configured():
		return _mock_coverage_gaps(module_filter)

	import asyncio
	client = AdoClient()
	phi = PhiSanitizer()
	try:
		if module_filter:
			candidates = await asyncio.wait_for(client.search_work_items_by_text(module_filter, top_n=20), timeout=20.0)
			if not candidates:
				return {
					"module": module_filter,
					"scope": "module",
					"source": "ado_live",
					"presidio_protected": True,
					"sanitization_mode": phi.sanitization_mode,
					"total_stories": 0,
					"covered": 0,
					"no_test_cases": 0,
					"coverage_percent": 0,
					"stories_without_tests": [],
					"module_items": [],
					"generated_coverage_gaps": "No ADO work items were found for this module.",
				}

			safe_module_items = []
			safe_stories_without_tests = []
			covered_count = 0
			for item in candidates:
				sanitized_item = await phi.sanitize_work_item(item)
				test_cases = await client.get_test_cases_for_story(item.id)
				has_tests = len(test_cases) > 0
				if has_tests:
					covered_count += 1
				safe_module_items.append({
					"id": sanitized_item.id,
					"title": _strip_html(sanitized_item.title),
					"state": sanitized_item.state,
					"type": sanitized_item.work_item_type,
					"linked_test_cases": len(test_cases),
				})
				if not has_tests:
					safe_stories_without_tests.append({
						"id": sanitized_item.id,
						"title": _strip_html(sanitized_item.title),
						"state": sanitized_item.state,
					})

			total_items = len(safe_module_items)
			no_tc = len(safe_stories_without_tests)
			coverage_percent = int((covered_count / total_items) * 100) if total_items else 0

			coverage_context = "\n".join([
				f"Module: {module_filter}",
				f"Total Items Considered: {total_items}",
				f"Covered Items: {covered_count}",
				f"Items Without Linked Test Cases: {no_tc}",
				"Items:",
				*[
					f"- {row['id']}: {row['title']} ({row['type']}, {row['state']}) linked_test_cases={row['linked_test_cases']}"
					for row in safe_module_items[:20]
				],
			])

			generated_coverage_gaps = ""
			if no_tc > 0:
				try:
					llm = FoundryClient()
					generated_coverage_gaps = await llm.generate(
						user_prompt=(
							"Generate module-level coverage gap analysis and missing test recommendations. "
							"Return sections:\n"
							"1) Coverage Verdict\n"
							"2) Top Missing Areas\n"
							"3) Recommended New Test Cases by Work Item ID\n"
							"4) Suggested Priority (Critical/High/Medium)\n"
							"Focus only on items with zero linked test cases."
						),
						feature="test_case",
						conversation_history=None,
						context=coverage_context,
					)
				except Exception as llm_exc:
					logger.warning("Module coverage gap generation failed for '%s': %s", module_filter, llm_exc)
					generated_coverage_gaps = (
						"Coverage gap generation is unavailable right now. "
						"Use the missing-items list below to create test cases."
					)
			else:
				generated_coverage_gaps = "No coverage gaps detected for this module based on linked test cases."

			return {
				"module": module_filter,
				"scope": "module",
				"source": "ado_live",
				"presidio_protected": True,
				"sanitization_mode": phi.sanitization_mode,
				"total_stories": total_items,
				"covered": covered_count,
				"no_test_cases": no_tc,
				"coverage_percent": coverage_percent,
				"stories_without_tests": safe_stories_without_tests,
				"module_items": safe_module_items,
				"generated_coverage_gaps": generated_coverage_gaps,
			}

		stories = await asyncio.wait_for(client.get_stories_without_test_cases(), timeout=10.0)
		if module_filter:
			stories = [s for s in stories if module_filter.lower() in (s.title or "").lower() or module_filter.lower() in (s.area_path or "").lower()]
		safe_stories = []
		for story in stories[:30]:
			sanitized_story = await phi.sanitize_work_item(story)
			safe_stories.append({
				"id": sanitized_story.id,
				"title": sanitized_story.title,
				"state": sanitized_story.state,
			})
		total_stories = len(stories) or 1
		no_tc = len(stories)
		covered = total_stories - no_tc
		return {
			"module": module_filter or settings.ado_project or "Project",
			"total_stories": total_stories,
			"covered": covered,
			"no_test_cases": no_tc,
			"coverage_percent": int((covered / total_stories) * 100),
			"stories_without_tests": safe_stories,
			"source": "ado_live",
			"presidio_protected": True,
			"sanitization_mode": phi.sanitization_mode,
		}
	except Exception as exc:
		if module_filter:
			return {
				"module": module_filter,
				"scope": "module",
				"source": "fallback",
				"presidio_protected": True,
				"sanitization_mode": phi.sanitization_mode,
				"error": str(exc),
				"total_stories": 0,
				"covered": 0,
				"no_test_cases": 0,
				"coverage_percent": 0,
				"stories_without_tests": [],
				"module_items": [],
				"generated_coverage_gaps": "Coverage lookup failed. Please retry or provide a specific work item ID.",
			}
		return _mock_coverage_gaps(module_filter)


@app.get("/api/sprint-summary")
async def sprint_summary() -> dict[str, object]:
	if not _ado_is_configured():
		return _mock_sprint_summary()

	client = AdoClient()
	phi = PhiSanitizer()
	try:
		items = await client.get_sprint_items()
		rows = []
		state_counts: dict[str, int] = {}
		for item in items[:50]:
			sanitized = await phi.sanitize_work_item(item)
			rows.append(
				{
					"id": sanitized.id,
					"title": sanitized.title,
					"type": sanitized.work_item_type,
					"state": sanitized.state,
				}
			)
			state_counts[sanitized.state] = state_counts.get(sanitized.state, 0) + 1
		return {
			"sprint": "Current Sprint",
			"total_items": len(rows),
			"by_state": state_counts,
			"items": rows,
			"source": "ado_live",
		}
	except Exception:
		return _mock_sprint_summary()


@app.post("/api/generate-test-cases")
async def generate_test_cases(payload: GenerateTestCasesRequest) -> dict[str, object]:
	if payload.mode == "mock" and not settings.retrieval_first_testcase:
		pack = _build_mock_test_pack(payload.feature_text, payload.work_item_id)
		pack["mode"] = payload.mode
		pack["generated_at"] = datetime.now(timezone.utc).isoformat()
		return pack

	requested_count = _extract_requested_count(payload.feature_text)
	feature_query = _extract_feature_query(payload.feature_text)
	result = await _generate_test_cases_from_ado_context(
		message=payload.feature_text,
		work_item_id=payload.work_item_id,
		feature_query=feature_query,
		requested_count=requested_count,
	)
	result["mode"] = "live"
	result["generated_at"] = datetime.now(timezone.utc).isoformat()
	return result


@app.post("/api/chat")
async def chat(payload: ChatRequest) -> dict[str, object]:
	message = payload.message.strip()
	lower = message.lower()
	extracted_work_item_id = payload.work_item_id or _extract_work_item_id(message)
	feature_query = _extract_feature_query(message)

	if _is_testcase_generation_intent(message):
		requested_count = _extract_requested_count(message)
		try:
			return await _generate_test_cases_from_ado_context(
				message=message,
				work_item_id=extracted_work_item_id,
				feature_query=feature_query,
				requested_count=requested_count,
			)
		except Exception as exc:
			logger.error("Test case generation failed: %s", exc)
			return {
				"assistant_message": f"AI response unavailable: {str(exc)[:200]}. Try generating a QA pack or check your Foundry configuration.",
				"error": str(exc),
				"source": "error",
				"response_meta": {
					"source": "error",
					"grounded": False,
					"presidio_check": "unknown",
					"sanitization_mode": "unknown",
				},
			}

	if _is_ado_grounded_chat_intent(message):
		try:
			return await _generate_chat_from_ado_context(
				message=message,
				work_item_id=extracted_work_item_id,
				feature_query=feature_query,
			)
		except Exception as exc:
			logger.error("ADO-grounded chat failed: %s", exc)
			return {
				"assistant_message": f"ADO-grounded response unavailable: {str(exc)[:200]}. Please retry or provide a specific work item ID.",
				"error": str(exc),
				"source": "ado_error",
				"response_meta": {
					"source": "ado_error",
					"grounded": False,
					"presidio_check": "unknown",
					"sanitization_mode": "unknown",
				},
			}

	# Determine feature context for the LLM prompt
	if "automation" in lower or "script" in lower or "selenium" in lower:
		feature = "automation_script"
	elif "user guide" in lower or "guide" in lower or "documentation" in lower:
		feature = "user_guide"
	else:
		feature = "default"

	try:
		client = FoundryClient()
		response = await client.generate(
			user_prompt=message,
			feature=feature,
			conversation_history=None
		)
		return {
			"assistant_message": response,
			"source": "llm_direct",
			"grounded": False,
			"presidio_protected": False,
			"sanitization_mode": "n/a",
			"response_meta": {
				"source": "llm_direct",
				"grounded": False,
				"presidio_check": "n/a",
				"sanitization_mode": "n/a",
			},
		}
	except Exception as exc:
		logger.error(f"GPT-4o call failed: {exc}")
		return {
			"assistant_message": f"AI response unavailable: {str(exc)[:200]}. Try generating a QA pack or check your Foundry configuration.",
			"error": str(exc),
			"source": "error",
			"response_meta": {
				"source": "error",
				"grounded": False,
				"presidio_check": "unknown",
				"sanitization_mode": "unknown",
			},
		}


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

		.shell{max-width:1180px;margin:0 auto;padding:34px 20px 90px;}
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
			grid-template-columns:1fr 1fr;
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

		@media (max-width:1024px){
			.home-title{font-size:44px;}
			.home-copy{font-size:20px;}
			.mode-btn{font-size:22px;min-width:220px;}
			.field-wrap label{font-size:28px;}
			.input,.upload-row,.result-title,.result-content,.action-btn{font-size:24px;}
			.action-btn{min-height:106px;}
		}

		@media (max-width:900px){
			.action-grid{grid-template-columns:1fr;}
			.chat-shell{grid-template-columns:1fr;}
			.chat-nav{display:none;}
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
		}
	</style>
</head>
<body>
	<main class="shell">
		<div class="top-strip">
			<div class="top-brand"><span class="orb"></span>SmartCare QA Assistant</div>
			<div class="muted">AI-powered testing companion</div>
		</div>

		<section id="homeView" class="screen active">
			<article class="card">
				<div class="card-head">
					<div class="brand-lockup">
						<img class="brand-logo" src="/brand-logo" alt="Streamline Healthcare logo"/>
					</div>
					<button class="ghost-btn" onclick="showView('keyword')">Open Workspace</button>
				</div>

				<div class="home-body">
					<h2 class="home-title"><span class="grad">QA AI Assistant</span></h2>
					<p class="home-copy">Welcome to the AI-powered QA Assistant for smarter, faster, and automated QA testing.</p>

					<div class="mode-switch">
						<button class="mode-btn active" id="goFreeTextHome" onclick="showView('freetext')">Free Text View</button>
						<button class="mode-btn" id="goKeywordHome" onclick="showView('keyword')">Keyword View</button>
					</div>

					<div class="hero-art">
						<div class="blob"></div>
					</div>
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
					<button class="new-chat" onclick="startNewChatSession()">+ New Chat</button>
					<div class="chat-section-title">History</div>
					<div id="chatHistory" class="chat-history"></div>
					<button class="ghost-btn" onclick="showView('home')">Back to Home</button>
				</aside>

				<div class="chat-main">
					<div class="chat-top">
						<h2>Free Text View</h2>
						<div class="chat-status">Live Assistant</div>
					</div>

					<div id="chatFeed" class="chat-feed"></div>

					<div class="chat-input-wrap">
						<div class="composer">
							<textarea id="chatInput" placeholder="Message QA AI Assistant..."></textarea>
							<button id="sendBtn" class="send" onclick="sendChat()">Send</button>
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
			document.getElementById('freetextView').classList.toggle('active', view === 'freetext');
			document.getElementById('goFreeTextHome').classList.toggle('active', view === 'freetext');
			document.getElementById('goKeywordHome').classList.toggle('active', view !== 'freetext');
		}

		function parseTicketIds() {
			var raw = (document.getElementById('ticketInput').value || '').trim();
			if (!raw) {
				return [];
			}
			return raw.split(',').map(function(part){ return part.trim(); }).filter(Boolean);
		}

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

		var CHAT_STORAGE_KEY = 'smartcare_chat_sessions_v1';
		var chatSessions = [];
		var currentChatId = null;

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

		function getCurrentSession() {
			return chatSessions.find(function(session) { return session.id === currentChatId; }) || null;
		}

		function renderChatHistory() {
			var host = document.getElementById('chatHistory');
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

			chatSessions.slice().sort(function(a, b) {
				return String(b.updatedAt).localeCompare(String(a.updatedAt));
			}).forEach(function(session) {
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
				return;
			}
			if (!session.messages.length) {
				appendMessageToFeed('assistant', 'New chat started. Share your QA request and I will help with test design, coverage gaps, and automation guidance.');
				return;
			}
			session.messages.forEach(function(msg) {
				appendMessageToFeed(msg.role, msg.text, msg.meta || null);
			});
		}

		function selectChatSession(sessionId) {
			currentChatId = sessionId;
			renderChatHistory();
			renderChatFeed();
			showView('freetext');
		}

		function startNewChatSession() {
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

			try {
				var res = await fetch('/api/chat', {
					method: 'POST',
					headers: {'Content-Type': 'application/json'},
					body: JSON.stringify({message: text})
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

		showView('home');
		setKeywordPresidioStatus(null, null, 'not-run');
		chatSessions = loadChatSessions();
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
