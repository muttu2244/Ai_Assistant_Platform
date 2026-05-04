"""
SmartCare QA Assistant — Test Case Generator Feature
Pipeline: sanitized prompt → GPT-4o → sanitized output → structured test cases
"""

from __future__ import annotations

import logging
from typing import Optional

from src.agents.ai_engine.foundry_client import FoundryClient
from src.agents.output_sanitizer.output_presidio import OutputSanitizer
from src.common.models.schemas import GeneratedTestCase

logger = logging.getLogger(__name__)


class TestCaseGenerator:
    """Generates test cases from a natural language scenario description."""

    def __init__(self) -> None:
        self._ai = FoundryClient()
        self._output_sanitizer = OutputSanitizer()

    async def generate(
        self,
        scenario: str,
        ado_context: str = "",
        conversation_history: Optional[list[dict]] = None,
    ) -> tuple[str, list[GeneratedTestCase]]:
        """
        Generate test cases for a given scenario.

        Args:
            scenario: User's description of what to test (already sanitized by STEP 2).
            ado_context: Optional ADO work item context (already sanitized).
            conversation_history: Prior conversation turns.

        Returns:
            (raw_text_response, list_of_parsed_test_cases)
        """
        prompt = _build_test_case_prompt(scenario)

        raw_response = await self._ai.generate(
            user_prompt=prompt,
            feature="test_case",
            context=ado_context,
            conversation_history=conversation_history,
        )

        # STEP 4 — double protection: sanitize the LLM output
        safe_response = await self._output_sanitizer.sanitize(raw_response)

        parsed = _parse_test_cases(safe_response)
        logger.info("Test case generator produced %d test cases.", len(parsed))
        return safe_response, parsed


def _build_test_case_prompt(scenario: str) -> str:
    return f"""Generate comprehensive test cases for the following scenario:

{scenario}

For each test case provide:
- Title
- Priority (1=Critical, 2=High, 3=Medium, 4=Low)
- Preconditions
- Numbered steps (Action | Expected Result)
- Tags (e.g. [smoke], [regression], [negative])

Separate each test case with '---'."""


def _parse_test_cases(text: str) -> list[GeneratedTestCase]:
    """Parse GPT-4o output into structured GeneratedTestCase objects."""
    test_cases: list[GeneratedTestCase] = []
    blocks = [b.strip() for b in text.split("---") if b.strip()]
    for i, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        title = next((ln.replace("Title:", "").strip() for ln in lines if "Title:" in ln), f"Test Case {i}")
        priority_raw = next((ln for ln in lines if "Priority:" in ln), "")
        try:
            priority = int("".join(c for c in priority_raw if c.isdigit())[:1]) or 2
        except (ValueError, IndexError):
            priority = 2
        preconditions = next((ln.replace("Preconditions:", "").strip() for ln in lines if "Preconditions:" in ln), "")
        tags = []
        import re
        tags_match = re.findall(r"\[([^\]]+)\]", block)
        if tags_match:
            tags = tags_match
        steps = []
        in_steps = False
        for ln in lines:
            if re.match(r"^(Step|\d+\.)", ln.strip(), re.I):
                in_steps = True
            if in_steps and "|" in ln:
                parts = ln.split("|")
                steps.append({"action": parts[0].strip(), "expected": parts[1].strip() if len(parts) > 1 else ""})
        test_cases.append(GeneratedTestCase(
            title=title,
            preconditions=preconditions,
            steps=steps,
            priority=priority,
            tags=tags,
        ))
    return test_cases
