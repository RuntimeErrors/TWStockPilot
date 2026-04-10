@echo off
REM =============================================================================
REM TwinStock — Windows 一鍵啟動腳本
REM 使用方式：雙擊 start.bat，或在命令提示字元執行
REM =============================================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion

cd /d "%~dp0"

echo ==================================================
echo   TWStockPilot - 本地儀表板伺服器
echo ==================================================

REM ── 1. 載入 .env（若存在）────────────────────────────────────────
if exist ".env" (
    echo 載入 .env 環境變數...
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "line=%%A"
        if not "!line:~0,1!"=="#" (
            if not "%%A"=="" (
                set "%%A=%%B"
            )
        )
    )
    echo .env 載入完成
) else (
    echo 未找到 .env，使用現有環境變數
    echo （可複製 .env.example 為 .env 並填入 Token）
)

REM ── 2. 檢查 Python ─────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [錯誤] 找不到 Python，請先安裝 Python 3.9 以上版本
    echo        下載：https://www.python.org/downloads/
    pause
    exit /b 1
)
echo.
for /f "tokens=*" %%i in ('python --version') do echo Python 版本：%%i

REM ── 3. 建立虛擬環境（若尚未建立）──────────────────────────────────
if not exist ".venv" (
    echo.
    echo 建立 Python 虛擬環境 .venv ...
    python -m venv .venv
    echo 虛擬環境建立完成
)

REM ── 4. 啟用虛擬環境 ────────────────────────────────────────────────
call .venv\Scripts\activate.bat
echo 虛擬環境已啟用

REM ── 5. 安裝/更新依賴 ───────────────────────────────────────────────
echo.
echo 安裝套件依賴（requirements.txt）...
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
echo 套件安裝完成

REM ── 6. 啟動伺服器 ──────────────────────────────────────────────────
echo.
python server.py

pause
