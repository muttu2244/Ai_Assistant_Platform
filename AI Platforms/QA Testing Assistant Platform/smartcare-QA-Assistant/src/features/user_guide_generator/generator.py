"""
SmartCare QA Assistant — User Guide Generator Feature
Generates plain-English user guides from ADO requirements / user stories.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.agents.ai_engine.foundry_client import FoundryClient
from src.agents.output_sanitizer.output_presidio import OutputSanitizer
from src.common.models.schemas import GeneratedUserGuide

logger = logging.getLogger(__name__)


class UserGuideGenerator:
    """Generates end-user documentation from requirements."""

    def __init__(self) -> None:
        self._ai = FoundryClient()
        self._output_sanitizer = OutputSanitizer()

    async def generate(
        self,
        requirements: str,
        ado_context: str = "",
        conversation_history: Optional[list[dict]] = None,
    ) -> tuple[str, GeneratedUserGuide]:
        """
        Generate a user guide from a requirements description.

        Args:
            requirements: Sanitized requirements or feature description.
            ado_context: Optional sanitized ADO context.
            conversation_history: Prior conversation turns.

        Returns:
            (raw_text_response, GeneratedUserGuide)
        """
        prompt = _build_user_guide_prompt(requirements)

        raw_response = await self._ai.generate(
            user_prompt=prompt,
            feature="user_guide",
            context=ado_context,
            conversation_history=conversation_history,
        )

        safe_response = await self._output_sanitizer.sanitize(raw_response)
        guide = _parse_user_guide(safe_response)
        logger.info("User guide generator produced %d sections.", len(guide.sections))
        return safe_response, guide


def _build_user_guide_prompt(requirements: str) -> str:
    return f"""Write a user guide for healthcare staff based on the following feature requirements:

{requirements}

The guide must include:
1. Overview — what this feature does and who uses it
2. Prerequisites — what users need before starting
3. Step-by-step Instructions — numbered steps with screenshots callouts (e.g. [Screenshot: X])
4. Tips and Best Practices
5. Troubleshooting — 3-5 common issues and solutions

Use plain English. Avoid technical jargon. All steps must be actionable."""


def _parse_user_guide(text: str) -> GeneratedUserGuide:
    import re
    sections: list[dict[str, str]] = []
    # Split on numbered headings like "1. Overview" or "## Overview"
    heading_pattern = re.compile(r"(?:^|\n)(?:#+\s*|\d+\.\s+)([^\n]+)", re.M)
    headings = list(heading_pattern.finditer(text))
    if not headings:
        sections.append({"heading": "Content", "body": text.strip()})
    else:
        for i, match in enumerate(headings):
            start = match.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            body = text[start:end].strip()
            sections.append({"heading": match.group(1).strip(), "body": body})

    title_match = re.search(r"(?:User Guide|Guide)(?:\s+for\s+([^\n]+))?", text[:200], re.I)
    title = title_match.group(0).strip() if title_match else "User Guide"
    return GeneratedUserGuide(title=title, sections=sections)
