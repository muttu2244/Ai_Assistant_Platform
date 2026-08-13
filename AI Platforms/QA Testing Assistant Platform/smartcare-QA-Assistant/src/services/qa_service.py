from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from src.agents.ado_fetcher.ado_client import AdoClient
from src.agents.ai_engine.foundry_client import FoundryClient
from src.agents.phi_sanitizer.presidio_analyzer import PhiSanitizer
from src.chat_ui.schemas import ChatRequest, GenerateTestCasesRequest
from src.common.config.settings import get_settings


settings = get_settings()

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


def ado_is_configured() -> bool:
	return settings.has_values(settings.ado_org_url, settings.ado_project, settings.ado_pat)


def strip_html(text: str | None) -> str:
	if not text:
		return ""
	clean = re.sub(r"<[^>]+>", " ", text)
	clean = clean.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")
	clean = re.sub(r"\s+", " ", clean).strip()
	return clean


def is_testcase_generation_intent(message: str) -> bool:
	lower = message.lower()
	has_target = any(token in lower for token in ("test case", "testcase", "test cases", "qa pack"))
	has_action = any(token in lower for token in ("generate", "create", "build", "write", "prepare", "draft"))
	return has_target and has_action


def extract_requested_count(message: str, default: int = 3) -> int:
	match = re.search(r"\b(\d{1,2})\s+(?:test\s*cases?|cases?)\b", message, flags=re.IGNORECASE)
	if match:
		return max(1, min(int(match.group(1)), 20))
	word_match = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:test\s*cases?|cases?)\b", message, flags=re.IGNORECASE)
	if word_match:
		return _NUMBER_WORDS[word_match.group(1).lower()]
	return default


def extract_work_item_id(message: str) -> int | None:
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
	standalone_ids = re.findall(r"\b(\d{5,9})\b", message)
	if len(standalone_ids) == 1:
		return int(standalone_ids[0])
	return None


def extract_feature_query(message: str) -> str | None:
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


def candidate_score(item: object, feature_query: str) -> int:
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


def count_generated_cases(text: str) -> int:
	if not text:
		return 0
	tc_matches = re.findall(r"(?im)^\s*(?:test\s*case\s*\d+|tc[-_ ]?\d+)\b", text)
	if tc_matches:
		return len(tc_matches)
	section_matches = re.findall(r"(?im)^\s*\d+\.\s+", text)
	return len(section_matches)


def is_linked_testcase_table_request(message: str) -> bool:
	lower = (message or "").lower()
	table_requested = "table" in lower or "tabular" in lower
	tc_requested = any(token in lower for token in ("linked test case", "linked test cases", "test cases linked", "existing test cases"))
	return table_requested and tc_requested


def table_cell(value: str) -> str:
	cell = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
	cell = re.sub(r"\n+", "<br>", cell)
	return cell.replace("|", "\\|")


def build_linked_testcase_markdown_table(rows: list[dict[str, str]]) -> str:
	if not rows:
		return "No linked test cases found for the selected work item."

	headers = ["Test Case ID", "Description", "Pre-Conditions", "Test Steps", "Expected Result"]
	lines = ["| " + " | ".join(headers) + " |", "|---|---|---|---|---|"]
	for row in rows:
		lines.append(
			"| {id} | {desc} | {pre} | {steps} | {expected} |".format(
				id=table_cell(row.get("test_case_id", "")),
				desc=table_cell(row.get("description", "")),
				pre=table_cell(row.get("pre_conditions", "")),
				steps=table_cell(row.get("test_steps", "")),
				expected=table_cell(row.get("expected_result", "")),
			)
		)
	return "\n".join(lines)


def prepend_response_meta_header(message_text: str, meta: dict[str, object]) -> str:
	parts = []
	if meta.get("work_item_id"):
		parts.append(f"Work Item ID: {meta['work_item_id']}")
	if meta.get("selection_mode"):
		parts.append(f"Selection Mode: {meta['selection_mode']}")
	if meta.get("presidio_check"):
		parts.append(f"Presidio Check: {meta['presidio_check']}")
	if meta.get("sanitization_mode"):
		parts.append(f"Sanitization Mode: {meta['sanitization_mode']}")
	if not parts:
		return message_text
	return "\n".join(parts) + "\n\n" + message_text


def is_ado_grounded_chat_intent(message: str) -> bool:
	lower = message.lower()
	return any(token in lower for token in (
		"ado", "azure devops", "work item", "user story", "acceptance criteria", "linked test case", "linked test cases", "test case linked",
	))


async def generate_chat_from_ado_context(message: str, work_item_id: int | None, feature_query: str | None) -> dict[str, object]:
	if not ado_is_configured():
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
		ranked = sorted(candidates, key=lambda item: candidate_score(item, feature_query), reverse=True)
		selected_id = ranked[0].id
		if len(ranked) > 1 and candidate_score(ranked[0], feature_query) < (candidate_score(ranked[1], feature_query) + 3):
			selection_mode = "search_match_ambiguous"

	full_context = await ado.get_work_item_full_context(selected_id)
	base_item = full_context["work_item"]
	safe_item = await phi.sanitize_work_item(base_item)

	safe_related_titles: list[str] = []
	for rel in full_context.get("related_items", [])[:8]:
		safe_rel = await phi.sanitize_work_item(rel)
		safe_related_titles.append(f"- {safe_rel.id}: {strip_html(safe_rel.title)} ({safe_rel.work_item_type}, {safe_rel.state})")

	safe_linked_cases: list[str] = []
	table_rows: list[dict[str, str]] = []
	for tc in full_context.get("linked_test_cases", [])[:8]:
		safe_tc = await phi.sanitize_test_case(tc)
		step_actions: list[str] = []
		expected_outcomes: list[str] = []
		for index, step in enumerate(safe_tc.steps[:8], start=1):
			action = strip_html(step.get("action", ""))
			expected = strip_html(step.get("expected", ""))
			if action:
				step_actions.append(f"{index}. {action}")
			if expected:
				expected_outcomes.append(f"{index}. {expected}")
			table_rows.append({
				"test_case_id": f"TC-{safe_tc.id}",
				"description": strip_html(safe_tc.title),
				"pre_conditions": "Not explicitly available in linked ADO test case metadata",
				"test_steps": "\n".join(step_actions) if step_actions else "Not provided",
				"expected_result": "\n".join(expected_outcomes) if expected_outcomes else "Not provided",
			})
			safe_linked_cases.append(f"- TC {safe_tc.id}: {strip_html(safe_tc.title)} [{safe_tc.state}] automated={safe_tc.automated} steps={len(safe_tc.steps)}")

	context_sections = [
		f"Work Item ID: {safe_item.id}",
		f"Title: {strip_html(safe_item.title)}",
		f"Type: {safe_item.work_item_type}",
		f"State: {safe_item.state}",
		f"Area Path: {safe_item.area_path}",
		f"Iteration Path: {safe_item.iteration_path}",
		"Description:\n" + (strip_html(safe_item.description) or "Not provided"),
		"Acceptance Criteria:\n" + (strip_html(safe_item.acceptance_criteria) or "Not provided"),
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

	if is_linked_testcase_table_request(message):
		table_text = build_linked_testcase_markdown_table(table_rows)
		safe_table_text = await phi.sanitize_output(table_text)
		response_with_meta = prepend_response_meta_header(safe_table_text, response_meta)
		return {
			"assistant_message": response_with_meta,
			"source": "ado_live",
			"grounded": True,
			"used_work_item_id": safe_item.id,
			"used_work_item_title": strip_html(safe_item.title),
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
	response = await client.generate(user_prompt=grounded_prompt, feature=feature, conversation_history=None, context=ado_context)
	safe_response = await phi.sanitize_output(response)
	response_with_meta = prepend_response_meta_header(safe_response, response_meta)
	return {
		"assistant_message": response_with_meta,
		"source": "ado_live",
		"grounded": True,
		"used_work_item_id": safe_item.id,
		"used_work_item_title": strip_html(safe_item.title),
		"selection_mode": selection_mode,
		"presidio_protected": True,
		"sanitization_mode": phi.sanitization_mode,
		"response_meta": response_meta,
	}


async def generate_test_cases_from_ado_context(message: str, work_item_id: int | None, feature_query: str | None, requested_count: int) -> dict[str, object]:
	if not settings.retrieval_first_testcase:
		client = FoundryClient()
		response = await client.generate(user_prompt=message, feature="test_case")
		return {"assistant_message": response, "source": "llm_direct"}

	if not ado_is_configured():
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
		ranked = sorted(candidates, key=lambda item: candidate_score(item, feature_query), reverse=True)
		if len(ranked) > 1 and candidate_score(ranked[0], feature_query) >= (candidate_score(ranked[1], feature_query) + 3):
			selected_id = ranked[0].id
		elif len(ranked) > 1:
			candidate_rows = []
			for item in ranked[:5]:
				candidate_rows.append({"id": item.id, "title": strip_html(item.title), "state": item.state, "area_path": item.area_path})
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
		safe_related_titles.append(f"- {safe_rel.id}: {strip_html(safe_rel.title)} ({safe_rel.work_item_type}, {safe_rel.state})")

	safe_linked_cases: list[str] = []
	for tc in full_context.get("linked_test_cases", [])[:8]:
		safe_tc = await phi.sanitize_test_case(tc)
		safe_linked_cases.append(f"- TC {safe_tc.id}: {strip_html(safe_tc.title)} [{safe_tc.state}]")

	context_sections = [
		f"Work Item ID: {safe_item.id}",
		f"Title: {strip_html(safe_item.title)}",
		f"Type: {safe_item.work_item_type}",
		f"State: {safe_item.state}",
		f"Area Path: {safe_item.area_path}",
		f"Iteration Path: {safe_item.iteration_path}",
		"Description:\n" + (strip_html(safe_item.description) or "Not provided"),
		"Acceptance Criteria:\n" + (strip_html(safe_item.acceptance_criteria) or "Not provided"),
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
	response = await client.generate(user_prompt=generation_prompt, feature="test_case", conversation_history=None, context=ado_context)
	produced_count = count_generated_cases(response)
	if produced_count and produced_count != requested_count:
		correction_prompt = (
			f"Regenerate and return exactly {requested_count} test cases. "
			f"Your previous response contained {produced_count}. Keep the same format and keep outputs grounded to context."
		)
		response = await client.generate(
			user_prompt=correction_prompt,
			feature="test_case",
			conversation_history=[{"role": "user", "content": generation_prompt}, {"role": "assistant", "content": response}],
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
	response_with_meta = prepend_response_meta_header(safe_response, response_meta)
	return {
		"assistant_message": response_with_meta,
		"source": "ado_live",
		"used_work_item_id": safe_item.id,
		"used_work_item_title": strip_html(safe_item.title),
		"selection_mode": selection_mode,
		"sanitized": True,
		"presidio_protected": True,
		"sanitization_mode": phi.sanitization_mode,
		"response_meta": response_meta,
	}


def build_mock_test_pack(feature_text: str, work_item_id: int | None = None) -> dict[str, object]:
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
		"metrics": {"total_test_cases": len(test_cases), "critical_tests": 1, "high_tests": 1, "medium_tests": 1, "estimated_minutes_saved": 75},
		"compliance": {"hipaa_safe": True, "phi_sanitized": True},
	}


def mock_coverage_gaps(module: str | None) -> dict[str, object]:
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


def mock_coverage_for_item(work_item_id: int) -> dict[str, object]:
	return {
		"source": "mock",
		"scope": "work_item",
		"presidio_protected": True,
		"sanitization_mode": "mock",
		"work_item": {"id": work_item_id, "title": "Mock work item for coverage demo", "description": "ADO is not configured locally. Showing mock coverage details.", "state": "Active", "type": "User Story", "area_path": "SmartCare\\Demo", "iteration_path": "Sprint Demo"},
		"total_stories": 1,
		"covered": 0,
		"no_test_cases": 1,
		"coverage_percent": 0,
		"stories_without_tests": [{"id": work_item_id, "title": "Mock work item for coverage demo", "state": "Active"}],
		"linked_test_cases": [],
	}


def mock_sprint_summary() -> dict[str, object]:
	items = [
		{"id": 675745, "title": "Member care plan parameterized string fix", "type": "User Story", "state": "Active"},
		{"id": 675746, "title": "Provider mapping regression", "type": "Bug", "state": "Committed"},
		{"id": 675747, "title": "Policy acknowledgment automation", "type": "Task", "state": "New"},
		{"id": 675748, "title": "Coverage import performance validation", "type": "User Story", "state": "Active"},
	]
	return {"sprint": "Current Sprint", "total_items": len(items), "by_state": {"New": 1, "Active": 2, "Committed": 1}, "items": items, "source": "mock"}


async def lookup_test_case(work_item_id: int) -> dict[str, object]:
	if not ado_is_configured():
		return {"source": "mock", "presidio_protected": True, "sanitization_mode": "mock", "work_item": {"id": work_item_id, "title": "Mock story for local demo", "description": "ADO is not configured locally. Showing sanitized mock data.", "state": "Active", "type": "User Story"}}

	client = AdoClient()
	phi = PhiSanitizer()
	try:
		item = await client.get_work_item(work_item_id)
		sanitized = await phi.sanitize_work_item(item)
		dump = sanitized.model_dump()
		dump["description"] = strip_html(dump.get("description"))
		dump["acceptance_criteria"] = strip_html(dump.get("acceptance_criteria"))
		return {"source": "ado_live", "presidio_protected": True, "sanitization_mode": phi.sanitization_mode, "work_item": dump}
	except Exception as exc:
		return {"source": "fallback", "presidio_protected": True, "sanitization_mode": phi.sanitization_mode, "error": str(exc), "work_item": {"id": work_item_id, "title": "Fallback story", "description": "Live fetch failed, fallback data shown for demo continuity.", "state": "Unknown", "type": "User Story"}}


async def get_coverage_gaps(module: str | None = None, work_item_id: int | None = None) -> dict[str, object]:
	if work_item_id is None and module and module.strip().isdigit():
		work_item_id = int(module.strip())
	if work_item_id is not None:
		if not ado_is_configured():
			return mock_coverage_for_item(work_item_id)
		client = AdoClient()
		phi = PhiSanitizer()
		try:
			item = await client.get_work_item(work_item_id)
			test_cases = await client.get_test_cases_for_story(work_item_id)
			sanitized_item = await phi.sanitize_work_item(item)
			dump = sanitized_item.model_dump()
			dump["description"] = strip_html(dump.get("description"))
			dump["acceptance_criteria"] = strip_html(dump.get("acceptance_criteria"))
			safe_test_cases = []
			for tc in test_cases[:25]:
				safe_tc = await phi.sanitize_test_case(tc)
				safe_test_cases.append({"id": safe_tc.id, "title": strip_html(safe_tc.title), "state": safe_tc.state})
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
				coverage_context_lines.append("Linked Test Cases:\n" + "\n".join([f"- TC-{tc['id']}: {tc['title']} ({tc['state']})" for tc in safe_test_cases]))
			else:
				coverage_context_lines.append("Linked Test Cases:\n- None linked")
			coverage_context = "\n\n".join(coverage_context_lines)
			analysis_prompt = (
				"Perform a test coverage gap analysis for this ADO work item. Return concise QA-focused output with these sections:\n"
				"1) Coverage Verdict (Covered/Partially Covered/Not Covered)\n"
				"2) Key Gaps (bullet list)\n"
				"3) Recommended Missing Test Scenarios (numbered)\n"
				"4) Risk if Not Tested (short)\n"
				"Base this only on provided context and linked test cases."
			)
			generated_coverage_gaps = ""
			try:
				llm = FoundryClient()
				generated_coverage_gaps = await llm.generate(user_prompt=analysis_prompt, feature="test_case", conversation_history=None, context=coverage_context)
			except Exception:
				generated_coverage_gaps = "Coverage analysis generation is currently unavailable. Basic linked-test-case coverage metrics are shown above."
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
				"stories_without_tests": [] if has_coverage else [{"id": dump["id"], "title": dump["title"], "state": dump["state"]}],
				"linked_test_cases": safe_test_cases,
				"generated_coverage_gaps": generated_coverage_gaps,
			}
		except Exception as exc:
			return {"source": "fallback", "scope": "work_item", "presidio_protected": True, "sanitization_mode": phi.sanitization_mode, "error": str(exc), **mock_coverage_for_item(work_item_id)}

	module_filter = module.strip() if module and module.strip() else None
	if not ado_is_configured():
		return mock_coverage_gaps(module_filter)
	client = AdoClient()
	phi = PhiSanitizer()
	try:
		if module_filter:
			candidates = await asyncio.wait_for(client.search_work_items_by_text(module_filter, top_n=20), timeout=20.0)
			if not candidates:
				return {"module": module_filter, "scope": "module", "source": "ado_live", "presidio_protected": True, "sanitization_mode": phi.sanitization_mode, "total_stories": 0, "covered": 0, "no_test_cases": 0, "coverage_percent": 0, "stories_without_tests": [], "module_items": [], "generated_coverage_gaps": "No ADO work items were found for this module."}
			safe_module_items = []
			safe_stories_without_tests = []
			covered_count = 0
			for item in candidates:
				sanitized_item = await phi.sanitize_work_item(item)
				test_cases = await client.get_test_cases_for_story(item.id)
				has_tests = len(test_cases) > 0
				if has_tests:
					covered_count += 1
				safe_module_items.append({"id": sanitized_item.id, "title": strip_html(sanitized_item.title), "state": sanitized_item.state, "type": sanitized_item.work_item_type, "linked_test_cases": len(test_cases)})
				if not has_tests:
					safe_stories_without_tests.append({"id": sanitized_item.id, "title": strip_html(sanitized_item.title), "state": sanitized_item.state})
			total_items = len(safe_module_items)
			no_tc = len(safe_stories_without_tests)
			coverage_percent = int((covered_count / total_items) * 100) if total_items else 0
			coverage_context = "\n".join([
				f"Module: {module_filter}", f"Total Items Considered: {total_items}", f"Covered Items: {covered_count}", f"Items Without Linked Test Cases: {no_tc}", "Items:", *[f"- {row['id']}: {row['title']} ({row['type']}, {row['state']}) linked_test_cases={row['linked_test_cases']}" for row in safe_module_items[:20]],
			])
			if no_tc > 0:
				try:
					llm = FoundryClient()
					generated_coverage_gaps = await llm.generate(user_prompt=("Generate module-level coverage gap analysis and missing test recommendations. Return sections:\n1) Coverage Verdict\n2) Top Missing Areas\n3) Recommended New Test Cases by Work Item ID\n4) Suggested Priority (Critical/High/Medium)\nFocus only on items with zero linked test cases."), feature="test_case", conversation_history=None, context=coverage_context)
				except Exception:
					generated_coverage_gaps = "Coverage gap generation is unavailable right now. Use the missing-items list below to create test cases."
			else:
				generated_coverage_gaps = "No coverage gaps detected for this module based on linked test cases."
			return {"module": module_filter, "scope": "module", "source": "ado_live", "presidio_protected": True, "sanitization_mode": phi.sanitization_mode, "total_stories": total_items, "covered": covered_count, "no_test_cases": no_tc, "coverage_percent": coverage_percent, "stories_without_tests": safe_stories_without_tests, "module_items": safe_module_items, "generated_coverage_gaps": generated_coverage_gaps}

		stories = await asyncio.wait_for(client.get_stories_without_test_cases(), timeout=10.0)
		if module_filter:
			stories = [s for s in stories if module_filter.lower() in (s.title or "").lower() or module_filter.lower() in (s.area_path or "").lower()]
		safe_stories = []
		for story in stories[:30]:
			sanitized_story = await phi.sanitize_work_item(story)
			safe_stories.append({"id": sanitized_story.id, "title": sanitized_story.title, "state": sanitized_story.state})
		total_stories = len(stories) or 1
		no_tc = len(stories)
		covered = total_stories - no_tc
		return {"module": module_filter or settings.ado_project or "Project", "total_stories": total_stories, "covered": covered, "no_test_cases": no_tc, "coverage_percent": int((covered / total_stories) * 100), "stories_without_tests": safe_stories, "source": "ado_live", "presidio_protected": True, "sanitization_mode": phi.sanitization_mode}
	except Exception:
		if module_filter:
			return {"module": module_filter, "scope": "module", "source": "fallback", "presidio_protected": True, "sanitization_mode": phi.sanitization_mode, "error": "Coverage lookup failed. Please retry or provide a specific work item ID.", "total_stories": 0, "covered": 0, "no_test_cases": 0, "coverage_percent": 0, "stories_without_tests": [], "module_items": [], "generated_coverage_gaps": "Coverage lookup failed. Please retry or provide a specific work item ID."}
		return mock_coverage_gaps(module_filter)


async def get_sprint_summary() -> dict[str, object]:
	if not ado_is_configured():
		return mock_sprint_summary()
	client = AdoClient()
	phi = PhiSanitizer()
	try:
		items = await client.get_sprint_items()
		rows = []
		state_counts: dict[str, int] = {}
		for item in items[:50]:
			sanitized = await phi.sanitize_work_item(item)
			rows.append({"id": sanitized.id, "title": sanitized.title, "type": sanitized.work_item_type, "state": sanitized.state})
			state_counts[sanitized.state] = state_counts.get(sanitized.state, 0) + 1
		return {"sprint": "Current Sprint", "total_items": len(rows), "by_state": state_counts, "items": rows, "source": "ado_live"}
	except Exception:
		return mock_sprint_summary()


async def generate_test_cases(payload: GenerateTestCasesRequest) -> dict[str, object]:
	if payload.mode == "mock" and not settings.retrieval_first_testcase:
		pack = build_mock_test_pack(payload.feature_text, payload.work_item_id)
		pack["mode"] = payload.mode
		pack["generated_at"] = datetime.now(timezone.utc).isoformat()
		return pack
	requested_count = extract_requested_count(payload.feature_text)
	feature_query = extract_feature_query(payload.feature_text)
	result = await generate_test_cases_from_ado_context(message=payload.feature_text, work_item_id=payload.work_item_id, feature_query=feature_query, requested_count=requested_count)
	result["mode"] = "live"
	result["generated_at"] = datetime.now(timezone.utc).isoformat()
	return result


async def handle_chat(payload: ChatRequest, uploaded_context: str, uploaded_sources: list[str]) -> dict[str, object]:
	message = payload.message.strip()
	lower = message.lower()
	extracted_work_item_id = payload.work_item_id or extract_work_item_id(message)
	feature_query = extract_feature_query(message)
	file_only_tokens = ("use attached csv files only", "attached files only", "do not query external systems", "do not use external systems", "use uploaded files only")
	force_uploaded_context_only = any(token in lower for token in file_only_tokens)
	explicit_ado_tokens = ("azure devops", " from ado", "use ado", "query ado", "ado work item", "work item id")
	explicit_ado_request = any(token in lower for token in explicit_ado_tokens)
	expects_attached_context = any(token in lower for token in ("attached", "attachment", "uploaded", "csv", "file", "source of truth", "validate fields"))
	if payload.include_uploaded_context and expects_attached_context and not uploaded_context:
		return {
			"assistant_message": "No active uploaded context was found for this chat session. Please attach files again in this same chat thread, then resend your prompt.",
			"source": "uploaded_context_missing",
			"grounded": False,
			"presidio_protected": False,
			"sanitization_mode": "n/a",
			"response_meta": {"source": "uploaded_context_missing", "grounded": False, "presidio_check": "n/a", "sanitization_mode": "n/a", "uploaded_context_used": False, "uploaded_sources": [], "session_id": payload.session_id},
		}
	if is_testcase_generation_intent(message):
		requested_count = extract_requested_count(message)
		try:
			return await generate_test_cases_from_ado_context(message=message, work_item_id=extracted_work_item_id, feature_query=feature_query, requested_count=requested_count)
		except Exception as exc:
			return {"assistant_message": f"AI response unavailable: {str(exc)[:200]}. Try generating a QA pack or check your Foundry configuration.", "error": str(exc), "source": "error", "response_meta": {"source": "error", "grounded": False, "presidio_check": "unknown", "sanitization_mode": "unknown"}}
	should_route_to_ado = is_ado_grounded_chat_intent(message) and not force_uploaded_context_only and (extracted_work_item_id is not None or explicit_ado_request or not uploaded_context)
	if should_route_to_ado:
		try:
			return await generate_chat_from_ado_context(message=message, work_item_id=extracted_work_item_id, feature_query=feature_query)
		except Exception as exc:
			return {"assistant_message": f"ADO-grounded response unavailable: {str(exc)[:200]}. Please retry or provide a specific work item ID.", "error": str(exc), "source": "ado_error", "response_meta": {"source": "ado_error", "grounded": False, "presidio_check": "unknown", "sanitization_mode": "unknown"}}
	if "automation" in lower or "script" in lower or "selenium" in lower:
		feature = "automation_script"
	elif "user guide" in lower or "guide" in lower or "documentation" in lower:
		feature = "user_guide"
	else:
		feature = "default"
	try:
		client = FoundryClient()
		response = await client.generate(user_prompt=message, feature=feature, conversation_history=None, context=uploaded_context or None)
		return {"assistant_message": response, "source": "llm_direct", "grounded": False, "presidio_protected": False, "sanitization_mode": "n/a", "response_meta": {"source": "llm_direct", "grounded": False, "presidio_check": "n/a", "sanitization_mode": "n/a", "uploaded_context_used": bool(uploaded_context), "uploaded_sources": uploaded_sources}}
	except Exception as exc:
		return {"assistant_message": f"AI response unavailable: {str(exc)[:200]}. Try generating a QA pack or check your Foundry configuration.", "error": str(exc), "source": "error", "response_meta": {"source": "error", "grounded": False, "presidio_check": "unknown", "sanitization_mode": "unknown"}}
