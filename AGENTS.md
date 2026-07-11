# Repository Guidelines

## Project Structure & Module Organization
`app.py` is the main Flask application factory and route layer. Core business logic lives in `services/`, grouped by domain such as `inventory.py`, `invoices.py`, `cash_analysis.py`, and `auth.py`. Jinja templates are in `templates/`, shared styling and emoji assets are in `static/`, and runtime CSV data is stored under `data/` with backups in `data/backups/`. Production entrypoints are `run_production.py`, `start_production.bat`, `desktop_launcher.py`, and `launch_buen_yantar.bat`.

## Build, Test, and Development Commands
Create a virtual environment and install dependencies with `python -m venv .venv` and `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`. Run the local server with `python app.py` if you are wiring a dev entrypoint, or use `.\start_production.bat` to start the Waitress server with the repository defaults. Use `python run_production.py` for a direct production-style launch and `.\launch_buen_yantar.bat` for the desktop browser wrapper.

## Coding Style & Naming Conventions
Follow the existing Python style: 4-space indentation, type hints where practical, and `snake_case` for modules, functions, variables, and CSV field helpers. Keep Flask routes thin and move reusable logic into `services/`. Preserve the current Spanish-facing UI text and CSV filenames unless a change explicitly requires renaming both code and stored data. Template filenames use descriptive lowercase names such as `factura_nueva.html`.

## Testing Guidelines
There is no committed automated test suite yet. Before opening a PR, at minimum run `python -m py_compile app.py services\*.py` and smoke-test the main flows: login, inventory edits, invoice creation, cash history, and export paths. If you add tests, prefer `pytest`, place them in a top-level `tests/` directory, and name files `test_<feature>.py`.

## Commit & Pull Request Guidelines
Recent history uses short, direct commit messages, often in Spanish, for example `fix en migracion` or `EDITAR PRODUCTO ARREGLADO`. Keep commits focused and descriptive; use imperative language and mention the affected area. PRs should include a concise summary, manual test notes, any data migration impact, and screenshots when templates or styling change.

## Security & Configuration Tips
Do not commit populated `data/*.csv` files with real business data. Set `SECRET_KEY` outside development, and treat `LOW_RESOURCE_MODE`, `HOST`, `PORT`, and Waitress thread settings as environment-driven deployment controls.
