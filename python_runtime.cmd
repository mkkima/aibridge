@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHON_EXE="

if exist "%ROOT%.venv\Scripts\python.exe" set "PYTHON_EXE=%ROOT%.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYTHON_EXE where python >nul 2>nul && set "PYTHON_EXE=python"

if not defined PYTHON_EXE (
  echo Python runtime was not found. Install Python 3.11+ or create .venv. 1>&2
  endlocal & exit /b 1
)

if defined PYTHONPATH (
  set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%ROOT%"
)

"%PYTHON_EXE%" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%

