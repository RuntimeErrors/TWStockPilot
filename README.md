# 🚀 TWStockPilot - 台股量化分析導航員

TWStockPilot 是一款專為台灣股市設計的量化分析工具，透過多因子評分模型（技術面、法籌面、基本面）深度解析個股與族群強弱勢。系統結合了宏觀經濟數據、自動化 CI/CD 排程以及專業級互動圖表，為投資決策提供數據支持。

---

## ✨ 核心特色

- **📊 多因子量化分析**：整合技術指標（MA, MACD, RSI, 突破動能）、三大法人買賣超、融資融券變化及基本面營收 EPS。
- **🌐 全球局勢儀表板**：透過 `generate_portal.py` 自動抓取 FRED 與 Yahoo Finance 宏觀數據（通膨、美債利差、全球指數），快速掌握市場環境。
- **🥇 智能評分系統**：根據自定義權重（`config.json`）對個股進行 0-100 分量化評分，快速識別市場格局（強勢多頭/震盪/空頭）。
- **🎯 Top-Down 族群動能掃描**：一鍵掃描特定產業族群（如 AI 伺服器、半導體），計算「族群平均漲幅」，並依據動能強度自動篩選出族群內的強勢領頭羊。
- **📡 自動化 CI/CD 排程**：
    - **早盤預警 (08:00 Taipei Time)**：開盤前更新宏觀數據與最新報告。
    - **盤後複盤 (17:15 Taipei Time)**：盤後資訊揭露後，第一時間完成全自動掃描與報告更新。
- **📈 專業級互動報告**：採用 **TradingView Lightweight Charts** 打造極致流暢的 K 線圖視覺化體驗，支援離線查看。
- **📲 Telegram 即時推播**：分析完成後自動發送分析摘要與詳細歷史報價數據（CSV）至您的 Telegram。

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
您可以建立一個 `.env` 檔案或在系統中設定以下變數：
- `FINMIND_TOKEN`: [FinMind](https://finmindtrade.com/) API Token (強烈建議，避免受限)。
- `TG_BOT_TOKEN`: Telegram Bot Token。
- `TG_CHAT_ID`: Telegram 接收訊息的 Chat ID。

---

## 🚀 快速開始

### 單股深度分析
分析指定個股並發送報告：
```bash
export STOCK_ID=2330
python main.py
```

### 產業族群批次分析
執行 `industry_analyzer.py` 會根據 `industry_groups.py` 中定義的族群進行掃描：
```bash
python industry_analyzer.py
```

### 生成門戶儀表板
彙整所有報告並抓取最新宏觀數據：
```bash
python generate_portal.py
```
> 分析結果與儀表板將存放在 `reports/` 目錄下。

---

## ⚙️ 配置文件說明 (`config.json`)

您可以在 `config.json` 中自定義各項技術、籌碼、基本面指標的權重：

| 指標類別 | 關鍵參數 | 說明 |
| :--- | :--- | :--- |
| **技術面** | `ma_bullish_score` | 均線多頭排列時加的分數 |
| **法人面** | `it_buy_score` | 投信連買時加的分數 |
| **籌碼面** | `tdcc_high_threshold` | 千張大戶持股比例門檻 (TDCC) |
| **基本面** | `rev_yoy_growth_score` | 營收年增率正向時加的分數 |
| **格局判定** | `strong_bull` | 判定為「強勢多頭」的總分門檻 |

---

## 📁 專案結構

- `main.py`: 單股分析核心腳本。
- `industry_analyzer.py`: 產業族群批次分析工具。
- `industry_groups.py`: 定義各類股族群成分股。
- `generate_portal.py`: 生成全球局勢儀表板與報告連結索引。
- `config.json`: 全域量化指標權重與門檻設定。
- `.github/workflows/`: 自動化 CI 排程工作流。
- `reports/`: 存放生成的 HTML 互動圖表、TXT 報告與 `index.html` 門戶首頁。
- `stock_data/` / `cache/`: 資料暫存快取。

---

## ⚖️ 免責聲明
本專案僅提供程式交易研究與量化分析學習之用，不構成任何形式的投資建議。市場有風險，投資需謹慎，請獨立判斷損益。

## 📄 開源協議
本專案採用 [MIT License](LICENSE) 開源。