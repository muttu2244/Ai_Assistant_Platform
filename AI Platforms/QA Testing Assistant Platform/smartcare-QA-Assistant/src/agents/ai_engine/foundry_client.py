"""
SmartCare QA Assistant — STEP 3: Azure AI Foundry / GPT-4o Client
Only ever receives SANITIZED data. PHI must have been stripped by STEP 2 first.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.common.config.settings import get_settings
from src.common.exceptions.errors import AiContentFilterError, AiEngineError, AiRateLimitError
from src.common.utils.helpers import truncate_text

logger = logging.getLogger(__name__)


def _load_anthropic_foundry() -> type[Any]:
    try:
        anthropic_module = importlib.import_module("anthropic")
    except ImportError as exc:
        raise AiEngineError(
            "Anthropic support is not installed. Install the 'anthropic' package or use an Azure OpenAI endpoint.",
            str(exc),
        ) from exc

    client_class = getattr(anthropic_module, "AnthropicFoundry", None)
    if client_class is None:
        raise AiEngineError(
            "Installed anthropic package does not expose AnthropicFoundry. Upgrade to a compatible version or switch providers.",
            anthropic_module.__file__ or "anthropic",
        )

    return client_class


def _load_default_azure_credential() -> type[Any]:
    try:
        azure_identity = importlib.import_module("azure.identity")
    except ImportError as exc:
        raise AiEngineError(
            "Azure Identity support is not installed. Install 'azure-identity' to use Foundry authentication.",
            str(exc),
        ) from exc

    credential_class = getattr(azure_identity, "DefaultAzureCredential", None)
    if credential_class is None:
        raise AiEngineError(
            "Installed azure.identity package does not expose DefaultAzureCredential.",
            azure_identity.__file__ or "azure.identity",
        )

    return credential_class


def _load_openai_sdk() -> tuple[type[Any], type[Exception], type[Exception]]:
    try:
        openai_module = importlib.import_module("openai")
    except ImportError as exc:
        raise AiEngineError(
            "OpenAI SDK is not installed. Install 'openai' to use Azure OpenAI deployments.",
            str(exc),
        ) from exc

    async_client = getattr(openai_module, "AsyncAzureOpenAI", None)
    api_status_error = getattr(openai_module, "APIStatusError", None)
    rate_limit_error = getattr(openai_module, "RateLimitError", None)
    if not all((async_client, api_status_error, rate_limit_error)):
        raise AiEngineError(
            "Installed openai package is missing Azure OpenAI client symbols. Upgrade to a compatible version.",
            openai_module.__file__ or "openai",
        )

    return async_client, api_status_error, rate_limit_error

# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_BASE_STRICT = """
You are SmartCare QA Assistant, an expert AI assistant for healthcare software QA teams.
You help with test case generation, automation script creation, and user guide writing.
Prefer healthcare QA scope; if a request is unrelated, briefly redirect to healthcare QA support.
You ONLY work with sanitized, non-PHI data. Never include real patient names, IDs, or health data.
Be precise, structured, and output clean professional content suitable for healthcare QA.
""".strip()

_SYSTEM_PROMPT_BASE_BALANCED = """
You are SmartCare QA Assistant, an expert AI assistant for healthcare software QA teams.
You help with test case generation, automation script creation, and user guide writing.
You can answer general user questions directly, while prioritizing healthcare QA assistance when relevant.
You ONLY work with sanitized, non-PHI data. Never include real patient names, IDs, or health data.
Be precise, structured, and output clean professional content suitable for healthcare QA.
""".strip()

_SYSTEM_PROMPT_BASE_RELAXED = """
You are SmartCare QA Assistant, an expert AI assistant for healthcare software QA teams.
You help with test case generation, automation script creation, and user guide writing.
You can answer both healthcare QA and general questions in a helpful, conversational way.
You ONLY work with sanitized, non-PHI data. Never include real patient names, IDs, or health data.
Be precise, structured, and output clean professional content suitable for healthcare QA.
""".strip()


def _normalize_assistant_mode(mode: str | None) -> str:
    normalized = (mode or "balanced").strip().lower()
    if normalized in {"strict", "balanced", "relaxed"}:
        return normalized
    return "balanced"


def _build_system_prompts(mode: str) -> dict[str, str]:
    if mode == "strict":
        base_prompt = _SYSTEM_PROMPT_BASE_STRICT
    elif mode == "relaxed":
        base_prompt = _SYSTEM_PROMPT_BASE_RELAXED
    else:
        base_prompt = _SYSTEM_PROMPT_BASE_BALANCED

    return {
        "test_case": base_prompt + """

When generating test cases:
- Use Given/When/Then or Action/Expected step formats.
- Include preconditions, positive, negative, and edge cases.
- Number all steps clearly.
- Set priority (1=Critical, 2=High, 3=Medium, 4=Low).
- Suggest tags like [smoke], [regression], [negative], [accessibility].
""".strip(),
        "automation_script": base_prompt + """

When generating Selenium Java automation scripts:
- Use Page Object Model (POM) pattern.
- Use JUnit 5 + Selenium 4 + WebDriverManager.
- Add meaningful assertions with clear error messages.
- Include @BeforeEach/@AfterEach setup/teardown.
- Comment each logical section clearly.
- Do NOT hardcode URLs or credentials — use config/properties files.
""".strip(),
        "user_guide": base_prompt + """

When generating user guides:
- Use clear, plain English suitable for non-technical healthcare staff.
- Structure with Overview, Prerequisites, Step-by-step Instructions, Tips, Troubleshooting.
- Use numbered steps and bullet points.
- Avoid jargon. Define acronyms on first use.
""".strip(),
        "default": base_prompt,
    }


class FoundryClient:
    """
    Async wrapper around Azure AI Foundry model deployments.
    Accepts SANITIZED context only. Raises AiEngineError on failure.
    Supports both API key auth and Entra ID (DefaultAzureCredential) fallback.
    """

    def __init__(self) -> None:
        s = get_settings()
        if not s.ai_foundry_endpoint:
            raise AiEngineError("AI Foundry endpoint not configured — set AI_FOUNDRY_ENDPOINT.")
        self._assistant_mode = _normalize_assistant_mode(getattr(s, "assistant_mode", "balanced"))
        self._system_prompts = _build_system_prompts(self._assistant_mode)
        endpoint = (s.ai_foundry_endpoint or "").strip()
        deployment = (s.ai_foundry_deployment or "").strip()

        self._provider = "anthropic" if self._is_anthropic_config(endpoint, deployment) else "openai"
        self._credential: Any | None = None

        configured_api_key = s.ai_foundry_api_key if (s.ai_foundry_api_key and s.ai_foundry_api_key.strip() and s.ai_foundry_api_key != "YOUR_KEY") else None

        if self._provider == "anthropic":
            anthropic_foundry = _load_anthropic_foundry()
            base_url = self._normalize_anthropic_base_url(endpoint)
            if configured_api_key:
                logger.info("Using API key for Anthropic Foundry authentication")
                self._client = anthropic_foundry(api_key=configured_api_key, base_url=base_url)
            else:
                default_azure_credential = _load_default_azure_credential()
                self._credential = default_azure_credential()
                logger.info("Using Entra ID for Anthropic Foundry authentication")
                self._client = anthropic_foundry(
                    base_url=base_url,
                    azure_ad_token_provider=self._anthropic_token_provider,
                )
            self._anthropic_base_url = base_url
        else:
            async_azure_openai, self._openai_api_status_error, self._openai_rate_limit_error = _load_openai_sdk()
            openai_endpoint = self._normalize_openai_endpoint(endpoint)
            if configured_api_key:
                logger.info("Using API key for Azure OpenAI authentication")
                self._client = async_azure_openai(
                    azure_endpoint=openai_endpoint,
                    api_key=configured_api_key,
                    api_version=s.ai_foundry_api_version,
                )
            else:
                logger.info("Using Entra ID for Azure OpenAI authentication")
                default_azure_credential = _load_default_azure_credential()
                self._credential = default_azure_credential()

                def token_provider() -> str:
                    assert self._credential is not None
                    token = self._credential.get_token("https://cognitiveservices.azure.com/.default")
                    return token.token

                self._client = async_azure_openai(
                    azure_endpoint=openai_endpoint,
                    azure_ad_token_provider=token_provider,
                    api_version=s.ai_foundry_api_version,
                )
        
        self._deployment = s.ai_foundry_deployment
        self._max_tokens = s.ai_max_tokens
        self._temperature = s.ai_temperature
        logger.info("Assistant mode set to: %s", self._assistant_mode)

    @staticmethod
    def _is_anthropic_config(endpoint: str, deployment: str) -> bool:
        endpoint_l = endpoint.lower()
        deployment_l = deployment.lower()
        return "services.ai.azure.com" in endpoint_l or "/anthropic" in endpoint_l or deployment_l.startswith("claude")

    @staticmethod
    def _normalize_openai_endpoint(endpoint: str) -> str:
        cleaned = endpoint.strip().rstrip("/")
        for suffix in ("/openai/v1", "/openai"):
            if cleaned.lower().endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
                break
        return cleaned

    @staticmethod
    def _normalize_anthropic_base_url(endpoint: str) -> str:
        cleaned = endpoint.strip().rstrip("/")
        for suffix in ("/v1/messages", "/v1", "/messages"):
            if cleaned.lower().endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
                break
        if not cleaned.lower().endswith("/anthropic"):
            cleaned = cleaned + "/anthropic"
        return cleaned.rstrip("/") + "/"

    def _anthropic_token_provider(self) -> str:
        assert self._credential is not None
        token = self._credential.get_token("https://cognitiveservices.azure.com/.default")
        return token.token

    # ── Main generation call ──────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(AiRateLimitError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=5, max=60),
    )
    async def generate(
        self,
        user_prompt: str,
        feature: str = "default",
        conversation_history: list[dict[str, str]] | None = None,
        context: str = "",
    ) -> str:
        """
        Generate a response using GPT-4o.

        Args:
            user_prompt: The sanitized user request.
            feature: One of 'test_case', 'automation_script', 'user_guide', 'default'.
            conversation_history: Prior turns as [{"role": ..., "content": ...}].
            context: Optional sanitized ADO context to inject.
        Returns:
            The generated text response.
        """
        system_prompt = self._system_prompts.get(feature, self._system_prompts["default"])

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # Inject ADO context if provided
        if context:
            messages.append({
                "role": "system",
                "content": f"CONTEXT FROM AZURE DEVOPS (sanitized):\n{truncate_text(context, 6000)}",
            })

        # Prior conversation turns
        for turn in (conversation_history or []):
            messages.append({"role": turn["role"], "content": turn["content"]})

        # Current user message
        messages.append({"role": "user", "content": truncate_text(user_prompt, 4000)})

        if self._provider == "anthropic":
            return await self._generate_anthropic(messages)

        try:
            response = await self._client.chat.completions.create(
                model=self._deployment,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except self._openai_rate_limit_error as exc:
            logger.warning("Model rate limit hit — will retry: %s", exc)
            raise AiRateLimitError("Model rate limit", str(exc)) from exc
        except self._openai_api_status_error as exc:
            if exc.status_code == 400 and "content_filter" in str(exc).lower():
                raise AiContentFilterError("Azure Content Safety blocked this request.", str(exc)) from exc
            raise AiEngineError(f"Model API error {exc.status_code}", str(exc)) from exc
        except Exception as exc:
            raise AiEngineError("Unexpected model error", str(exc)) from exc

        reply = response.choices[0].message.content or ""
        logger.info(
            "OpenAI-style response: feature=%s tokens_used=%s",
            feature,
            response.usage.total_tokens if response.usage else "?",
        )
        return reply

    async def _generate_anthropic(self, messages: list[dict[str, Any]]) -> str:
        system_parts: list[str] = []
        anthropic_messages: list[dict[str, str]] = []
        for msg in messages:
            role = msg.get("role")
            content = str(msg.get("content", ""))
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                anthropic_messages.append({"role": role, "content": content})

        if not anthropic_messages:
            anthropic_messages = [{"role": "user", "content": "Hello"}]

        try:
            response = await asyncio.to_thread(
                self._client.messages.create,
                model=self._deployment,
                system="\n\n".join(system_parts),
                messages=anthropic_messages,
                max_tokens=min(self._max_tokens, 4096),
                temperature=self._temperature,
            )
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 401:
                raise AiEngineError("Anthropic API error 401: Check your Entra credentials or RBAC role on the Foundry resource", str(exc)) from exc
            if status_code == 403:
                raise AiEngineError(
                    "Anthropic API error 403: Access denied. Verify Foundry RBAC role assignment and model deployment permissions for this identity.",
                    str(exc),
                ) from exc
            if status_code is not None:
                raise AiEngineError(f"Anthropic API error {status_code}", str(exc)) from exc
            raise AiEngineError("Unexpected Anthropic error", str(exc)) from exc

        blocks = getattr(response, "content", []) or []
        parts: list[str] = []
        for block in blocks:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    # ── Convenience wrappers ──────────────────────────────────────────────────

    async def generate_test_cases(self, prompt: str, context: str = "") -> str:
        return await self.generate(prompt, feature="test_case", context=context)

    async def generate_automation_script(self, prompt: str, context: str = "") -> str:
        return await self.generate(prompt, feature="automation_script", context=context)

    async def generate_user_guide(self, prompt: str, context: str = "") -> str:
        return await self.generate(prompt, feature="user_guide", context=context)


# Keep legacy symbol for compatibility with existing imports.
Gpt4oClient = FoundryClient
