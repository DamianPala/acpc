# Agents

## Commit rules

- `docs/plans/` is local-only working documentation. Never commit files from this directory.
- No AI attribution trailers (Co-Authored-By, AI-assistant, etc.)
- Conventional Commits format, imperative mood

## Project

- Python >= 3.12, cross-platform (Linux, macOS, Windows)
- Toolchain: uv (build, deps, run)
- Lint/format: `uv run ruff check && uv run ruff format`
- Type check: `uv run pyright`
- Tests: `uv run pytest tests/ --ignore=tests/test_live.py` (live tests require real adapters)
- Installed in editable mode: `uv tool install -e .`
