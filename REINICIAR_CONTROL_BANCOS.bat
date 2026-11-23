@echo off
title Reiniciar Control Bancos (puerto 8000)
cd /d "%~dp0"
echo Cerrando SOLO lo que usa el puerto 8000 (Control Bancos). Sol Quant (8010) no se toca...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ids=@(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); $all=@(Get-CimInstance Win32_Process); $todo=New-Object System.Collections.Generic.HashSet[int]; function T([int]$i){ if($todo.Add($i)){ $all | Where-Object { $_.ParentProcessId -eq $i } | ForEach-Object { T ([int]$_.ProcessId) } } }; foreach($i in $ids){ T ([int]$i); $p=$all | Where-Object { $_.ProcessId -eq $i }; if($p -and $p.ParentProcessId){ $pp=$all | Where-Object { $_.ProcessId -eq $p.ParentProcessId -and $_.Name -match '^python' }; if($pp){ T ([int]$pp.ProcessId) } } }; foreach($i in $todo){ if($i -gt 4){ Write-Host ('  - Terminando PID ' + $i); Stop-Process -Id $i -Force -ErrorAction SilentlyContinue } }; if($todo.Count -eq 0){ Write-Host '  (no habia nada en el puerto 8000)' }"
timeout /t 2 /nobreak >nul
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if %errorlevel%==0 (
    echo.
    echo [AVISO] El puerto 8000 sigue ocupado. Cierra la ventana negra anterior de Control Bancos y vuelve a ejecutar este archivo.
    pause
    exit /b 1
)
echo Puerto libre. Iniciando Control Bancos...
call "%~dp0iniciar_sistema.bat"
