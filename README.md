# 🚀 TWStockPilot - 台股量化分析導航員

TWStockPilot 是一款專為台灣股市設計的量化分析工具，透過多因子評分模型（技術面、法籌面、基本面）深度解析個股強弱勢。系統支援 Telegram 自動化通知、CSV 數據匯出以及基於 TradingView 的互動式 HTML 視覺化報告。

---

## ✨ 核心特色

- **📊 多因子量化分析**：整合技術指標（MA, MACD, RSI, ATR）、三大法人買賣超、融資融券變化及基本面營收 EPS。
- **🥇 智能評分系統**：根據自定義權重對個股進行 0-100 分量化評分，快速識別市場格局（強勢多頭/震盪/空頭）。
- **📡 Telegram 整合**：分析完成後自動發送圖文摘要報告與歷史數據檔至您的 Telegram 頻道。
- **📈 互動式 HTML 報告**：生成專業級 K 線圖報告，內建 TradingView Lightweight Charts，支援離線查看與極致交互體驗。
- **🔍 產業族群掃描**：一鍵分析特定產業族群（如 AI 伺服器、半導體），自動生成多股對照表與排行。
- **⚙️ 高度可客製化**：透過 `config.json` 即可調整各項指標的加權分數與判斷門檻。

---

## 🛠️ 安裝說明

### 1. 複製專案
```bash
git clone https://github.com/TsaiWeiChang/TWStockPilot.git
cd TWStockPilot
```

### 2. 安裝依賴
建議使用虛擬環境：
```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# 或 .venv\Scripts\activate # Windows
pip install -r requirements.txt
```

### 3. 環境變數設定
您可以建立一個 `.env` 檔案或直接在系統中設定以下變數：
- `FINMIND_TOKEN`: [FinMind](https://finmindtrade.com/) API Token (強烈建議，否則會受速率限制)。
- `TG_BOT_TOKEN`: Telegram Bot Token。
- `TG_CHAT_ID`: Telegram 接收訊息的 Chat ID。

---

## 🚀 快速開始

### 單股深度分析
分析指定個股（例如 2330 台積電）並發送報告：
```bash
export STOCK_ID=2330
python main.py
```

### 產業族群掃描
執行 `industry_analyzer.py` 會根據 `industry_groups.py` 中定義的族群進行批次分析：
```bash
python industry_analyzer.py
```
> 分析結果將存放在 `reports/` 目錄下（包含 HTML 與 TXT 報告）。

---

## ⚙️ 配置文件說明 (`config.json`)

您可以透過修改 `config.json` 來微調系統的交易邏輯。

| 分類 | 參數範例 | 說明 |
| :--- | :--- | :--- |
| **技術面** | `ma_bullish_score` | 均線多頭排列時加的分數 |
| **法人面** | `foreign_buy_score` | 外資連買時加的分數 |
| **籌碼面** | `tdcc_high_threshold` | 千張大戶持股比例門檻 |
| **基本面** | `rev_yoy_growth_score` | 營收年增率正向時加的分數 |
| **門檻** | `strong_bull` | 判定為「強勢多頭」的總分下限 |

---

## 📁 專案結構

- `main.py`: 單股分析核心腳本。
- `industry_analyzer.py`: 產業/多股批次分析工具。
- `industry_groups.py`: 定義個股族群分類。
- `config.json`: 全域評分參數設定。
- `reports/`: 存放生成的 HTML 互動圖表與文本報告。
- `stock_data/` / `cache/`: 資料暫存快取。

---

## ⚖️ 免責聲明
本專案僅供程式交易研究與量化分析學習參考，不構成任何形式的投資建議。投資人應獨立判斷並自負投資風險。

## 📄 開源協議
本專案採用 [MIT License](LICENSE) 開源。