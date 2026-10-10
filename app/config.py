"""Shared configuration, loaded independently of the working directory."""
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

APP_DIR = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(APP_DIR.parent / ".cache" / "huggingface"))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
load_dotenv(APP_DIR / ".env")
load_dotenv(APP_DIR.parent / ".env")


def get_model():
    return os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def credentials_configured():
    key = os.getenv("GROQ_API_KEY", "").strip()
    return bool(key and key not in {"your_actual_groq_api_key", "your_groq_api_key_here"})


@lru_cache(maxsize=1)
def get_groq_client():
    if not credentials_configured():
        raise ValueError("Add your GROQ_API_KEY to app/.env before asking questions.")
    return Groq()
