@echo off
setlocal
cd /d %~dp0

where py >nul 2>nul
if %errorlevel%==0 goto RUN
where python >nul 2>nul
if %errorlevel%==0 goto RUNPYTHON

echo [INFO] Python not found.
echo [INFO] Please install Python 3 from https://www.python.org/downloads/windows/
echo [INFO] After installation, run this file again.
pause
exit /b 1

:RUN
if "%ANNOTATOR_PORT%"=="" set ANNOTATOR_PORT=8765
if "%ANNOTATOR_AUTO_OPEN%"=="" set ANNOTATOR_AUTO_OPEN=1
py app.py
goto END

:RUNPYTHON
if "%ANNOTATOR_PORT%"=="" set ANNOTATOR_PORT=8765
if "%ANNOTATOR_AUTO_OPEN%"=="" set ANNOTATOR_AUTO_OPEN=1
python app.py

:END
endlocal
