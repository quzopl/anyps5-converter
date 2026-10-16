@echo off
rem AnyPS5 Converter - opens the window when run without arguments
where py >nul 2>nul
if %errorlevel%==0 (py -3 "%~dp0anyps5_converter.py" %*) else (python "%~dp0anyps5_converter.py" %*)
if errorlevel 1 pause
