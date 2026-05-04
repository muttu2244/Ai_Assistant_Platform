from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from integrations.copilot_power_automate_bridge.services.core_api_client import CoreApiClient
from integrations.copilot_power_automate_bridge.services.mock_engine import build_mock_result


class BridgeRequest(BaseModel):
    user_id: str = Field(min_length=1)
    feature_text: str = Field(min_length=1)
    work_item_id: int | None = None
    mode: Literal["mock", "proxy"] = "mock"


class PowerAutomateRequest(BaseModel):
    request_id: str = Field(min_length=1)
    feature_text: str = Field(min_length=1)
    work_item_id: int | None = None
    mode: Literal["mock", "proxy"] = "mock"


app = FastAPI(
    title="SmartCare Copilot/Power Automate Bridge",
    version="0.1.0",
    description=(
        "Standalone integration bridge for Copilot Studio and Power Automate. "
        "Runs independently from the core SmartCare API and supports offline mock mode."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _validate_api_key(x_api_key: str | None = Header(None)) -> None:
    """Validate API key if API_KEY_REQUIRED is set."""
    if os.getenv("SMARTCARE_BRIDGE_API_KEY_REQUIRED", "false").lower() == "true":
        expected_key = os.getenv("SMARTCARE_BRIDGE_API_KEY", "demo-key-change-in-prod")
        if not x_api_key or x_api_key != expected_key:
            raise HTTPException(status_code=403, detail="Invalid or missing API key")


def _get_core_client() -> CoreApiClient:
    base_url = os.getenv("SMARTCARE_CORE_API_BASE_URL", "http://127.0.0.1:8000")
    timeout_seconds = int(os.getenv("SMARTCARE_CORE_API_TIMEOUT_SECONDS", "20"))
    return CoreApiClient(base_url=base_url, timeout_seconds=timeout_seconds)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "copilot_power_automate_bridge",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/bridge/info")
async def bridge_info() -> dict[str, object]:
    return {
        "standalone": True,
        "default_mode": "mock",
        "supports_proxy_mode": True,
        "core_api_base_url": os.getenv("SMARTCARE_CORE_API_BASE_URL", "http://127.0.0.1:8000"),
        "intended_channels": ["Copilot Studio", "Power Automate"],
    }


@app.get("/demo/generate")
async def demo_generate(
    feature: str,
    work_item_id: int | None = None,
    mode: Literal["mock", "proxy"] = "mock",
) -> dict[str, object]:
    """Browser-friendly demo endpoint for quick testing."""
    result = build_mock_result(feature, work_item_id)
    return _format_card_response(
        channel="demo",
        summary=result.summary,
        test_cases=result.test_cases,
        traceability=result.traceability,
        estimated_minutes_saved=result.estimated_minutes_saved,
    )


@app.post("/bridge/copilot-chat")
async def copilot_chat(
    payload: BridgeRequest,
    x_api_key: str | None = Header(None),
) -> dict[str, object]:
    if payload.mode == "mock":
        result = build_mock_result(payload.feature_text, payload.work_item_id)
        return {
            "channel": "copilot_studio",
            "mode": "mock",
            "user_id": payload.user_id,
            "summary": result.summary,
            "test_cases": result.test_cases,
            "traceability": result.traceability,
            "estimated_minutes_saved": result.estimated_minutes_saved,
        }

    client = _get_core_client()
    try:
        forwarded = await client.relay_generation(
            endpoint_path=os.getenv("SMARTCARE_CORE_GENERATION_PATH", "/generate/test-cases"),
            payload={
                "feature_text": payload.feature_text,
                "work_item_id": payload.work_item_id,
                "user_id": payload.user_id,
            },
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=f"Core API proxy failed: {exc}") from exc

    return {
        "channel": "copilot_studio",
        "mode": "proxy",
        "forwarded_response": forwarded,
    }


@app.post("/bridge/power-automate")
async def power_automate(
    payload: PowerAutomateRequest,
    x_api_key: str | None = Header(None),
) -> dict[str, object]:
    _validate_api_key(x_api_key)
    if payload.mode == "mock":
        result = build_mock_result(payload.feature_text, payload.work_item_id)
        return _format_card_response(
            channel="power_automate",
            request_id=payload.request_id,
            summary=result.summary,
            test_cases=result.test_cases,
            traceability=result.traceability,
            estimated_minutes_saved=result.estimated_minutes_saved,
            export_file_name=f"{payload.request_id}-qa-pack.json",
        )

    client = _get_core_client()
    try:
        forwarded = await client.relay_generation(
            endpoint_path=os.getenv("SMARTCARE_CORE_GENERATION_PATH", "/generate/test-cases"),
            payload={
                "feature_text": payload.feature_text,
                "work_item_id": payload.work_item_id,
                "request_id": payload.request_id,
            },
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=f"Core API proxy failed: {exc}") from exc

    return {
        "channel": "power_automate",
        "mode": "proxy",
        "forwarded_response": forwarded,
        "request_id": payload.request_id,
    }


def _format_card_response(
    channel: str,
    summary: str,
    test_cases: list[dict[str, object]],
    traceability: list[dict[str, str]],
    estimated_minutes_saved: int,
    user_id: str | None = None,
    request_id: str | None = None,
    export_file_name: str | None = None,
) -> dict[str, object]:
    """Format response as a clean management-friendly card."""
    priority_counts = {"1": 0, "2": 0, "3": 0, "4": 0}
    for tc in test_cases:
        priority = str(tc.get("priority", 2))
        priority_counts[priority] = priority_counts.get(priority, 0) + 1

    response = {
        "channel": channel,
        "status": "success",
        "summary": summary,
        "metrics": {
            "total_test_cases": len(test_cases),
            "critical_tests": priority_counts.get("1", 0),
            "high_tests": priority_counts.get("2", 0),
            "medium_tests": priority_counts.get("3", 0),
            "low_tests": priority_counts.get("4", 0),
            "estimated_minutes_saved": estimated_minutes_saved,
            "traceability_lines": len(traceability),
        },
        "test_cases_sample": test_cases[:3],
        "compliance": {
            "hipaa_safe": True,
            "phi_sanitized": True,
        },
    }

    if user_id:
        response["user_id"] = user_id
    if request_id:
        response["request_id"] = request_id
    if export_file_name:
        response["export"] = {"file_name": export_file_name, "content_type": "application/json"}

    return response
