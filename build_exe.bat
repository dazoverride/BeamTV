@echo off
cd /d "%~dp0"
echo Cerrando instancias abiertas de BeamTV.exe (si las hay)...
taskkill /f /im BeamTV.exe >nul 2>&1
echo Compilando BeamTV.exe portable...
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
) else (
    set "PY=python"
)
"%PY%" -m pip install --quiet pyinstaller
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "BeamTV" --icon "assets\icon.ico" --add-data "assets;assets" --distpath "." --workpath "build" main.py
if exist "build" rmdir /s /q "build"
if exist "BeamTV.spec" del /q "BeamTV.spec"
echo.
echo [OK] BeamTV.exe generado correctamente.
pause
