@echo off
cd /d "C:\Users\itspr\Downloads\google alert"
"C:\Users\itspr\AppData\Local\Programs\Python\Python311\python.exe" watcher.py >> task_runs.log 2>&1
"C:\Users\itspr\AppData\Local\Programs\Python\Python311\python.exe" watcher.py --send-digest >> task_runs.log 2>&1
