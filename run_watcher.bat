@echo off
cd /d "%~dp0"
"C:\Users\itspr\AppData\Local\Programs\Python\Python311\python.exe" watcher.py >> task_runs.log 2>&1
