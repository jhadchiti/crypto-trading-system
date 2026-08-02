"""
Daily automated check — single entry point for the scheduled task.
====================================================================

Runs the operational routine in sequence:
  1. signal_alerter.py  — fires Discord/email/log if action needed
  2. dashboard.py       — refreshes dashboard.html

Logs all stdout/stderr to ./automation.log so you can audit what the
scheduled task did. If either step fails, the failure is logged and the
script continues (the dashboard refresh shouldn't be blocked by a transient
alerter failure).

This script is designed to be invoked by Windows Task Scheduler. See
setup_automation.ps1 for one-shot scheduled-task registration.

Run manually (e.g., to verify before scheduling):
    python daily_check.py
"""

from __future__ import annotations

import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


import secrets_local  # loads secrets.env into os.environ
LOG_FILE = Path("automation.log")
HERE = Path(__file__).resolve().parent
PYTHON = sys.executable   # use whichever python invoked this script


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_step(name: str, script: str) -> bool:
    log(f"START {name} ({script})")
    try:
        result = subprocess.run(
            [PYTHON, str(HERE / script)],
            cwd=str(HERE),
            capture_output=True,
            text=True,
            timeout=600,   # 10-min cap
        )
        if result.stdout:
            for line in result.stdout.rstrip().splitlines():
                log(f"  {name}: {line}")
        if result.stderr:
            for line in result.stderr.rstrip().splitlines():
                log(f"  {name} STDERR: {line}")
        if result.returncode != 0:
            log(f"FAIL {name} (exit {result.returncode})")
            return False
        log(f"OK {name}")
        return True
    except subprocess.TimeoutExpired:
        log(f"FAIL {name} (timeout after 10 min)")
        return False
    except Exception as e:
        log(f"FAIL {name}: {e}")
        log(traceback.format_exc())
        return False


def wait_for_network(max_wait_s: int = 900) -> bool:
    """After wake-from-sleep the task can fire before Wi-Fi/VPN are up.
    Wait until Binance futures is reachable (max 15 min), else give up."""
    import urllib.request
    deadline = __import__("time").time() + max_wait_s
    attempt = 0
    while __import__("time").time() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(
                "https://fapi.binance.com/fapi/v1/ping",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status in (200, 451):   # 451 = reachable but geo-blocked (VPN down)
                    if r.status == 451:
                        log("  network up but 451 — VPN likely not connected yet; waiting ...")
                    else:
                        if attempt > 1:
                            log(f"  network ready after {attempt} attempts")
                        return True
        except Exception:
            pass
        __import__("time").sleep(30)
    log("  network/VPN never became ready — aborting this run")
    return False


def main():
    log("=" * 70)
    log("daily_check starting")
    log(f"  python:  {PYTHON}")
    log(f"  cwd:     {HERE}")

    if not wait_for_network():
        log("daily_check aborted (no network) — will retry at next schedule")
        sys.exit(1)

    # Run alerter — fires Discord/email/log if there's something actionable
    alerter_ok = run_step("alerter", "signal_alerter.py")

    # Executor — automated trade execution (rails + kill-file inside).
    # Runs only if API keys are configured.
    import os as _os2
    if _os2.environ.get("BINANCE_API_KEY"):
        executor_ok = run_step("executor", "executor.py")
    else:
        executor_ok = True
        log("SKIP executor (BINANCE_API_KEY not set)")

    # Run dashboard regardless — gives you a fresh HTML even if alerter failed
    dashboard_ok = run_step("dashboard", "dashboard.py")

    # Funding-carry paper monitor (FUNDING_CARRY_SPEC.md §9 step 2).
    # Non-critical: a failure here never blocks the exit code.
    carry_ok = run_step("carry_monitor", "funding_carry_monitor.py")

    # Token-unlock shorts paper tracker (UNLOCK_SHORTS_SPEC.md). Advisory.
    run_step("unlock_tracker", "unlock_tracker.py")

    # Cost-of-discipline audit (blocked-signal receipts). Advisory.
    run_step("discipline_audit", "discipline_audit.py")

    # Account sync (read-only; runs only if API keys are configured).
    # Advisory: cross-checks exchange positions vs live_trades.csv.
    import os as _os
    if _os.environ.get("BINANCE_API_KEY"):
        sync_ok = run_step("account_sync", "account_sync.py")
    else:
        sync_ok = True
        log("SKIP account_sync (BINANCE_API_KEY not set)")

    # Daily digest to Discord (dashboard summary + html attachment). Advisory.
    run_step("digest", "dashboard_digest.py")

    log(f"daily_check complete (alerter={'OK' if alerter_ok else 'FAIL'}, "
        f"executor={'OK' if executor_ok else 'FAIL'}, "
        f"dashboard={'OK' if dashboard_ok else 'FAIL'}, "
        f"carry={'OK' if carry_ok else 'FAIL'}, "
        f"sync={'OK' if sync_ok else 'FAIL'})")

    # Exit code reflects overall health for the scheduler (carry is advisory)
    sys.exit(0 if (alerter_ok and dashboard_ok) else 1)


if __name__ == "__main__":
    main()
