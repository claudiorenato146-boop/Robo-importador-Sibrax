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

echo.
echo Instalando o componente de automacao do Chrome...
python -m pip install --upgrade -r requirements.txt
set "CODIGO_SAIDA=%ERRORLEVEL%"

echo.
if "%CODIGO_SAIDA%"=="0" (
    echo Instalacao concluida.
) else (
    echo Nao foi possivel instalar. Confira a internet e tente novamente.
)
echo.
pause
exit /b %CODIGO_SAIDA%

