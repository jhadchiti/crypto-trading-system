# RECOVERY.md — dead laptop to trading again in ~1 hour

*The disaster runbook. Test it mentally every quarter; test it for real if you
ever get a new machine. Prerequisites you must be able to produce from
OUTSIDE the dead laptop: GitHub login · Binance login · the latest
`crypto_state_*.zip` from the SECOND backup location · NordVPN login.*

## Step 1 — Base software (15 min)
1. Install Python 3.11+ (python.org, check "Add to PATH")
2. Install Git (git-scm.com)
3. Install NordVPN, log in, connect to Switzerland, enable auto-connect + kill switch

## Step 2 — Code (5 min)
```powershell
cd C:\Users\<you>\Documents\Claude\Projects
git clone https://github.com/jhadchiti/crypto-trading-system.git Crypto
cd Crypto
pip install pandas numpy requests --break-system-packages
```

## Step 3 — State (5 min)
Unzip the newest `crypto_state_*.zip` INTO the Crypto folder (overwrite all).
This restores: live_trades, ledger, journal, equity history, paper trials,
executor/alerter state, universe, logs.

## Step 4 — Secrets (10 min)
Create `secrets.env` in the folder (3 lines, no quotes):
```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
DISCORD_WEBHOOK_URL=...
```
- API key: Binance -> API Management. If the old key is lost, DELETE it there
  and create a new one: **Enable Reading + Enable Futures ONLY.**
- Webhook: Discord channel -> Edit Channel -> Integrations -> Webhooks.

## Step 5 — Scheduled task (5 min)
```powershell
powershell -ExecutionPolicy Bypass -File setup_automation.ps1
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RunOnlyIfNetworkAvailable
Set-ScheduledTask -TaskName "CryptoDailyCheck" -Settings $settings
```

## Step 6 — Verify (10 min)
```powershell
curl.exe https://ipinfo.io/country        # must NOT be a blocked region
python executor.py --selftest             # futures wallet + filters + keys
python daily_check.py                     # expect all steps OK
python backup_state.py                    # first backup on the new machine
```
Then: `Start-ScheduledTask -TaskName "CryptoDailyCheck"` and confirm the log
line ends all-OK from the scheduler's own context.

## While you were down
- Open positions were PROTECTED the whole time: stops live on Binance's
  servers, not the laptop.
- Entries/exits simply paused. On restart the system reconciles reality via
  account_sync (UNTRACKED/GHOST warnings if anything moved).
- If a stop filled during the outage, the executor detects the missing
  position and records the exit on first run.

## If the laptop is dead AND you have no backup archive
Track record and journals are lost (this is why backup_state runs nightly and
why the second location must be a different disk). The SYSTEM survives:
code from GitHub, positions/balances from Binance itself. Restart the
evidence from zero rather than reconstructing from memory.
