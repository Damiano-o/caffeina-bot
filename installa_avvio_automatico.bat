@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Installo il controllo automatico del bot ogni 5 minuti.
 echo Se il bot e' spento o bloccato, il watchdog lo riavvia.
echo.

schtasks /Create /TN "CaffeinaBot" /SC MINUTE /MO 5 /RL LIMITED /F /TR "wscript.exe \"%~dp0avvia_watchdog.vbs\""
if errorlevel 1 (
  echo.
  echo Creazione non riuscita. Apri il Prompt dei comandi come amministratore
  echo e rilancia questo file.
  pause
  exit /b 1
)

echo.
echo Fatto. Il controllo parte a ogni accesso (entro 5 minuti) e poi ogni 5 minuti.
echo Avvio immediato:    schtasks /Run /TN "CaffeinaBot"
echo Diagnostica:        python watchdog.py --status
echo Log del bot:         data\bot.log
echo Log del watchdog:    data\watchdog.log
echo Per rimuovere tutto: schtasks /Delete /TN "CaffeinaBot" /F
pause
