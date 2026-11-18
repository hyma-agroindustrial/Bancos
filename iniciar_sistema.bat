@echo off
title Sistema Control de Bancos - Hyma Agroindustrial
echo ========================================================
echo    Iniciando Servidor - Control Bancos y Gastos
echo ========================================================
cd /d "%~dp0"

echo Abriendo navegador en http://localhost:8000 ...
start http://localhost:8000

echo Levantando servidor backend y base de datos...
call venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

pause
