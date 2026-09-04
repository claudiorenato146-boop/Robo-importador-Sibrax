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

python -c "import selenium" >nul 2>&1
if errorlevel 1 (
    echo.
    echo O componente do Chrome ainda nao foi instalado.
    echo Execute primeiro o arquivo INSTALAR_DEPENDENCIAS.bat.
    echo.
    pause
    exit /b 1
)

python robo_completo.py
set "CODIGO_SAIDA=%ERRORLEVEL%"

echo.
if "%CODIGO_SAIDA%"=="0" (
    echo Execucao encerrada sem erros bloqueantes.
) else (
    echo Execucao encerrada com pendencias ou erros. Leia a mensagem acima.
)
echo.
pause
exit /b %CODIGO_SAIDA%

