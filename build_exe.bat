@echo off
rem Builds Panel.exe (a tiny launcher with the app name and icon)
rem Uses the C# compiler that ships with .NET Framework (present on every Windows).
setlocal
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
    echo csc.exe not found - install .NET Framework 4.x
    pause
    exit /b 1
)
"%CSC%" /nologo /target:winexe /codepage:65001 /win32icon:"%~dp0app.ico" /out:"%~dp0Panel.exe" "%~dp0Launcher.cs"
if errorlevel 1 (
    echo Build failed
    pause
    exit /b 1
)
echo Done: %~dp0Panel.exe
pause
