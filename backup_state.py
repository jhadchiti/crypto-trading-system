"""
State Backup — the track record must survive the laptop.
=========================================================

Code lives on GitHub; the EVIDENCE (trades, ledger, journal, equity history,
paper trials) is gitignored for privacy and therefore exists nowhere else.
This job zips all state files nightly into dated archives.

SECRETS ARE NEVER INCLUDED. Verify any archive: secrets.env must be absent.

Destination: ./state_backups/ always, PLUS an optional second location set in
backup_config.json:  {"second_location": "D:\\CryptoBackups"}
(Set it to a USB drive, second disk, or private encrypted folder. A backup on
the same dying disk is only half a backup.)

Retention: last 30 daily archives per location.

Usage:
    python backup_state.py            # run backup
    python backup_state.py --verify   # list newest archive's contents
"""

from __future__ import annotations

# UTF-8 console guard (post-mortems 2026-08-18 and 2026-09-13: cp1252 under
# Task Scheduler crashed digest, then alerter+executor ON FLIP MORNING.
# Console rendering must NEVER kill logic or delivery.)
import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

STATE_FILES = [
    "live_trades.csv", "live_trades_test_backup.csv",
    "sleeve_ledger.json", "equity_history.csv",
    "decision_journal.jsonl", "trade_reviews.csv",
    "unlock_events.json", "paper_carry_state.json", "paper_carry_log.csv",
    "account_state.json", "executor_state.json", "executor_config.json",
    "alerter_state.json", "last_dashboard_state.json",
    "discipline_audit.json", "mc_cone.json", "active_universe.json",
    "deployment_state.json", "alerts.log", "automation.log",
    "backup_config.json",
]
FORBIDDEN = {"secrets.env"}          # belt-and-suspenders: never archived
LOCAL_DIR = Path("state_backups")
KEEP = 30


def make_archive(dest_dir: Path) -> Path | None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = dest_dir / f"crypto_state_{stamp}.zip"
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in STATE_FILES:
            p = Path(name)
            if p.name in FORBIDDEN:
                continue
            if p.exists():
                z.write(p, p.name)
                n += 1
    if n == 0:
        out.unlink(missing_ok=True)
        return None
    # retention
    archives = sorted(dest_dir.glob("crypto_state_*.zip"))
    for old in archives[:-KEEP]:
        old.unlink(missing_ok=True)
    return out


def main():
    if "--verify" in sys.argv:
        archives = sorted(LOCAL_DIR.glob("crypto_state_*.zip"))
        if not archives:
            print("no archives yet"); return
        newest = archives[-1]
        with zipfile.ZipFile(newest) as z:
            names = z.namelist()
        print(f"{newest.name}: {len(names)} files, "
              f"{newest.stat().st_size/1024:.0f} KB")
        assert "secrets.env" not in names, "SECRETS IN ARCHIVE — INVESTIGATE"
        print("secrets.env absent: verified")
        for n in sorted(names):
            print(f"  {n}")
        return

    out = make_archive(LOCAL_DIR)
    if out is None:
        print("nothing to back up"); return
    print(f"backup: {out} ({out.stat().st_size/1024:.0f} KB)")

    # second location (strongly recommended: different physical disk)
    cfg = {}
    if Path("backup_config.json").exists():
        try:
            cfg = json.loads(Path("backup_config.json").read_text(encoding="utf-8"))
        except Exception:
            pass
    second = cfg.get("second_location")
    if second:
        try:
            out2 = make_archive(Path(second))
            print(f"second location: {out2}")
        except Exception as e:
            print(f"WARN: second location failed: {e}", file=sys.stderr)
    else:
        print("NOTE: no second_location configured (backup_config.json) — "
              "archives live on the same disk they protect against.")


if __name__ == "__main__":
    main()
