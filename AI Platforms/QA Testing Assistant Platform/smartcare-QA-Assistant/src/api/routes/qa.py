from __future__ import annotations

from fastapi import APIRouter, Query

from src.chat_ui.schemas import GenerateTestCasesRequest
from src.services.qa_service import generate_test_cases, get_coverage_gaps, get_sprint_summary, lookup_test_case


router = APIRouter(tags=["qa"])


@router.get("/api/test-case-lookup")
async def test_case_lookup(work_item_id: int = Query(..., gt=0)) -> dict[str, object]:
	return await lookup_test_case(work_item_id)


@router.get("/api/coverage-gaps")
async def coverage_gaps(module: str | None = None, work_item_id: int | None = None) -> dict[str, object]:
	return await get_coverage_gaps(module=module, work_item_id=work_item_id)


@router.get("/api/sprint-summary")
async def sprint_summary() -> dict[str, object]:
	return await get_sprint_summary()


@router.post("/api/generate-test-cases")
async def generate_test_cases_route(payload: GenerateTestCasesRequest) -> dict[str, object]:
	return await generate_test_cases(payload)
