@echo off
rem Stops tradeflow practice mode.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\practice.ps1" stop
