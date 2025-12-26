# Toolchain Specification

This project uses the **Astral toolchain** for Python development and dependency management.

## Tools Used

- **uv** - Package and project management (replaces pip/requirements.txt)
- **ruff** - Linting and code formatting (replaces black, flake8, etc.)
- **ty** - Type checking (replaces mypy)

## Tools NOT Used

This project does **not** use traditional Python tooling:

- ❌ `requirements.txt` - Dependencies are managed in `pyproject.toml` via `uv`
- ❌ `pip` - Package installation handled by `uv`
- ❌ `black` - Code formatting handled by `ruff`
- ❌ `flake8` - Linting handled by `ruff`
- ❌ `mypy` - Type checking handled by `ty`

## Usage

- Install dependencies: `uv sync`
- Run application: `uv run sync.py`
- Lint code: `uv run ruff check .`
- Format code: `uv run ruff format .`
- Type check: `uv run ty .`

