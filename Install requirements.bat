@echo off
rem Installs everything Laser Status needs. Run once.
python -m pip install --upgrade customtkinter pyserial pywin32 pillow psutil winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Globalization
echo.
echo Done. Double-click "Start Laser Status.bat" to run the app.
pause
