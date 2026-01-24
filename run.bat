@echo off
setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
set "MAIN=%ROOT%main.py"

if exist "%VENV_PY%" (
  "%VENV_PY%" "%MAIN%" %*
) else (
  py "%MAIN%" %*
)

endlocal
