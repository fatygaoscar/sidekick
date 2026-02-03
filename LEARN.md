# Lessons Learned

This file documents corrections and lessons learned during development that should be referenced in future projects.

## Python Environment

### Use `python3` instead of `python`
On modern Linux systems (especially Debian/Ubuntu), always use `python3` explicitly rather than `python`:
```bash
# Correct
python3 -m venv venv
python3 -m src.main

# May not work on some systems
python -m venv venv
```

### Virtual Environments are Required
Modern Python installations are "externally managed" (PEP 668) and prevent system-wide pip installs. Always create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# or
.\venv\Scripts\activate   # Windows
pip install -e .
```

### README.md Required for pyproject.toml
When using `hatchling` as a build backend with `readme = "README.md"` in pyproject.toml, the README.md file must exist or the build will fail with:
```
OSError: Readme file does not exist: README.md
```

## Pydantic Settings

### Literal Types Don't Work Well with Environment Variables
When using `pydantic-settings`, `Literal` types don't properly coerce string values from `.env` files:
```python
# This will fail when VAD_AGGRESSIVENESS=2 is read from .env
vad_aggressiveness: Literal[0, 1, 2, 3] = 2

# Use int with a validator instead
vad_aggressiveness: int = 2

@field_validator("vad_aggressiveness")
@classmethod
def validate_vad_aggressiveness(cls, v: int) -> int:
    if v not in (0, 1, 2, 3):
        raise ValueError("vad_aggressiveness must be 0, 1, 2, or 3")
    return v
```

## Dependencies

### Don't Forget Implicit Dependencies
When using features from a library in your code, make sure all required packages are in dependencies. For example:
- `scipy` is needed for `scipy.io.wavfile` even though it's commonly used with numpy
- Add it explicitly to `pyproject.toml`:
```toml
dependencies = [
    ...
    "scipy>=1.11.0",
]
```

### webrtcvad Deprecation Warning
The `webrtcvad` package uses deprecated `pkg_resources`. This is a warning only and doesn't affect functionality:
```
UserWarning: pkg_resources is deprecated as an API.
```
This will need to be addressed when upgrading to future Python/setuptools versions.

## Project Structure

### Ensure All __init__.py Files Exist
Every Python package directory needs an `__init__.py` file, even if empty, for proper module resolution.

### Static Files Path
When mounting static files in FastAPI, ensure the directory exists before mounting:
```python
web_dir = Path("web")
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")
```
