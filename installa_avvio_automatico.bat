@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Installo l'avvio automatico del bot a ogni accesso a Windows...
schtasks /Create /TN "CaffeinaBot" /SC ONLOGON /RL LIMITED /F /TR "wscript.exe \"%~dp0avvia_bot_silenzioso.vbs\""
if errorlevel 1 (
  echo.
  echo Creazione non riuscita. Apri il Prompt dei comandi come amministratore
  echo e rilancia questo file.
) else (
  echo.
  echo Fatto. Il bot partira' in background a ogni accesso.
  echo Per avviarlo subito: doppio clic su avvia_bot_silenzioso.vbs
  echo Per rimuoverlo: schtasks /Delete /TN "CaffeinaBot" /F
)
pause
