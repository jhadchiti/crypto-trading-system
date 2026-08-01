"""
Local secrets loader.
=====================

Loads KEY=VALUE pairs from `secrets.env` (same folder) into os.environ,
WITHOUT overwriting variables that are already set. Imported at the top of
every script that needs credentials, so the keys work identically from a
terminal, from Task Scheduler, and after reboots.

secrets.env format (one per line, no quotes needed):

    BINANCE_API_KEY=abc123...
    BINANCE_API_SECRET=def456...
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

Security notes:
  - secrets.env is in .gitignore — it must NEVER be committed.
  - Plaintext on disk is equivalent exposure to User-level env vars
    (any process running as this user can read either). Acceptable because
    the API key is Reading+Futures only: no withdrawals, no transfers.
  - Do not put this file in shared backups or cloud sync folders.
"""

from __future__ import annotations

import os
from pathlib import Path

SECRETS_FILE = Path(__file__).resolve().parent / "secrets.env"


def load() -> int:
    """Load secrets.env into os.environ (existing vars win). Returns count."""
    if not SECRETS_FILE.exists():
        return 0
    n = 0
    try:
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and not os.environ.get(key):
                os.environ[key] = value
                n += 1
    except Exception:
        pass
    return n


# auto-load on import
load()
