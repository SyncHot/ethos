@echo off
REM EthOS Drive Build Script
REM Builds Windows installer using PyInstaller + Inno Setup
REM Run from the ethos-drive/ directory

echo ========================================
echo  EthOS Drive Build
echo ========================================

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    exit /b 1
)

REM Check PyInstaller
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

REM Clean previous builds
echo Cleaning previous build...
if exist dist rmdir /s /q dist
if exist build_output rmdir /s /q build_output

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt

REM Run PyInstaller
echo Building executable...
python -m PyInstaller ^
    --name "EthOS Drive" ^
    --windowed ^
    --onedir ^
    --icon "src\resources\icons\ethos-drive.ico" ^
    --add-data "src\resources;resources" ^
    --hidden-import "PySide6.QtSvg" ^
    --hidden-import "PySide6.QtNetwork" ^
    --hidden-import "engineio.async_drivers.threading" ^
    --noconfirm ^
    --clean ^
    src\ethos_drive\main.py

if errorlevel 1 (
    echo ERROR: PyInstaller build failed
    exit /b 1
)

echo Build complete! Output in dist\EthOS Drive\

REM Check for Inno Setup
where iscc >nul 2>&1
if errorlevel 1 (
    echo.
    echo NOTE: Inno Setup not found. To create installer:
    echo   1. Install Inno Setup from https://jrsoftware.org/isinfo.php
    echo   2. Run: iscc src\resources\installer\ethos-drive.iss
    exit /b 0
)

REM Build installer
echo Building installer...
iscc src\resources\installer\ethos-drive.iss

if errorlevel 1 (
    echo ERROR: Installer build failed
    exit /b 1
)

echo ========================================
echo  Build complete!
echo  Installer: dist\EthOSDriveSetup.exe
echo ========================================
