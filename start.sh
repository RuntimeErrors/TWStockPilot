#!/usr/bin/env bash
# =============================================================================
# TwinStock — macOS / Linux 一鍵啟動腳本
# 使用方式：
#   chmod +x start.sh
#   ./start.sh
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================================="
echo "  TWStockPilot — 本地儀表板伺服器"
echo "=================================================="

# ── 1. 載入 .env（若存在）─────────────────────────────────────────
if [ -f ".env" ]; then
    echo "📋 載入 .env 環境變數..."
    # 逐行讀取，跳過註解與空行
    while IFS= read -r line || [ -n "$line" ]; do
        [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
        export "$line"
    done < ".env"
    echo "✅ .env 載入完成"
else
    echo "⚠️  未找到 .env，使用現有環境變數"
    echo "   （可複製 .env.example 為 .env 並填入 Token）"
fi

# ── 2. 建立虛擬環境（若尚未建立）───────────────────────────────────
if [ ! -d ".venv" ]; then
    echo ""
    echo "🔧 建立 Python 虛擬環境 (.venv)..."
    python3 -m venv .venv
    echo "✅ 虛擬環境建立完成"
fi

# ── 3. 啟用虛擬環境 ─────────────────────────────────────────────────
source .venv/bin/activate
echo "✅ 虛擬環境已啟用：$(which python)"

# ── 4. 安裝/更新依賴 ────────────────────────────────────────────────
echo ""
echo "📦 安裝套件依賴（requirements.txt）..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✅ 套件安裝完成"

# ── 5. 啟動伺服器 ─────────────────────────────────────────────────
echo ""
python server.py
