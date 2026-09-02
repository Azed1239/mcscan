@echo off
REM Rebuilds dist\mcscan.exe. Needs: pip install customtkinter pyinstaller pillow
cd /d "%~dp0"
python make_icon.py
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name mcscan --icon mcscan.ico ^
  --add-data "mcscan.ico;." ^
  --collect-all customtkinter --hidden-import darkdetect ^
  gui.py
echo.
echo Built dist\mcscan.exe
pause
