"""Loads config.yaml and .env."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("AUTOAPPLY_DATA", ROOT / "data"))
DATA.mkdir(exist_ok=True)
(DATA / "screenshots").mkdir(exist_ok=True)
(DATA / "letters").mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")


def config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def login_for(prefix: str) -> tuple[str, str]:
    """Login saved in the dashboard first, then .env as a fallback."""
    from .vault import get_login
    email, pw = get_login(prefix)
    if email and pw:
        return email, pw
    return env(f"{prefix}_EMAIL"), env(f"{prefix}_PASSWORD")
