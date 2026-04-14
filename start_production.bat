@echo off
setlocal

if exist .\venv\Scripts\python.exe (
  set "PYTHON_EXE=.\venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=python"
)

set "LOW_RESOURCE_MODE=1"
set "WAITRESS_THREADS=2"
set "WAITRESS_CONNECTION_LIMIT=40"
set "HOST=127.0.0.1"
set "PORT=8080"

"%PYTHON_EXE%" run_production.py
