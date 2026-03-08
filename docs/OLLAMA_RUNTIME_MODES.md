# Ollama Runtime Modes

Sidekick can use Ollama in either of these setups:

1. WSL-local Ollama
2. Windows-host Ollama

Both are valid. Windows host was previously preferred for better RAM headroom, but WSL-local Ollama is acceptable if performance is good enough on your machine.

## What Sidekick Actually Connects To

Sidekick does not care where Ollama is installed. It only cares about `OLLAMA_HOST` in `.env`.

Example:

```env
OLLAMA_HOST=http://127.0.0.1:11434
```

The important catch is that `127.0.0.1:11434` is ambiguous in WSL:

- It may reach a WSL Ollama daemon running inside Linux.
- It may reach the Windows host Ollama daemon if mirrored networking is enabled and Windows owns the port.

If both runtimes are installed, `127.0.0.1:11434` does not guarantee that Sidekick is using the Windows host.

## Current Project Position

- WSL-local Ollama: supported
- Windows-host Ollama: optional
- Do not assume `127.0.0.1:11434` means Windows host
- If you care which runtime is being used, verify it explicitly

## How to Verify Which Runtime Owns Ollama

### Check WSL

In WSL:

```bash
ps -ef | rg '[o]llama'
ollama ps
```

Interpretation:

- `ollama serve` running in WSL means Linux has its own Ollama daemon.
- `ollama ps` shows currently loaded models for the WSL daemon.

### Check Windows Host

From WSL:

```bash
powershell.exe -NoProfile -Command "Get-Process ollama -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path"
powershell.exe -NoProfile -Command "ollama ps"
```

Interpretation:

- `Get-Process ollama` confirms whether the Windows Ollama app/server is running.
- `powershell.exe ... \"ollama ps\"` shows currently loaded models for the Windows host runtime.

## Recommended Usage

If WSL Ollama is working well, keep it simple:

- leave `OLLAMA_HOST=http://127.0.0.1:11434`
- use the WSL daemon
- avoid chasing host/relay setup unless you actually need more headroom or stability

If you want Windows-host Ollama instead:

- follow [host_ollama_setup.md](/home/ozzfa/sidekick/docs/host_ollama_setup.md)
- make sure WSL is not also serving Ollama on the same port
- verify from both WSL and PowerShell after the change

## Timeouts vs Unloading

These are different controls:

- `SUMMARIZATION_TIMEOUT_SECONDS`: maximum time Sidekick waits for a single summarization call
- Ollama `keep_alive`: how long the model stays resident after a call finishes

`SUMMARIZATION_TIMEOUT_SECONDS` is not a VRAM unload timer.

Current Sidekick behavior:

- Whisper is explicitly unloaded before summarization to free VRAM
- Ollama summarization calls send `keep_alive=0`
- that tells Ollama to unload the summarization model immediately after each summary/refine call completes

## Why Windows Host Was Used Before

The earlier push toward host Ollama was mainly about:

- better RAM headroom
- avoiding WSL memory pressure
- cleaner separation between the app runtime and the LLM runtime

It may also help performance on some machines, but that is environment-dependent. It is not a universal rule that host Ollama is always faster.

## Failure Modes to Watch For

### `127.0.0.1:11434` points at the wrong runtime

Symptom:

- you think you are using Windows host, but Sidekick is actually talking to WSL Ollama

Fix:

- verify both runtimes explicitly
- stop the unwanted daemon or point `OLLAMA_HOST` at a non-ambiguous address

### Both WSL and Windows Ollama are installed

This is fine, but confusing.

If both are running:

- `ollama ps` in WSL shows WSL-loaded models
- `powershell.exe -Command "ollama ps"` shows Windows-loaded models

Treat them as separate runtimes with separate loaded-model state.

### Summarization appears to "hang" on first call

Possible causes:

- model cold-start
- too-large context
- wrong runtime
- runtime reachable, but not the one you thought

Checks:

```bash
ollama ps
powershell.exe -NoProfile -Command "ollama ps"
tail -f data/sidekick.log
```

## Practical Recommendation

If WSL summarization is performing well enough, do not optimize prematurely. The simplest stable configuration is usually best.
