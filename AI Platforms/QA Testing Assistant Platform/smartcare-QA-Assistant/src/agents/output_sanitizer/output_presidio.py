"""
SmartCare QA Assistant — STEP 4: Output Sanitizer
Second Presidio pass on LLM-generated output before it reaches the user.
"""

from __future__ import annotations

from src.agents.phi_sanitizer.presidio_analyzer import PhiSanitizer


class OutputSanitizer:
    """
    Thin wrapper that applies Presidio sanitization to LLM-generated output.
    Reuses the same PhiSanitizer logic so both inbound and outbound data
    are scrubbed through the same HIPAA gate.
    """

    def __init__(self) -> None:
        self._sanitizer = PhiSanitizer()

    async def sanitize(self, text: str) -> str:
        """Sanitize raw GPT-4o output before returning it to the user."""
        return await self._sanitizer.sanitize_output(text)
