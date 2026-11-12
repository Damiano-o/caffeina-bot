@echo off
rem Avvia il bot con la finestra visibile (log a schermo + data\bot.log)
cd /d "%~dp0"
if not exist data mkdir data
python run.py run
pause
