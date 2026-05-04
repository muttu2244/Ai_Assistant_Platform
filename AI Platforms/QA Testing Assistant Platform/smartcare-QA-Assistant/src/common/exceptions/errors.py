"""
SmartCare QA Assistant — Custom Exceptions
"""


class SmartCareBaseError(Exception):
    """Base for all SmartCare errors."""
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


# ── PHI / HIPAA ───────────────────────────────────────────────────────────────

class PhiSanitizationError(SmartCareBaseError):
    """Raised when Presidio fails to sanitize input."""


class UnsanitizedDataError(SmartCareBaseError):
    """Raised when raw (potentially PHI-bearing) data is about to cross the HIPAA boundary."""


# ── Azure DevOps ──────────────────────────────────────────────────────────────

class AdoClientError(SmartCareBaseError):
    """Raised when the ADO REST API call fails."""


class AdoItemNotFoundError(AdoClientError):
    """Raised when a requested work item / test case does not exist in ADO."""


class AdoAuthenticationError(AdoClientError):
    """Raised when the ADO PAT is invalid or expired."""


# ── AI / LLM ──────────────────────────────────────────────────────────────────

class AiEngineError(SmartCareBaseError):
    """Generic error from the Azure AI Foundry / GPT-4o call."""


class AiRateLimitError(AiEngineError):
    """Raised when GPT-4o returns a 429 response."""


class AiContentFilterError(AiEngineError):
    """Raised when Azure Content Safety blocks the request/response."""


# ── Storage ───────────────────────────────────────────────────────────────────

class BlobStorageError(SmartCareBaseError):
    """Raised on Azure Blob Storage failures."""


class CacheError(SmartCareBaseError):
    """Raised on Redis cache failures (non-fatal — callers should gracefully degrade)."""


class CosmosDbError(SmartCareBaseError):
    """Raised on Cosmos DB read/write failures."""


class SqlDbError(SmartCareBaseError):
    """Raised on Azure SQL failures."""


class AiSearchError(SmartCareBaseError):
    """Raised when Azure AI Search indexing or query fails."""


# ── Auth / Auth ───────────────────────────────────────────────────────────────

class AuthenticationError(SmartCareBaseError):
    """Raised when Entra ID token validation fails."""


class AuthorizationError(SmartCareBaseError):
    """Raised when the user lacks RBAC permissions for the requested action."""


# ── Messaging ─────────────────────────────────────────────────────────────────

class ServiceBusError(SmartCareBaseError):
    """Raised when publishing to or consuming from Service Bus fails."""


# ── Configuration ─────────────────────────────────────────────────────────────

class ConfigurationError(SmartCareBaseError):
    """Raised when a required configuration value is missing or invalid."""
