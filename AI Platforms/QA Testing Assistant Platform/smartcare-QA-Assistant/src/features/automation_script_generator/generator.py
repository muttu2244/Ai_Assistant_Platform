"""
SmartCare QA Assistant — Automation Script Generator Feature
Generates Selenium Java (JUnit 5 + POM) scripts from a test case description.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from src.agents.ai_engine.foundry_client import FoundryClient
from src.agents.output_sanitizer.output_presidio import OutputSanitizer
from src.common.models.schemas import GeneratedAutomationScript

logger = logging.getLogger(__name__)


class AutomationScriptGenerator:
    """Generates Selenium Java automation scripts from a test case."""

    def __init__(self) -> None:
        self._ai = FoundryClient()
        self._output_sanitizer = OutputSanitizer()

    async def generate(
        self,
        test_case_description: str,
        ado_context: str = "",
        conversation_history: Optional[list[dict]] = None,
    ) -> tuple[str, GeneratedAutomationScript]:
        """
        Generate a Selenium Java automation script.

        Args:
            test_case_description: Sanitized test case title + steps.
            ado_context: Optional sanitized ADO context.
            conversation_history: Prior conversation turns.

        Returns:
            (raw_text_response, GeneratedAutomationScript)
        """
        prompt = _build_automation_prompt(test_case_description)

        raw_response = await self._ai.generate(
            user_prompt=prompt,
            feature="automation_script",
            context=ado_context,
            conversation_history=conversation_history,
        )

        safe_response = await self._output_sanitizer.sanitize(raw_response)
        script = _parse_script(safe_response)
        logger.info("Automation script generator produced class: %s", script.class_name)
        return safe_response, script


def _build_automation_prompt(description: str) -> str:
    return f"""Generate a complete Selenium Java automation script for the following test case:

{description}

Requirements:
- JUnit 5 + Selenium 4 + WebDriverManager
- Page Object Model (POM) — separate the page class and test class
- Use ChromeDriver by default
- Read base URL from system property 'base.url' (default: http://localhost)
- Include @BeforeEach for driver setup and @AfterEach for teardown
- Add meaningful assertions (assertEquals, assertTrue, etc.)
- Comment each logical section

Return ONLY the Java code, no extra prose."""


def _parse_script(text: str) -> GeneratedAutomationScript:
    class_match = re.search(r"public class (\w+)", text)
    class_name = class_match.group(1) if class_match else "SmartCareTest"
    return GeneratedAutomationScript(
        language="Java",
        framework="Selenium",
        class_name=class_name,
        code=text,
    )
