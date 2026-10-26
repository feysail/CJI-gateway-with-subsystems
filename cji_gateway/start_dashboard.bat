@echo off
title CJI Gateway - Unified RGB & Port Controller
cd /d "%~dp0"
echo Starting CJI Gateway Unified Controller...
python server.py 8088
pause
