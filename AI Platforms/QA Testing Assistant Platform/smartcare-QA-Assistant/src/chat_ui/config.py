from __future__ import annotations

from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
LANDING_LOGO_PATH = ROOT_DIR / "Streamline_Logo_Gradient.jpg"
UPLOAD_CONTEXT_DIR = ROOT_DIR / ".chat_context_uploads"
UPLOAD_CONTEXT_DB_PATH = ROOT_DIR / ".chat_context_store.sqlite3"

ALLOWED_UPLOAD_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".docx", ".pdf"}
MAX_FILE_SIZE_BYTES = 5_000_000
MAX_FILES_PER_SESSION = 5
MAX_CONTEXT_CHARS = 150_000
MAX_CONTEXT_CHARS_PER_FILE = max(20_000, MAX_CONTEXT_CHARS // max(1, MAX_FILES_PER_SESSION))

DEFAULT_TICKET_EXPORT_CSV = ROOT_DIR / "poc_ado_query_results.csv"
DEFAULT_FEATURE_MODULE_CSV = ROOT_DIR / "msp_feature_module_mapping.csv"
DEFAULT_MODULE_SUMMARY_CSV = ROOT_DIR / "bug_ticket_feature_module_recurrence_summary.csv"
DEFAULT_FUNCTIONALITY_SUMMARY_CSV = ROOT_DIR / "bug_ticket_functionality_summary.csv"
DEFAULT_RECURRENCE_OUTPUT_CSV = ROOT_DIR / "probable_recurrence_candidates.csv"
DEFAULT_RECENT_WINDOW_DAYS = 60
DEFAULT_MSP_SHEET_NAME = "6.0_1-AprilMSP_2026"
DEFAULT_MSP_TARGET_CATEGORY = "Engineering Improvement Initiatives- NBL(I)"

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_ML_MODEL_DIR = ROOT_DIR / "artifacts" / "ml_model"
