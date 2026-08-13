from __future__ import annotations

from fastapi import APIRouter

from src.common.config.settings import get_settings


router = APIRouter(tags=["system"])
settings = get_settings()


@router.get("/api/status")
async def api_status() -> dict[str, object]:
	status = settings.feature_status()
	return {
		"app": settings.app_name,
		"environment": settings.app_env,
		"status": "running",
		"available_now": [feature_name for feature_name, feature_status in status.items() if feature_status["configured"]],
	}


@router.get("/health")
async def health() -> dict[str, str]:
	return {"status": "ok", "app": settings.app_name, "environment": settings.app_env}


@router.get("/readiness")
async def readiness() -> dict[str, object]:
	status = settings.feature_status()
	return {"summary": {"basic_app": status["basic_app"]["configured"], "basic_ai_pipeline": status["basic_ai_pipeline"]["configured"], "full_pipeline": status["full_pipeline"]["configured"]}, "features": status}


@router.get("/capabilities")
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
		"needs_more_config": {feature_name: feature_status["missing"] for feature_name, feature_status in status.items() if feature_status["missing"]},
	}
