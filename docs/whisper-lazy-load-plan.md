# Plan: Lazy Whisper Load + Idle Unload + Recording-Start Warmup

## Context

Currently `WhisperModel(large-v3)` is eagerly loaded at Sidekick startup via `app.py`, consuming ~3GB VRAM for the entire session. The goal is to:

1. **Lazy load**: Don't load Whisper at startup — load on first use only
2. **Idle unload**: After 5 min of no transcription activity, unload the model and free VRAM
3. **Recording-start warmup**: When user clicks Record, fire a background warmup so the model is already loaded by the time they stop recording

---

## Changes (5 touch points)

### 1. `config/settings.py`
Add one new setting:
```python
whisper_idle_timeout_seconds: int = 300  # 0 = never unload
```

### 2. `src/transcription/whisper_local.py`

**`__init__`**: Read `whisper_idle_timeout_seconds` from settings. Add:
```python
self._idle_timeout_seconds = settings.whisper_idle_timeout_seconds
self._unload_task: asyncio.Task | None = None
self._init_lock = asyncio.Lock()
```

**`initialize()`**: Wrap body in `asyncio.Lock` to prevent concurrent double-loads (warmup + eager processing could both call this simultaneously):
```python
async def initialize(self):
    if self._initialized:
        return
    async with self._init_lock:
        if self._initialized:  # re-check after acquiring lock
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._load_model)
        self._initialized = True
```

**`transcribe()`**: Cancel any pending unload task at entry, schedule a new one after completion:
```python
async def transcribe(self, audio, sample_rate=16000, language=None, ...):
    # Cancel pending idle unload
    if self._unload_task and not self._unload_task.done():
        self._unload_task.cancel()
        self._unload_task = None

    if not self._initialized:
        await self.initialize()

    # ... existing transcription logic ...
    result = await loop.run_in_executor(...)

    # Schedule idle unload (0 = disabled)
    if self._idle_timeout_seconds > 0:
        self._unload_task = asyncio.create_task(self._idle_unload())

    return result
```

**New `_idle_unload()` method**:
```python
async def _idle_unload(self):
    try:
        await asyncio.sleep(self._idle_timeout_seconds)
        self._model = None
        self._initialized = False
        # Run CUDA/GC cleanup in thread pool (non-blocking)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._cleanup_after_unload)
    except asyncio.CancelledError:
        pass  # New transcription started — stay loaded
```

**New `_cleanup_after_unload()` method** (sync, runs in executor):
```python
def _cleanup_after_unload(self):
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
```

**`_transcribe_sync()` guard**: Add at top of method to handle the edge case where unload races with a queued transcription:
```python
if self._model is None:
    self._load_model()
```

**`shutdown()`**: Cancel any pending unload task on shutdown:
```python
async def shutdown(self):
    if self._unload_task and not self._unload_task.done():
        self._unload_task.cancel()
    self._model = None
    self._executor.shutdown(wait=False)
    self._initialized = False
```

### 3. `src/api/app.py`
Remove the pre-init block entirely (lines 55–63):
```python
# DELETE this block:
# Pre-initialize transcription manager to load model at startup
print(f"[Startup] Initializing transcription manager ...")
try:
    await app.state.transcription_manager.initialize()
    ...
```
The `TranscriptionManager` is still created (for the state object) — just not initialized.

### 4. `src/api/routes/export.py`
Add a new fire-and-forget warmup endpoint (place near top with other endpoints):
```python
@router.post("/transcription/warmup")
async def warmup_transcription(request: Request):
    """Pre-load the Whisper model in the background without blocking."""
    manager = request.app.state.transcription_manager
    asyncio.create_task(manager.initialize())
    return {"status": "warming up"}
```

### 5. `web/js/app.js` — `_startRecording()`
Add a fire-and-forget warmup fetch right after the recording starts successfully (after `this.state.isRecording = true`):
```javascript
// Warm up transcription model in background while user records
fetch('/api/transcription/warmup', { method: 'POST' }).catch(() => {});
```
No await — this returns immediately, model loads in background.

---

## Flow After This Change

| Event | What happens |
|---|---|
| Sidekick starts | 0 VRAM consumed (no model load) |
| User clicks Record | Warmup fires → model loads in background (~10-30s) |
| User records (2+ min) | Model finishes loading while they talk |
| User stops recording | Eager transcription starts — model already loaded, no delay |
| Transcription completes | 5-min idle timer starts |
| 5 min pass with no recording | Model unloads, VRAM freed |
| User clicks Record again | Warmup fires, model reloads |

---

## Critical Files

| File | Change |
|---|---|
| `config/settings.py` | Add `whisper_idle_timeout_seconds: int = 300` |
| `src/transcription/whisper_local.py` | Idle unload timer, init lock, unload/cleanup methods, guard in _transcribe_sync |
| `src/api/app.py` | Remove pre-init block |
| `src/api/routes/export.py` | Add `/api/transcription/warmup` endpoint |
| `web/js/app.js` | Fire warmup fetch in `_startRecording()` |

---

## Verification

1. `./restart.sh` — check logs show no "Initializing transcription manager" on startup
2. Open task manager / `nvidia-smi` — confirm 0 VRAM for Whisper at startup
3. Click Record — wait 30s — model should load in background (check `sidekick.log`)
4. Stop recording, process — should proceed without model-load delay
5. Wait 5 min after a transcription — confirm VRAM drops in `nvidia-smi`
6. Record again — confirm warmup reloads model
