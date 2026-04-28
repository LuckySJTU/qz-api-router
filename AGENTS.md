# Repository Guidelines

## Project Structure & Module Organization

```
api-router/
├── main.py              # Entry point: CLI args, starts proxy + optional TUI
├── config.yaml          # Backend URLs, API key, timeouts, thresholds
├── requirements.txt     # Python dependencies (aiohttp, textual, pyyaml, rich)
├── models.py            # Backend data model, load tracking, thread-safe state
├── logger.py            # Routing and health-check logging (JSON + console)
├── router.py            # Core router: LoadBalancer, HealthChecker, APIRouter
├── proxy.py             # aiohttp local proxy server (unified endpoint)
├── tui.py               # Textual TUI dashboard
└── logs/                # Runtime log files (routing.log, health.log)
```

- **`models.py`** — `Backend` dataclass with thread-safe load counters and status.
- **`router.py`** — `LoadBalancer` (least-load selection), `HealthChecker` (periodic probes), `APIRouter` (request dispatch with retry).
- **`proxy.py`** — aiohttp web server that forwards all paths to the router.
- **`tui.py`** — Textual app rendering backend status, load, and avg response time.
- **`main.py`** — CLI entry point with `--no-tui`, `--config`, `--timeout` options.

## Build, Test, and Development Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the router + TUI dashboard
python main.py

# Run without TUI (proxy only, headless / CI)
python main.py --no-tui

# Custom config path and debug timeout
python main.py -c /path/to/config.yaml --timeout 30
```

## Coding Style & Naming Conventions

- **Language:** Python 3.10+. Use type hints on all public functions.
- **Naming:** `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- **Indentation:** 4 spaces, no tabs.
- **Docstrings:** Triple-quoted, imperative mood (e.g., "Select the backend...").
- **Concurrency:** Use `asyncio` and `aiohttp`; protect shared state with `threading.Lock`.
- No formatter is enforced; keep style consistent with existing files.

## Testing Guidelines

- No test suite exists yet. When adding tests, use **pytest** with files in a `tests/` directory.
- Name test files `test_<module>.py` and functions `test_<behavior>()`.
- Mock external HTTP calls with `aioresponses` or `unittest.mock`.
- Run: `pytest -v`.

## Commit & Pull Request Guidelines

- **Commits:** short imperative subject (≤72 chars), e.g. `Add retry logic to load balancer`.
- **PRs:** describe what changed and why; link related issues; include screenshots for TUI changes.

## Security & Configuration Tips

- Never commit real API keys. Use placeholder values in `config.yaml`.
- The `api_key` field is shared across all backends — rotate it outside this repo.
- `request.timeout` in `config.yaml` controls per-request timeout; lower it for debugging.
