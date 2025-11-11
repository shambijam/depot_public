@echo off
REM Script de diagnostic pour Windows VPS
REM Double-cliquer sur ce fichier pour lancer le diagnostic

echo ========================================
echo DIAGNOSTIC TRAILING STOP
echo ========================================
echo.

REM Trouver Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERREUR: Python non trouve dans le PATH
    echo Verifiez que Python est installe
    pause
    exit /b 1
)

echo Lancement du diagnostic...
echo.

python diagnose_trailing_vps.py

echo.
echo ========================================
echo DIAGNOSTIC TERMINE
echo ========================================
echo.
echo Copiez TOUTE la sortie ci-dessus et envoyez-la pour analyse.
echo.

pause
