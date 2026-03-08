# Host Ollama + WSL Sidekick Setup

Optional setup guide for running Sidekick in WSL while Ollama runs on the Windows host.

Use this when you specifically want:

- better RAM headroom on the Windows side
- to avoid running the Ollama daemon inside WSL
- a clearer split between the app runtime and the LLM runtime

If WSL-local Ollama is already performing well enough, you do not need this setup.

For the full runtime decision guide, see [OLLAMA_RUNTIME_MODES.md](/home/ozzfa/sidekick/docs/OLLAMA_RUNTIME_MODES.md).

## Recommended Secure Mode (Localhost + Mirrored Networking)

This is the preferred security posture:
- Keep Ollama bound to localhost on Windows (`127.0.0.1:11434`)
- Use WSL mirrored networking so WSL can access host localhost directly
- Avoid exposing Ollama on `0.0.0.0` unless necessary

### A. Enable mirrored networking in Windows `.wslconfig`
Create or edit:
- `C:\Users\<YourUser>\.wslconfig`

Use:
```ini
[wsl2]
networkingMode=mirrored
localhostForwarding=true
```

Apply changes from Windows PowerShell:
```powershell
wsl --shutdown
```

Reopen WSL after shutdown.

### B. Keep Ollama loopback-only
In Windows PowerShell:
```powershell
setx OLLAMA_HOST "127.0.0.1:11434"
```

Restart Ollama app (quit tray app, reopen).

### C. Verify from WSL
```bash
curl http://127.0.0.1:11434/api/tags
```

If this works, set Sidekick `.env`:
```env
OLLAMA_HOST=http://127.0.0.1:11434
```

### D. Restart Sidekick
```bash
./restart.sh
```

Fallback:
- If mirrored mode is unavailable in your Windows/WSL version, use the non-mirrored section below with `HOST_IP` and firewall scoping.

## 1) Configure Ollama on Windows host

### A. Open Windows PowerShell (not WSL)
- Start Menu -> search `PowerShell` -> open it.

### B. Confirm Ollama exists on Windows
```powershell
ollama --version
ollama list
```

If `ollama` is not found:
1. Download/install Ollama for Windows:
   - https://ollama.com/download/windows
2. Re-open PowerShell and verify:
```powershell
ollama --version
ollama list
```
3. Pull model on host:
```powershell
ollama pull qwen3:8b
```

### C. Set Windows environment variables
```powershell
setx OLLAMA_NUM_PARALLEL 1
setx OLLAMA_CONTEXT_LENGTH 4096
```

Notes:
- `setx` is persistent for your user profile.
- These values apply after restarting Ollama.

### D. Restart Ollama on Windows
Option 1 (recommended):
1. Quit Ollama from the system tray.
2. Start Ollama again from Start Menu.

Option 2 (PowerShell):
```powershell
taskkill /IM ollama.exe /F
ollama serve
```

### E. Verify Ollama API on Windows
```powershell
curl.exe http://localhost:11434/api/tags
```

Expected: JSON with available models.

---

## 2) Point Sidekick (WSL) to host Ollama

### A. Open project in WSL
```bash
cd ~/sidekick
```

### B. Update `.env`
Set these values:
```env
OLLAMA_HOST=http://host.docker.internal:11434
SUMMARIZATION_TIMEOUT_SECONDS=600
OLLAMA_CONTEXT_LENGTH=4096
```

If `host.docker.internal` does not resolve in your WSL network mode, use Windows host IP:

```bash
HOST_IP=$(awk '/nameserver/ {print $2; exit}' /etc/resolv.conf)
echo "$HOST_IP"
```

Then set:
```env
OLLAMA_HOST=http://<HOST_IP>:11434
```

### C. Test connectivity from WSL
```bash
curl http://host.docker.internal:11434/api/tags
```

If that fails:
```bash
curl "http://$HOST_IP:11434/api/tags"
```

Expected: JSON with model list.

### D. Restart Sidekick
```bash
./restart.sh
```

Or with Cloudflare:
```bash
./restart.sh --cloudflare
```

---

## Quick Troubleshooting

- `curl .../api/tags` fails from WSL:
  - Use `HOST_IP` approach.
  - Ensure Windows Ollama is running.
  - Check Windows Firewall rules if needed.

- Re-summarize still slow on `chunk 1/6`:
  - Ensure only one summarization job at a time.
  - Confirm `OLLAMA_NUM_PARALLEL=1` on Windows.
  - Keep `OLLAMA_CONTEXT_LENGTH=4096` (or reduce to `3072` if still unstable).

- Export job appears stuck and `updated_at` does not change:
  1. Check job payload:
     - `curl -s http://127.0.0.1:8000/api/export-jobs/<job_id>`
  2. If `status=running`, `message=extraction: Processing chunk 1/N`, and `updated_at` is frozen:
     - the worker is blocked in the first model call.
  3. In Windows PowerShell, check active inference:
     - `ollama ps`
  4. If `ollama ps` is empty while app says extraction is running:
     - restart Sidekick (`./restart.sh`) and retry once.
     - lower timeout temporarily for faster failure visibility:
       - `.env`: `SUMMARIZATION_TIMEOUT_SECONDS=180`
     - lower context if needed:
       - `.env`: `OLLAMA_CONTEXT_LENGTH=3072` (then `2048` if still unstable)
  5. Restart after any `.env` change:
     - `./restart.sh`

- `ollama ps` fails with `Head "http://127.0.0.1:1...`:
  - Your current PowerShell session likely has `OLLAMA_HOST` set incorrectly.
  - Check:
    - `echo $Env:OLLAMA_HOST`
  - Clear for current session:
    - `Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue`
  - Then retry:
    - `ollama ps`

---

## Stuck-State Recovery (WSL relay vs host Ollama)

If you keep seeing `Only one usage of each socket address...` on `ollama serve`, it means port `11434` is already in use. Do not start a second server.

### 1) Identify who owns port 11434 (Windows PowerShell)
```powershell
Get-NetTCPConnection -LocalPort 11434 | Select-Object LocalAddress,LocalPort,State,OwningProcess
Get-Process -Id <OwningProcess>
```

Interpretation:
- `ProcessName = ollama`: good, host Ollama is active.
- `ProcessName = wslrelay`: traffic is being forwarded to WSL (not host-native runtime).

### 2) If owner is `wslrelay`, stop WSL Ollama first
In WSL:
```bash
pkill -f "ollama serve|ollama runner" || true
```

Then in Windows PowerShell:
```powershell
Get-NetTCPConnection -LocalPort 11434 | Select-Object LocalAddress,LocalPort,State,OwningProcess
Get-Process -Id <OwningProcess>
```

You want the owner to be `ollama`.

### 3) PowerShell curl gotcha
In PowerShell, `curl` maps to `Invoke-WebRequest` and may prompt a security warning. Use:

```powershell
curl.exe http://localhost:11434/api/tags
```

or:

```powershell
Invoke-RestMethod http://localhost:11434/api/tags
```

### 4) If `api/tags` returns `{"models":[]}`
That means the currently active Ollama store has no model loaded. Pull again in the active runtime:

```powershell
ollama pull qwen3:8b
ollama list
curl.exe http://localhost:11434/api/tags
```

### 5) Final WSL check + app restart
In WSL:
```bash
curl http://host.docker.internal:11434/api/tags
```

If it works, ensure `.env` contains:
```env
OLLAMA_HOST=http://host.docker.internal:11434
SUMMARIZATION_TIMEOUT_SECONDS=600
OLLAMA_CONTEXT_LENGTH=4096
```

Then:
```bash
./restart.sh
```

---

## If WSL cannot reach host Ollama

Symptom:
- `curl http://host.docker.internal:11434/api/tags` -> cannot resolve host
- `curl http://<HOST_IP>:11434/api/tags` -> connection refused/failed

Cause:
- Host Ollama is usually bound to `127.0.0.1:11434` only, which WSL NAT cannot access.

### Fix on Windows host

1) Set Ollama bind address for host accessibility:
```powershell
setx OLLAMA_HOST "0.0.0.0:11434"
```

2) Restart Ollama app/service (quit tray app, reopen Ollama).

3) Verify listener is not loopback-only:
```powershell
Get-NetTCPConnection -LocalPort 11434 | Select-Object LocalAddress,LocalPort,State,OwningProcess
```

Expected `LocalAddress` should be `0.0.0.0` (or include non-loopback), not only `127.0.0.1`.

4) Test again from WSL:
```bash
HOST_IP=$(awk '/nameserver/ {print $2; exit}' /etc/resolv.conf)
curl "http://$HOST_IP:11434/api/tags"
```

5) Use that endpoint in Sidekick `.env`:
```env
OLLAMA_HOST=http://<HOST_IP>:11434
```

Security note:
- If needed, add a Windows Firewall inbound rule for TCP 11434 scoped to local/private/WSL subnet only.

---

## Monitoring Helpers

### Unified command from WSL (recommended)

```bash
# Ollama watcher (PowerShell + ollama ps)
./debug.sh ollama

# Ollama + GPU
./debug.sh ollama --gpu --interval 1

# Export job monitor by id
./debug.sh export <job_id>

# Export job monitor for latest seen job in logs
./debug.sh export-latest

# Benchmark model latency against a real recording transcript chunk
./debug.sh benchmark --runs 2
```

### PowerShell: live Ollama watcher

```powershell
# Ollama only (refresh every 2s)
powershell -ExecutionPolicy Bypass -File .\scripts\monitor_ollama.ps1

# Ollama + GPU stats
powershell -ExecutionPolicy Bypass -File .\scripts\monitor_ollama.ps1 -ShowGpu
```

### WSL: live export job watcher

```bash
# Usage: ./scripts/monitor_export_job.sh <job_id> [interval_seconds] [base_url]
./scripts/monitor_export_job.sh <job_id> 1 http://127.0.0.1:8000
```

This prints status/stage/progress and exits when the job completes, fails, or is not found.
