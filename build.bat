@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

set "OUT_DIR=dist\GTA6SubVoice"

echo ============================================================
echo  GTA6SubVoice - Build (--onedir, CPU-only, autonomous)
echo ============================================================

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment .venv not found!
    exit /b 1
)

if not exist "models\v5_6_ru.pt" (
    echo [ERROR] Model models\v5_6_ru.pt not found!
    echo Run: %VENV_PY% -m src.dependency_manager --download-all
    exit /b 1
)
if not exist "models\ocr\ch_PP-OCRv5_mobile_det.onnx" (
    echo [ERROR] OCR models in models\ocr\ not found!
    echo Run: %VENV_PY% -m src.dependency_manager --download-all
    exit /b 1
)

if exist "tests" (
    echo [1/4] Running pytest unit tests...
    "%VENV_PY%" -m pytest -q
    if errorlevel 1 (
        echo [ERROR] Unit tests failed. Aborting build.
        exit /b 1
    )
) else (
    echo [1/4] Skipping pytest ^(tests/ folder not present^)...
)

echo [2/4] Cleaning previous build artifacts...
taskkill /IM GTA6SubVoice.exe /F >nul 2>&1
ping -n 2 127.0.0.1 >nul
if exist build rmdir /s /q build
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%"
if exist "dist\GTA6SubVoice_lite" rmdir /s /q "dist\GTA6SubVoice_lite"

echo [3/4] Compiling with PyInstaller (--onedir --windowed)...
"%VENV_PY%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --windowed ^
    --name GTA6SubVoice ^
    --icon "icons\GTA6SubVoice_icon.ico" ^
    --collect-all rapidocr_onnxruntime ^
    --collect-binaries onnxruntime ^
    --collect-data pymorphy3_dicts_ru ^
    --collect-data num2words ^
    --hidden-import PySide6.QtSvg ^
    --hidden-import PySide6.QtNetwork ^
    --hidden-import pycaw ^
    --hidden-import pycaw.pycaw ^
    --hidden-import pycaw.constants ^
    --hidden-import pycaw.utils ^
    --hidden-import comtypes ^
    --hidden-import comtypes.client ^
    --hidden-import comtypes.stream ^
    --hidden-import num2words ^
    --hidden-import num2words.lang_RU ^
    --hidden-import httpx ^
    --hidden-import pymorphy3_dicts_ru ^
    --hidden-import winrt.windows.media.ocr ^
    --hidden-import winrt.windows.graphics.imaging ^
    --hidden-import winrt.windows.storage.streams ^
    --hidden-import winrt.windows.globalization ^
    --exclude-module PySide6.QtWebEngineCore ^
    --exclude-module PySide6.QtWebEngineWidgets ^
    --exclude-module PySide6.QtWebEngineQuick ^
    --exclude-module PySide6.Qt3DCore ^
    --exclude-module PySide6.Qt3DRender ^
    --exclude-module PySide6.Qt3DInput ^
    --exclude-module PySide6.Qt3DLogic ^
    --exclude-module PySide6.Qt3DAnimation ^
    --exclude-module PySide6.Qt3DExtras ^
    --exclude-module PySide6.QtQuick ^
    --exclude-module PySide6.QtQuickWidgets ^
    --exclude-module PySide6.QtQml ^
    --exclude-module PySide6.QtPdf ^
    --exclude-module PySide6.QtPdfWidgets ^
    --exclude-module PySide6.QtCharts ^
    --exclude-module PySide6.QtMultimedia ^
    --exclude-module PySide6.QtMultimediaWidgets ^
    --exclude-module PySide6.QtDesigner ^
    --exclude-module PySide6.QtHelp ^
    --exclude-module PySide6.QtTest ^
    --exclude-module matplotlib ^
    --exclude-module scipy ^
    --exclude-module tkinter ^
    main.py

if errorlevel 1 (
    echo [ERROR] PyInstaller build failed!
    exit /b 1
)

echo [4/4] Packaging data/, icons/ and models/ into %OUT_DIR%...
xcopy /E /I /Y "data" "%OUT_DIR%\data" >nul
if not exist "%OUT_DIR%\icons" mkdir "%OUT_DIR%\icons"
if exist "icons\GTA6SubVoice_icon.ico" copy /Y "icons\GTA6SubVoice_icon.ico" "%OUT_DIR%\icons\GTA6SubVoice_icon.ico" >nul
if exist "%OUT_DIR%\data\config.json" del /F /Q "%OUT_DIR%\data\config.json"
if exist "%OUT_DIR%\data\characters.json" (
    echo {} > "%OUT_DIR%\data\characters.json"
)
if not exist "%OUT_DIR%\models" mkdir "%OUT_DIR%\models"
if not exist "%OUT_DIR%\models\ocr" mkdir "%OUT_DIR%\models\ocr"
if exist "models\subtitle_font_clf.onnx" (
    copy /Y "models\subtitle_font_clf.onnx" "%OUT_DIR%\models\subtitle_font_clf.onnx" >nul
)
copy /Y "models\v5_6_ru.pt" "%OUT_DIR%\models\v5_6_ru.pt" >nul
xcopy /E /I /Y "models\ocr" "%OUT_DIR%\models\ocr" >nul

echo.
echo [CHECK] Verifying %OUT_DIR%\GTA6SubVoice.exe ...
if not exist "%OUT_DIR%\GTA6SubVoice.exe" (
    echo [ERROR] Executable not found!
    exit /b 1
)

echo [SUCCESS] Build completed: %OUT_DIR%\GTA6SubVoice.exe
exit /b 0
