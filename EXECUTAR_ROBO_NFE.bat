@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERRO: Python nao foi encontrado neste computador.
    echo Instale o Python 3.10 ou superior e tente novamente.
    echo.
    pause
    exit /b 1
)

python robo_importacao_nfe.py
set "CODIGO_SAIDA=%ERRORLEVEL%"

echo.
if "%CODIGO_SAIDA%"=="0" (
    echo Execucao encerrada sem erros bloqueantes.
) else (
    echo Execucao encerrada com pendencias ou erros. Consulte o relatorio.
)
echo.
pause
exit /b %CODIGO_SAIDA%
