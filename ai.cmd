@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
call "%~dp0python_runtime.cmd" -m aibridge.cli %*
