@echo off
chcp 65001 >nul
title Organizador de PDFs em lotes
cd /d "%~dp0"

rem Procura o Python: primeiro o inicializador "py", depois "python"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo Python nao encontrado neste computador.
    echo Instale pelo site https://www.python.org/downloads/
    echo e marque a opcao "Add python.exe to PATH" na instalacao.
    echo.
    pause
    exit /b 1
)

echo Abrindo o Organizador de PDFs... (esta janela pode ficar aberta)
%PY% organizar_pdfs_gui.py
if errorlevel 1 (
    echo.
    echo O programa terminou com erro. Veja a mensagem acima.
    pause
)
