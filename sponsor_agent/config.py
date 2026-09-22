"""Environment-driven settings. Nothing secret lives in this file."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(env_key: str, default: str) -> Path:
    p = Path(os.getenv(env_key) or default)
    return p if p.is_absolute() else ROOT / p


WORKBOOK = _path("WORKBOOK_PATH", "data/outreach.xlsx")
INPUT_FILE = _path("INPUT_FILE", "data/prospects_input.csv")
FACT_SHEET = _path("FACT_SHEET_PATH", "config/fact_sheet.json")
GMAIL_CREDENTIALS = _path("GMAIL_CREDENTIALS", "config/credentials.json")
GMAIL_TOKEN = _path("GMAIL_TOKEN", "config/token.json")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

SEND_CAP = int(os.getenv("SEND_CAP", "25"))
MAX_VALIDATION_RETRIES = int(os.getenv("MAX_VALIDATION_RETRIES", "2"))
FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "5"))

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}

# --- LLM provider ----------------------------------------------------------
# "ollama" (default, local, no API key) or "anthropic" (fallback, needs a key).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()

OLLAMA_HOST = (os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")

# No default model name on purpose: whatever is set must actually be pulled,
# and if nothing is set the first installed model is used. See llm.resolve_model.
OLLAMA_DRAFT_MODEL = (os.getenv("OLLAMA_DRAFT_MODEL") or "").strip()

# Falls back to the drafting model when unset. A larger, slower model here is
# a reasonable trade: validation runs once per draft and catches fabrication.
OLLAMA_VALIDATE_MODEL = (os.getenv("OLLAMA_VALIDATE_MODEL") or "").strip() or OLLAMA_DRAFT_MODEL
