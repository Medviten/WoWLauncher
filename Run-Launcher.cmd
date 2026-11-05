@echo off
setlocal
cd /d "%~dp0"

if exist "src-tauri\target\release\whe-launcher.exe" (
    start "" "src-tauri\target\release\whe-launcher.exe"
    exit /b 0
)

echo Release build not found. Starting through Cargo...
cargo run --manifest-path "src-tauri\Cargo.toml"
if errorlevel 1 (
    echo.
    echo Failed to start WHE Launcher.
    pause
)
