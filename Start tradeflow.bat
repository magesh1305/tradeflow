@echo off
rem Starts tradeflow in practice mode (pretend money) and opens it in your browser.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\practice.ps1" start
