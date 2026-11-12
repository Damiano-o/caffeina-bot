@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Crea bot sconti caffeina

echo ============================================================
echo  CREAZIONE BOT TELEGRAM - SCONTI CAFFEINA
echo ============================================================
echo.
echo 1) Su Telegram apri @BotFather
echo 2) Invia:  /newbot
echo 3) Scegli un nome e un username (deve finire con "bot")
echo 4) BotFather risponde con il token: tienilo premuto e copia
echo.
echo Premi un tasto quando hai il token negli appunti...
pause >nul

echo.
echo Lettura del token dagli appunti...
python run.py setup --clipboard
if errorlevel 1 goto errore

echo.
echo ============================================================
echo  Bot configurato. Avvio in corso...
echo  Lascia questa finestra aperta: chiudendola il bot si ferma.
echo ============================================================
python run.py run
goto fine

:errore
echo.
echo Configurazione non riuscita.
echo Se il token non e' stato letto dagli appunti, rilancia questo file
echo e incollalo a mano con Ctrl+V quando richiesto.
pause

:fine
