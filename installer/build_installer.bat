@echo off
rem Builds the Beacon installer in one go:
rem   1) bundles the app into dist\Beacon\ (no Python needed on customers' PCs)
rem   2) wraps it into installer\Output\Beacon-Setup-<version>.exe
rem Needs (once): Inno Setup 6 (jrsoftware.org) and Python 3.11+ from python.org.
rem The bundling runs in its own environment (build\buildenv) with the exact
rem package versions from build-requirements.txt; your normal Python is untouched.
rem (Python 3.10.0 can't be used: a bug in it breaks PyInstaller.)
rem
rem Code signing (optional, stops the "Windows protected your PC" warning once
rem the certificate has a reputation). Set ONE of these before running:
rem   set SIGN_THUMBPRINT=<thumbprint of a certificate in your Windows store>
rem   set SIGN_CERT=C:\path\to\certificate.pfx   and   set SIGN_PASSWORD=<its password>
rem Needs signtool.exe from the Windows SDK. Without them, nothing is signed.
setlocal
cd /d "%~dp0.."
set "PY=build\buildenv\Scripts\python.exe"

if not exist "%PY%" (
    echo Creating the build environment - first time only...
    py -3.14 -m venv build\buildenv 2>nul || py -3 -m venv build\buildenv
    if not exist "%PY%" goto :nopython
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r installer\build-requirements.txt
    if errorlevel 1 goto :failed
)

echo [1/2] Bundling the app...
"%PY%" -m PyInstaller --noconfirm --clean --windowed ^
    --name "Beacon" ^
    --icon "%CD%\laser_status.ico" ^
    --add-data "%CD%\laser_status.ico;." ^
    --collect-all winrt ^
    --collect-data customtkinter ^
    --specpath build ^
    laser_status_app.py
if errorlevel 1 goto :failed
call :sign "dist\Beacon\Beacon.exe"
if errorlevel 1 goto :failed

echo [2/2] Building the installer...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 isn't installed. Get it from https://jrsoftware.org/isdl.php
    goto :failed
)
"%ISCC%" installer\LaserStatus.iss
if errorlevel 1 goto :failed
for %%F in (installer\Output\Beacon-Setup-*.exe) do (
    call :sign "%%F"
    if errorlevel 1 goto :failed
)

echo.
echo Done: installer\Output\
dir /b installer\Output
pause
exit /b 0

:sign
if "%SIGN_THUMBPRINT%%SIGN_CERT%"=="" exit /b 0
set "SIGNTOOL="
for /f "delims=" %%S in ('dir /b /s /o-n "%ProgramFiles(x86)%\Windows Kits\10\bin\*signtool.exe" 2^>nul ^| findstr /i "\\x64\\"') do if not defined SIGNTOOL set "SIGNTOOL=%%S"
if not defined SIGNTOOL (
    echo signtool.exe not found: install the Windows SDK, or clear SIGN_THUMBPRINT / SIGN_CERT.
    exit /b 1
)
echo Signing %~1
if not "%SIGN_THUMBPRINT%"=="" (
    "%SIGNTOOL%" sign /sha1 %SIGN_THUMBPRINT% /fd sha256 /tr http://timestamp.digicert.com /td sha256 "%~1"
) else (
    "%SIGNTOOL%" sign /f "%SIGN_CERT%" /p "%SIGN_PASSWORD%" /fd sha256 /tr http://timestamp.digicert.com /td sha256 "%~1"
)
exit /b %errorlevel%

:nopython
echo Couldn't create the build environment: install Python 3.11 or newer from python.org.
:failed
echo.
echo Build failed - see the messages above.
pause
exit /b 1
