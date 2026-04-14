@echo off
setlocal

if exist .\venv\Scripts\pyinstaller.exe (
  set "PYINSTALLER_EXE=.\venv\Scripts\pyinstaller.exe"
) else (
  set "PYINSTALLER_EXE=pyinstaller"
)

"%PYINSTALLER_EXE%" --clean --noconfirm --onefile --name BuenYantarLauncher desktop_launcher.py
