from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from src.chat_ui.config import DEFAULT_MSP_SHEET_NAME, DEFAULT_MSP_TARGET_CATEGORY
from src.chat_ui.schemas import MLScoreRequest, MLScoreResponse, MLTicketScore
from src.services.predictive_service import (
	get_run_status,
	run_predictive_defaults_service,
	run_predictive_end_to_end_service,
)


router = APIRouter(tags=["predictive"])


@router.post("/api/predictive/run-defaults")
async def run_predictive_defaults(
	release_name: str = Form("MSP Default Run"),
	days: int = Form(180),
	top_score_levels: int = Form(50),
	work_item_scope: str = Form("Bug,Customer Ticket"),
	priority_scope: str = Form("On Fire,Urgent,High"),
	match_mode: str = Form("strict"),
	include_dependencies: bool = Form(True),
	modified_functions_csv: UploadFile | None = File(None),
	dependency_metrics_csv: UploadFile | None = File(None),
	feature_module_csv: UploadFile | None = File(None),
	module_summary_csv: UploadFile | None = File(None),
	functionality_summary_csv: UploadFile | None = File(None),
) -> dict[str, object]:
	return await run_predictive_defaults_service(
		release_name=release_name,
		days=days,
		top_score_levels=top_score_levels,
		work_item_scope=work_item_scope,
		priority_scope=priority_scope,
		match_mode=match_mode,
		include_dependencies=include_dependencies,
		modified_functions_csv=modified_functions_csv,
		dependency_metrics_csv=dependency_metrics_csv,
		feature_module_csv=feature_module_csv,
		module_summary_csv=module_summary_csv,
		functionality_summary_csv=functionality_summary_csv,
	)


@router.post("/api/predictive/run-e2e")
async def run_predictive_end_to_end(
	release_name: str = Form("MSP End-to-End Run"),
	days: int = Form(180),
	top_score_levels: int = Form(50),
	work_item_scope: str = Form("Bug,Customer Ticket"),
	priority_scope: str = Form("On Fire,Urgent,High"),
	match_mode: str = Form("strict"),
	include_dependencies: bool = Form(True),
	refresh_ado_export: bool = Form(True),
	apply_query_update: bool = Form(False),
	msp_sheet_name: str = Form(DEFAULT_MSP_SHEET_NAME),
	msp_target_category: str = Form(DEFAULT_MSP_TARGET_CATEGORY),
	run_id: str = Form(""),
	msp_workbook: UploadFile | None = File(None),
	dependency_metrics_csv: UploadFile | None = File(None),
	modified_functions_csv: UploadFile | None = File(None),
) -> dict[str, object]:
	return await run_predictive_end_to_end_service(
		release_name=release_name,
		days=days,
		top_score_levels=top_score_levels,
		work_item_scope=work_item_scope,
		priority_scope=priority_scope,
		match_mode=match_mode,
		include_dependencies=include_dependencies,
		refresh_ado_export=refresh_ado_export,
		apply_query_update=apply_query_update,
		msp_sheet_name=msp_sheet_name,
		msp_target_category=msp_target_category,
		run_id=run_id,
		msp_workbook=msp_workbook,
		dependency_metrics_csv=dependency_metrics_csv,
		modified_functions_csv=modified_functions_csv,
	)


@router.get("/api/predictive/run-status")
async def predictive_run_status(run_id: str = Query(...)) -> dict[str, object]:
	return get_run_status(run_id)


@router.post("/api/predictive/ml-score", response_model=MLScoreResponse)
async def ml_score_tickets(request: MLScoreRequest) -> MLScoreResponse:
	"""Score a batch of tickets using the trained XGBoost model."""
	try:
		from src.services.ml_inference_service import get_ml_inference_service
		svc = get_ml_inference_service()
	except FileNotFoundError as exc:
		raise HTTPException(
			status_code=503,
			detail=f"ML model not available: {exc}. Run the offline training script with --save-model first.",
		) from exc
	except Exception as exc:
		raise HTTPException(status_code=500, detail=f"Failed to load ML model: {exc}") from exc

	tickets_dicts = [
		{
			"ticket_id": t.ticket_id,
			"title": t.title,
			"changed_date": t.changed_date,
			"module_name": t.module_name,
			"modified_functionality": t.modified_functionality,
			"dependent_functionality": t.dependent_functionality,
			"customer_priority": t.customer_priority,
			"relationship_type": t.relationship_type,
			"work_item_type": t.work_item_type,
			"extra_fields": t.extra_fields,
		}
		for t in request.tickets
	]

	try:
		raw_scores = svc.score_tickets(tickets_dicts, threshold_override=request.threshold_override)
	except Exception as exc:
		raise HTTPException(status_code=500, detail=f"Scoring failed: {exc}") from exc

	scores = [MLTicketScore(**s) for s in raw_scores]
	predicted_positive_count = sum(1 for s in scores if s.predicted_label == 1)

	return MLScoreResponse(
		scores=scores,
		model_version=scores[0].model_version if scores else "unknown",
		threshold_used=scores[0].threshold_used if scores else 0.5,
		total_tickets=len(scores),
		predicted_positive_count=predicted_positive_count,
	)