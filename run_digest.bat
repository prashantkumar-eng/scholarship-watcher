@echo off
cd /d "%~dp0"
"C:\Users\itspr\AppData\Local\Programs\Python\Python311\python.exe" watcher.py >> task_runs.log 2>&1
"C:\Users\itspr\AppData\Local\Programs\Python\Python311\python.exe" deep_scan.py --queue --workers 24 --timeout 12 --links-per-site 4 >> task_runs.log 2>&1
"C:\Users\itspr\AppData\Local\Programs\Python\Python311\python.exe" watcher.py --send-digest >> task_runs.log 2>&1
