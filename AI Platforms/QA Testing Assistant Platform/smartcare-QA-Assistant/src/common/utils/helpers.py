"""
SmartCare QA Assistant — Shared Utility Helpers
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def new_session_id() -> str:
    """Generate a new UUID4 session identifier."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


def truncate_text(text: str, max_chars: int = 8000) -> str:
    """
    Truncate text to max_chars to stay within LLM context limits.
    Appends a marker so callers know truncation occurred.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[... truncated for context window ...]"


def strip_html(text: str) -> str:
    """Remove HTML tags from ADO rich-text fields."""
    clean = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s{2,}", " ", clean).strip()


def deterministic_id(value: str) -> str:
    """
    Return a stable short ID derived from a string value.
    Used by Presidio masking to ensure same input → same dummy output.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:12].upper()


def flatten_ado_fields(raw_fields: dict[str, Any]) -> dict[str, Any]:
    """
    ADO REST API returns fields under a nested 'fields' key with long names
    like 'System.Title'. Flatten to simple snake_case keys for internal use.
    """
    mapping = {
        "System.Id": "id",
        "System.Title": "title",
        "System.Description": "description",
        "System.WorkItemType": "work_item_type",
        "System.State": "state",
        "System.AssignedTo": "assigned_to",
        "System.AreaPath": "area_path",
        "System.IterationPath": "iteration_path",
        "System.Tags": "tags",
        "System.CreatedBy": "created_by",
        "System.CreatedDate": "created_date",
        "System.ChangedDate": "changed_date",
        "Microsoft.VSTS.Common.AcceptanceCriteria": "acceptance_criteria",
    }
    flat: dict[str, Any] = {}
    for ado_key, simple_key in mapping.items():
        value = raw_fields.get(ado_key)
        if value is None:
            continue
        # AssignedTo / CreatedBy come as dicts with a 'displayName' key
        if isinstance(value, dict) and "displayName" in value:
            value = value["displayName"]
        # Tags come as a semicolon-separated string
        if simple_key == "tags" and isinstance(value, str):
            value = [t.strip() for t in value.split(";") if t.strip()]
        flat[simple_key] = value
    return flat


def build_ado_test_steps(steps_xml: str) -> list[dict[str, str]]:
    """
    Parse ADO test-case steps from the XML format returned by the REST API
    into a list of {"action": ..., "expected": ...} dicts.
    """
    steps: list[dict[str, str]] = []
    action_pattern = re.compile(r"<parameterizedString[^>]*>(.+?)</parameterizedString>", re.DOTALL)
    step_pattern = re.compile(r"<step[^>]*>(.*?)</step>", re.DOTALL)
    for step_match in step_pattern.finditer(steps_xml or ""):
        parts = action_pattern.findall(step_match.group(1))
        action = strip_html(parts[0]) if len(parts) > 0 else ""
        expected = strip_html(parts[1]) if len(parts) > 1 else ""
        if action:
            steps.append({"action": action, "expected": expected})
    return steps


def safe_get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dicts without raising KeyError."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
        if current is None:
            return default
    return current
