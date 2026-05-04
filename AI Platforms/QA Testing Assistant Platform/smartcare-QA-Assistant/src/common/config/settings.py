"""
SmartCare QA Assistant — Application Settings
Loads all configuration from environment variables (or Key Vault-backed .env).
"""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    app_name: str = "SmartCare QA Assistant"
    app_env: str = "development"          # development | staging | production
    log_level: str = "INFO"
    assistant_mode: str = "balanced"      # strict | balanced | relaxed
    retrieval_first_testcase: bool = True

    # ── Azure DevOps ─────────────────────────────────────────────────────────
    ado_org_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ADO_ORG_URL", "ADO_ORG"),
    )
    ado_project: str | None = None         # e.g. SmartCare
    ado_pat: str | None = None             # Personal Access Token (from Key Vault)
    ado_api_version: str = "7.1"

    # ── Azure AI Foundry / GPT-4o ────────────────────────────────────────────
    ai_foundry_endpoint: str | None = None  # e.g. https://<hub>.openai.azure.com/ or https://<hub>.services.ai.azure.com/anthropic/
    ai_foundry_api_key: str | None = None
    ai_foundry_deployment: str = Field(
        default="gpt-4o",
        validation_alias=AliasChoices("AI_FOUNDRY_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
    )
    ai_foundry_api_version: str = "2024-02-01"
    ai_max_tokens: int = 4096
    ai_temperature: float = 0.2

    # ── Microsoft Presidio (PHI Sanitization) ────────────────────────────────
    presidio_backend: str = "remote"                # local | remote
    presidio_analyzer_url: str = "http://localhost:3000"   # container endpoint
    presidio_anonymizer_url: str = "http://localhost:3001"

    # ── Azure Key Vault ───────────────────────────────────────────────────────
    key_vault_url: str | None = None       # e.g. https://smartcare-kv.vault.azure.net/

    # ── Azure Blob Storage ────────────────────────────────────────────────────
    blob_account_url: str | None = None
    blob_container_generated: str = "generated-artifacts"

    # ── Azure Cache for Redis ─────────────────────────────────────────────────
    redis_host: str | None = None
    redis_port: int = 6380
    redis_password: str | None = None
    redis_ssl: bool = True
    redis_ttl_seconds: int = 3600          # 1 hour default cache TTL

    # ── Azure Cosmos DB ───────────────────────────────────────────────────────
    cosmos_endpoint: str | None = None
    cosmos_key: str | None = None
    cosmos_database: str = "smartcare_qa"
    cosmos_container_chat: str = "chat_history"
    cosmos_container_prompts: str = "prompt_templates"

    # ── Azure SQL Database ────────────────────────────────────────────────────
    sql_connection_string: str | None = None  # ODBC / SQLAlchemy connection string

    # ── Azure AI Search ───────────────────────────────────────────────────────
    ai_search_endpoint: str | None = None
    ai_search_api_key: str | None = None
    ai_search_index_testcases: str = "testcases-index"

    # ── Azure Service Bus ─────────────────────────────────────────────────────
    service_bus_connection_string: str | None = None
    service_bus_queue_generation: str = "generation-requests"

    # ── Azure Application Insights ────────────────────────────────────────────
    appinsights_connection_string: str | None = None

    # ── Entra ID / Auth ───────────────────────────────────────────────────────
    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_client_secret: str | None = None
    entra_scopes: str = "https://graph.microsoft.com/.default"

    # ── Azure Content Safety ──────────────────────────────────────────────────
    content_safety_endpoint: str | None = None
    content_safety_api_key: str | None = None

    # ── APIM ──────────────────────────────────────────────────────────────────
    apim_base_url: str = ""               # set in staging/prod

    def has_values(self, *values: str | None) -> bool:
        return all(bool(value and value.strip()) for value in values)

    def missing_env(self, env_var_names: list[str]) -> list[str]:
        return [name for name in env_var_names if not getattr(self, name.lower(), None)]

    @property
    def feature_requirements(self) -> dict[str, list[str]]:
        phi_requirements: list[str] = []
        if self.presidio_backend.strip().lower() == "remote":
            phi_requirements = ["PRESIDIO_ANALYZER_URL", "PRESIDIO_ANONYMIZER_URL"]

        return {
            "ado_fetch": ["ADO_ORG_URL", "ADO_PROJECT", "ADO_PAT"],
            "phi_sanitization": phi_requirements,
            "ai_generation": [
                "AI_FOUNDRY_ENDPOINT",
                "AI_FOUNDRY_API_KEY",
                "AI_FOUNDRY_DEPLOYMENT",
            ],
            "content_safety": ["CONTENT_SAFETY_ENDPOINT", "CONTENT_SAFETY_API_KEY"],
            "blob_storage": ["BLOB_ACCOUNT_URL", "BLOB_CONTAINER_GENERATED"],
            "redis_cache": ["REDIS_HOST", "REDIS_PASSWORD"],
            "cosmos_memory": ["COSMOS_ENDPOINT", "COSMOS_KEY"],
            "sql_audit": ["SQL_CONNECTION_STRING"],
            "ai_search": ["AI_SEARCH_ENDPOINT", "AI_SEARCH_API_KEY"],
            "service_bus": ["SERVICE_BUS_CONNECTION_STRING"],
            "entra_auth": ["ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"],
            "observability": ["APPINSIGHTS_CONNECTION_STRING"],
        }

    def feature_status(self) -> dict[str, dict[str, object]]:
        status: dict[str, dict[str, object]] = {}
        for feature_name, env_vars in self.feature_requirements.items():
            missing = self.missing_env(env_vars)
            status[feature_name] = {
                "configured": not missing,
                "missing": missing,
            }
        status["basic_app"] = {"configured": True, "missing": []}
        status["basic_ai_pipeline"] = {
            "configured": status["phi_sanitization"]["configured"] and status["ai_generation"]["configured"],
            "missing": sorted(
                set(
                    status["phi_sanitization"]["missing"]
                    + status["ai_generation"]["missing"]
                )
            ),
        }
        status["full_pipeline"] = {
            "configured": all(item["configured"] for item in status.values()),
            "missing": sorted(
                {
                    env_var
                    for item in status.values()
                    for env_var in item["missing"]
                }
            ),
        }
        return status


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
