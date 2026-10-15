@echo off
rem Start Datapipeline Automation Hub locally on Windows: double-click this file, or run  run_local.bat  in a terminal.
rem UI http://localhost:8080   API docs http://localhost:8080/api/docs
rem Options (see run_local.py):  run_local.bat --port 8081    run_local.bat --no-browser
rem Turn on Gemini:  set SF_GCP_PROJECT=my-dev-project  then  run_local.bat
rem Google access (Gemini, BigQuery dry-runs, GCS) uses your ADC:  gcloud auth application-default login
setlocal
set "HERE=%~dp0"
set "PY="

rem The launcher is standard-library Python: use the project venv if it exists, else the py launcher, else python.
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto :nopython

"%PY%" "%HERE%run_local.py" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :end
echo.
echo   Datapipeline Automation Hub stopped with exit code %RC%. Read the messages above.
pause
goto :end

:nopython
echo.
echo   Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
echo   (tick "Add python.exe to PATH"), then run run_local.bat again.
echo.
pause
set "RC=1"

:end
endlocal & exit /b %RC%
