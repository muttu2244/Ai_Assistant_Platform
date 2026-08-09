from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GenerateTestCasesRequest(BaseModel):
	feature_text: str = Field(min_length=3)
	work_item_id: int | None = None
	mode: Literal["mock", "live"] = "mock"


class ChatRequest(BaseModel):
	message: str = Field(min_length=1)
	work_item_id: int | None = None
	session_id: str | None = None
	include_uploaded_context: bool = True


class MLTicketInput(BaseModel):
	ticket_id: str | int
	title: str | None = None
	changed_date: str | None = None
	module_name: str | None = None
	modified_functionality: str | None = None
	dependent_functionality: str | None = None
	customer_priority: str | None = None
	relationship_type: str | None = None
	work_item_type: str | None = None
	extra_fields: dict[str, object] = Field(default_factory=dict)


class MLTicketScore(BaseModel):
	ticket_id: str | int
	risk_score: float
	predicted_label: int
	threshold_used: float
	model_version: str
	ranked_priority: int


class MLScoreRequest(BaseModel):
	tickets: list[MLTicketInput] = Field(min_length=1)
	threshold_override: float | None = Field(default=None, ge=0.0, le=1.0)


class MLScoreResponse(BaseModel):
	scores: list[MLTicketScore]
	model_version: str
	threshold_used: float
	total_tickets: int
	predicted_positive_count: int
