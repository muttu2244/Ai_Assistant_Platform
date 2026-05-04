"""
SmartCare QA Assistant — Shared Pydantic Schemas

All data that crosses module boundaries is typed here.
PHI-bearing fields are annotated; they must be sanitized before leaving ado_fetcher.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Enumerations ──────────────────────────────────────────────────────────────

class WorkItemType(str, Enum):
    USER_STORY = "User Story"
    BUG = "Bug"
    TASK = "Task"
    TEST_CASE = "Test Case"
    EPIC = "Epic"
    FEATURE = "Feature"


class SanitizationStatus(str, Enum):
    RAW = "raw"           # PHI may be present — DO NOT send to LLM
    SANITIZED = "sanitized"  # Presidio-cleaned — safe for LLM


class GenerationFeature(str, Enum):
    TEST_CASE = "test_case"
    AUTOMATION_SCRIPT = "automation_script"
    USER_GUIDE = "user_guide"


# ── Azure DevOps Raw & Sanitized Models ──────────────────────────────────────

class AdoWorkItem(BaseModel):
    """Raw ADO work item — may contain PHI. Never send directly to LLM."""
    id: int
    title: str
    description: Optional[str] = None
    work_item_type: str
    state: str
    assigned_to: Optional[str] = None     # PHI field
    area_path: str = ""
    iteration_path: str = ""
    tags: list[str] = Field(default_factory=list)
    acceptance_criteria: Optional[str] = None
    created_by: Optional[str] = None      # PHI field
    created_date: Optional[datetime] = None
    changed_date: Optional[datetime] = None
    sanitization_status: SanitizationStatus = SanitizationStatus.RAW
    raw_fields: dict[str, Any] = Field(default_factory=dict)


class AdoTestCase(BaseModel):
    """Raw ADO test case — may contain PHI. Never send directly to LLM."""
    id: int
    title: str
    steps: list[dict[str, str]] = Field(default_factory=list)
    priority: int = 2
    state: str = "Design"
    associated_story_ids: list[int] = Field(default_factory=list)
    automated: bool = False
    sanitization_status: SanitizationStatus = SanitizationStatus.RAW


class AdoTestPlan(BaseModel):
    id: int
    name: str
    sprint: str = ""
    test_suite_ids: list[int] = Field(default_factory=list)
    sanitization_status: SanitizationStatus = SanitizationStatus.RAW


class AdoDefectSummary(BaseModel):
    """Raw defect/work-item summary for analytics workflows."""
    id: int
    title: str
    state: str = ""
    severity: str = ""
    area_path: str = ""
    iteration_path: str = ""
    linked_work_item_ids: list[int] = Field(default_factory=list)
    linked_test_case_ids: list[int] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    sanitization_status: SanitizationStatus = SanitizationStatus.RAW


class AdoTestExecutionRecord(BaseModel):
    """Raw test execution history row from ADO test runs/results."""
    run_id: int
    test_case_id: int
    test_case_title: str = ""
    outcome: str = ""
    started_date: Optional[datetime] = None
    completed_date: Optional[datetime] = None
    duration_ms: Optional[int] = None
    tester: str = ""
    automated_test_name: str = ""
    run_state: str = ""
    run_name: str = ""
    sanitization_status: SanitizationStatus = SanitizationStatus.RAW


class AdoWorkItemHierarchy(BaseModel):
    """Hierarchy snapshot for Epic -> Feature -> Story -> Test Case traversal."""
    focus_item: AdoWorkItem
    ancestor_items: list[AdoWorkItem] = Field(default_factory=list)
    parent_item_ids: list[int] = Field(default_factory=list)
    child_items: list[AdoWorkItem] = Field(default_factory=list)
    child_item_ids: list[int] = Field(default_factory=list)
    linked_test_cases: list[AdoTestCase] = Field(default_factory=list)
    linked_defects: list[AdoDefectSummary] = Field(default_factory=list)
    relation_count: int = 0
    sanitization_status: SanitizationStatus = SanitizationStatus.RAW


class SanitizedWorkItem(BaseModel):
    """Presidio-cleaned work item — safe to pass to GPT-4o."""
    id: int
    title: str
    description: Optional[str] = None
    work_item_type: str
    state: str
    area_path: str = ""
    iteration_path: str = ""
    tags: list[str] = Field(default_factory=list)
    acceptance_criteria: Optional[str] = None
    sanitization_status: SanitizationStatus = SanitizationStatus.SANITIZED


class SanitizedTestCase(BaseModel):
    """Presidio-cleaned test case — safe to pass to GPT-4o."""
    id: int
    title: str
    steps: list[dict[str, str]] = Field(default_factory=list)
    priority: int = 2
    state: str = "Design"
    associated_story_ids: list[int] = Field(default_factory=list)
    automated: bool = False
    sanitization_status: SanitizationStatus = SanitizationStatus.SANITIZED


# ── Chat / Request Models ─────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str           # "user" | "assistant" | "system"
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class UserRequest(BaseModel):
    session_id: str
    user_id: str
    feature: GenerationFeature
    prompt: str
    ado_item_ids: list[int] = Field(default_factory=list)
    conversation_history: list[ChatMessage] = Field(default_factory=list)


# ── Generation Output Models ──────────────────────────────────────────────────

class GeneratedTestCase(BaseModel):
    title: str
    preconditions: str = ""
    steps: list[dict[str, str]]           # [{"action": ..., "expected": ...}]
    priority: int = 2
    tags: list[str] = Field(default_factory=list)
    source_story_id: Optional[int] = None


class GeneratedAutomationScript(BaseModel):
    language: str = "Java"
    framework: str = "Selenium"
    class_name: str
    code: str
    source_test_case_id: Optional[int] = None


class GeneratedUserGuide(BaseModel):
    title: str
    sections: list[dict[str, str]]        # [{"heading": ..., "body": ...}]
    source_item_ids: list[int] = Field(default_factory=list)


class AssistantResponse(BaseModel):
    session_id: str
    feature: GenerationFeature
    text_response: str
    generated_artifact: Optional[
        GeneratedTestCase | GeneratedAutomationScript | GeneratedUserGuide
    ] = None
    blob_url: Optional[str] = None
    sanitization_applied: bool = True
    timestamp: datetime = Field(default_factory=datetime.utcnow)
