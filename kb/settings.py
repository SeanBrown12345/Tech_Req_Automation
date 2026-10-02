"""Paths and environment. Everything deployment-specific comes from env vars (or a local .env).

  KB_DATA_DIR   writable data folder: database, uploaded workbooks, dashboard-created profiles
                (default ./data; on Azure, point it at a mounted file share)
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)  # real environment variables win over .env

CONFIG_DIR = ROOT / "config"
SCALES_FILE = CONFIG_DIR / "scales.yaml"
REPO_PROFILES_DIR = CONFIG_DIR / "profiles"   # profiles kept in git; `file` relative to ROOT

DATA_DIR = Path(os.environ.get("KB_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "kb.sqlite"
UPLOADS_DIR = DATA_DIR / "workbooks"          # workbooks uploaded through the dashboard
DATA_PROFILES_DIR = DATA_DIR / "profiles"     # profiles created in the dashboard; `file` relative to DATA_DIR


def profile_locations() -> list[tuple[Path, Path]]:
    """(profile path, base dir its `file` is relative to) for every known profile."""
    found = [(p, ROOT) for p in sorted(REPO_PROFILES_DIR.glob("*.yaml"))]
    found += [(p, DATA_DIR) for p in sorted(DATA_PROFILES_DIR.glob("*.yaml"))]
    return found
