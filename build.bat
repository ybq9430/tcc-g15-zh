@echo off
chcp 65001 >nul
rem 构建简体中文版（onedir，用于分发）
echo ========================================
echo  Building tcc-g15 (zh-CN)
echo ========================================

pyinstaller ^
  --icon=icons/gaugeIcon.ico ^
  --uac-admin ^
  --add-data "icons/gaugeIcon.png;icons" ^
  --add-data "tcc_g15_task.xml;." ^
  --add-data "src/GUI/translations;translations" ^
  --hidden-import PySide6 ^
  --hidden-import PySide6.QtNetwork ^
  --collect-all winrt ^
  -w -y src/tcc-g15.py
if errorlevel 1 goto :error

echo.
echo Build OK -^> dist\tcc-g15\
echo Copy the whole folder to run the app.
exit /b 0

:error
echo.
echo Build FAILED.
exit /b 1
