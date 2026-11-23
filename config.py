import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ENV_FILE, override=True)
except ImportError:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ[key.strip()] = val.strip().strip("'\"")


def parse_admin_id(raw: str | None) -> int | None:
    """Parse a single Telegram user ID into an int, or None if empty/invalid."""
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_admin_username(raw: str | None) -> str | None:
    """Parse a Telegram username (without '@' and lowercased), or None if empty."""
    if not raw:
        return None
    cleaned = raw.strip().lstrip("@").strip().lower()
    return cleaned or None


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID: int | None = parse_admin_id(os.getenv("ADMIN_ID", ""))
ADMIN_USERNAME: str | None = parse_admin_username(os.getenv("ADMIN_USERNAME", ""))
DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "contest.db")).strip()
