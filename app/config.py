"""Environment-driven settings. Read at call time so tests can override them."""
import os

from dotenv import load_dotenv

load_dotenv()


def supabase_url() -> str:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL is not set")
    return url


def app_env() -> str:
    """'dev' enables dangerous admin endpoints. Anything else is treated as production."""
    return (os.getenv("APP_ENV") or "prod").lower()


def app_base_url() -> str:
    return (os.getenv("APP_BASE_URL") or "http://localhost:8000").rstrip("/")


INVITE_TTL_DAYS = 7
