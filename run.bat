@echo off
rem Запуск AimTracker без IDE — двойной клик по этому файлу.
rem Сам ищет интерпретатор в venv рядом со скриптом. Если не найдёт, зовёт
rem системный питон, а дальше aim_tracker.py доищет venv сам.

rem UTF-8 в консоли: иначе русский текст ниже выйдет кракозябрами
chcp 65001 >nul 2>&1

setlocal
cd /d "%~dp0"
set "AIMTRACKER_LAUNCHER=1"

set "PY="
if defined AIMTRACKER_PYTHON if exist "%AIMTRACKER_PYTHON%" set "PY=%AIMTRACKER_PYTHON%"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "venv\Scripts\python.exe"  set "PY=venv\Scripts\python.exe"
if not defined PY if exist "env\Scripts\python.exe"   set "PY=env\Scripts\python.exe"
if not defined PY if exist ".env\Scripts\python.exe"  set "PY=.env\Scripts\python.exe"
if not defined PY set "PY=py"

echo [run.bat] Интерпретатор: %PY%
echo.
"%PY%" aim_tracker.py
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="9009" echo [run.bat] Питон не найден вообще. Установи Python с python.org.
if not "%CODE%"=="0" echo [run.bat] Код завершения: %CODE%

rem pause обязательна: без неё окно закроется вместе с текстом ошибки
pause
endlocal
