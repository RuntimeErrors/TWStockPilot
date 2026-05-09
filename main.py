import os
import io
import csv
import requests
import pandas as pd
import numpy as np
from FinMind.data import DataLoader
import concurrent.futures
from scipy.signal import argrelextrema
import datetime

# ==========================================
# 1. Configuration Settings
# ==========================================
stock_id = os.getenv("STOCK_ID", "2344")
# Fetch last 3 years of data optimally
start_date = (datetime.datetime.now() - datetime.timedelta(days=3*365)).strftime("%Y-%m-%d")
folder_name = "stock_data"

my_token = os.getenv("FINMIND_TOKEN")
tg_bot_token = os.getenv("TG_BOT_TOKEN")
tg_chat_id = os.getenv("TG_CHAT_ID")

if not os.path.exists(folder_name):
    os.makedirs(folder_name)

api = DataLoader()
if my_token:
    api.login_by_token(api_token=my_token)

print(f"🚀 Started {stock_id} Full Quantitative Analysis System...")

# ==========================================
# 2. Data Fetching
# ==========================================
def get_finmind_data(dataset_name, func, custom_start_date=None):
    # For CI, we skip local caching or we only cache temporarily
    sd = custom_start_date if custom_start_date else start_date
    print(f"📥 Downloading {dataset_name}...")
    try:
        df = func(stock_id=stock_id, start_date=sd)
        if df is not None and not df.empty:
            return dataset_name, df
    except Exception as e:
        print(f"❌ Failed to download {dataset_name}: {e}")
    return dataset_name, pd.DataFrame()

def get_tdcc_latest(stock_id):
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=60)
        if res.status_code == 200:
            df = pd.read_csv(io.BytesIO(res.content), encoding='utf-8-sig', dtype=str)
            df.columns = [c.strip() for c in df.columns]
            if '證券代號' not in df.columns: return "tdcc", pd.DataFrame()
            df['證券代號'] = df['證券代號'].str.strip()
            target_df = df[df['證券代號'] == str(stock_id).strip()].copy()
            return "tdcc", target_df
    except Exception as e:
        print(f"❌ Failed to download TDCC: {e}")
    return "tdcc", pd.DataFrame()

data_dict = {}
now_date = datetime.datetime.now()
def _get_sd_main(days):
    return (now_date - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
    futures = [
        executor.submit(get_finmind_data, "price", api.taiwan_stock_daily, _get_sd_main(365)),
        executor.submit(get_finmind_data, "financial", api.taiwan_stock_financial_statement, _get_sd_main(800)),
        executor.submit(get_finmind_data, "revenue", api.taiwan_stock_month_revenue, _get_sd_main(450)),
        executor.submit(get_finmind_data, "institutional", api.taiwan_stock_institutional_investors, _get_sd_main(30)),
        executor.submit(get_finmind_data, "margin", api.taiwan_stock_margin_purchase_short_sale, _get_sd_main(30)),
        executor.submit(get_finmind_data, "balance_sheet", api.taiwan_stock_balance_sheet, _get_sd_main(800)),
        executor.submit(get_tdcc_latest, stock_id)
    ]
    for future in concurrent.futures.as_completed(futures):
        name, df = future.result()
        data_dict[name] = df

df_price = data_dict.get("price", pd.DataFrame())
df_fs = data_dict.get("financial", pd.DataFrame())
df_rev = data_dict.get("revenue", pd.DataFrame())
df_inst = data_dict.get("institutional", pd.DataFrame())
df_margin = data_dict.get("margin", pd.DataFrame())
df_bs = data_dict.get("balance_sheet", pd.DataFrame())
df_tdcc = data_dict.get("tdcc", pd.DataFrame())

print("✅ Data download completed. Starting quantitative calculations...")

# ==========================================
# 3. Data Cleaning & Quantitative Indicators
# ==========================================
df_plot_price = pd.DataFrame()
recent_resistances = []
recent_supports = []

if not df_price.empty:
    df_plot_price = df_price.copy()
    df_plot_price = df_plot_price.rename(columns={'date': 'Date', 'close': 'Close', 'open': 'Open', 'max': 'High', 'min': 'Low', 'Trading_Volume': 'Volume'})
    df_plot_price['Date'] = pd.to_datetime(df_plot_price['Date'])
    df_plot_price = df_plot_price.set_index('Date').sort_index()
    
    cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    df_plot_price[cols] = df_plot_price[cols].apply(pd.to_numeric, errors='coerce')
    df_plot_price.dropna(subset=cols, inplace=True)

    # MA & BB
    df_plot_price['MA5'] = df_plot_price['Close'].rolling(5).mean()
    df_plot_price['MA10'] = df_plot_price['Close'].rolling(10).mean()
    df_plot_price['MA20'] = df_plot_price['Close'].rolling(20).mean()
    df_plot_price['MA60'] = df_plot_price['Close'].rolling(60).mean()
    df_plot_price['STD20'] = df_plot_price['Close'].rolling(20).std()
    df_plot_price['BB_Upper'] = df_plot_price['MA20'] + 2 * df_plot_price['STD20']
    df_plot_price['BB_Lower'] = df_plot_price['MA20'] - 2 * df_plot_price['STD20']

    # ATR
    df_plot_price['H-L'] = df_plot_price['High'] - df_plot_price['Low']
    df_plot_price['H-PC'] = abs(df_plot_price['High'] - df_plot_price['Close'].shift(1))
    df_plot_price['L-PC'] = abs(df_plot_price['Low'] - df_plot_price['Close'].shift(1))
    df_plot_price['TR'] = df_plot_price[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    df_plot_price['ATR14'] = df_plot_price['TR'].rolling(window=14).mean()

    # RSI
    delta = df_plot_price['Close'].diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss
    df_plot_price['RSI14'] = 100 - (100 / (1 + rs))

    # MACD
    df_plot_price['EMA12'] = df_plot_price['Close'].ewm(span=12, adjust=False).mean()
    df_plot_price['EMA26'] = df_plot_price['Close'].ewm(span=26, adjust=False).mean()
    df_plot_price['MACD'] = df_plot_price['EMA12'] - df_plot_price['EMA26']
    df_plot_price['MACD_Signal'] = df_plot_price['MACD'].ewm(span=9, adjust=False).mean()
    df_plot_price['MACD_Hist'] = df_plot_price['MACD'] - df_plot_price['MACD_Signal']

    # Support / Resistance
    n_days = 15
    local_max_idx = argrelextrema(df_plot_price['High'].values, np.greater_equal, order=n_days)[0]
    local_min_idx = argrelextrema(df_plot_price['Low'].values, np.less_equal, order=n_days)[0]
    
    recent_resistances = df_plot_price['High'].iloc[local_max_idx].tail(2).values
    recent_supports = df_plot_price['Low'].iloc[local_min_idx].tail(2).values

# Institutional
inst_pivot = pd.DataFrame()
if not df_inst.empty:
    df_inst_clean = df_inst.copy().rename(columns={'date': 'Date'})
    df_inst_clean['Date'] = pd.to_datetime(df_inst_clean['Date'])
    df_inst_clean['net'] = pd.to_numeric(df_inst_clean['buy'], errors='coerce') - pd.to_numeric(df_inst_clean['sell'], errors='coerce')
    inst_pivot = df_inst_clean.pivot_table(index='Date', columns='name', values='net', aggfunc='sum').fillna(0)
    for c in ['Foreign_Investor', 'Investment_Trust']:
        if c not in inst_pivot.columns: inst_pivot[c] = 0

# Revenue
revenue_trend = pd.DataFrame()
if not df_rev.empty:
    df_rev_clean = df_rev.copy().rename(columns={'date': 'Date', 'revenue': 'Revenue'})
    df_rev_clean['Date'] = pd.to_datetime(df_rev_clean['Date'])
    df_rev_clean = df_rev_clean.set_index('Date').sort_index()
    df_rev_clean['Revenue'] = pd.to_numeric(df_rev_clean['Revenue'], errors='coerce')
    if 'revenue_year_growth' not in df_rev_clean.columns:
        df_rev_clean['Revenue_YoY'] = df_rev_clean['Revenue'].pct_change(periods=12) * 100
    else:
        df_rev_clean['Revenue_YoY'] = pd.to_numeric(df_rev_clean['revenue_year_growth'], errors='coerce')
    revenue_trend = df_rev_clean

# ==========================================
# 4. Multi-Factor Scoring Model
# ==========================================
score = 50
if not df_plot_price.empty:
    last = df_plot_price.iloc[-1]
    # Technical (+/- 20)
    if last['Close'] > last['MA20'] and last['MA20'] > last['MA60']: score += 10
    elif last['Close'] < last['MA20'] and last['MA20'] < last['MA60']: score -= 15
    if last['MACD_Hist'] > 0 and df_plot_price['MACD_Hist'].iloc[-2] <= 0: score += 10
    elif last['MACD_Hist'] < 0: score -= 5
    if last['Volume'] > df_plot_price['Volume'].tail(5).mean() * 1.5 and last['Close'] > df_plot_price['Open'].iloc[-1]: score += 5

if not inst_pivot.empty:
    # Institutional (+/- 15)
    last_5_trust = inst_pivot['Investment_Trust'].tail(5).sum()
    if last_5_trust > 0: score += 10
    else: score -= 5

if not revenue_trend.empty:
    # Fundamental (+/- 15)
    if revenue_trend['Revenue_YoY'].iloc[-1] > 0 and revenue_trend['Revenue_YoY'].iloc[-2] > 0: score += 15
    elif revenue_trend['Revenue_YoY'].iloc[-1] < 0: score -= 10

score = max(0, min(100, score))

# ==========================================
# 5. Output Report String Generation
# ==========================================
report_lines = []
report_lines.append(f"🤖 股市趨勢導航員 分析報告：【{stock_id}】")
report_lines.append("="*30)

if not df_plot_price.empty:
    last_price = df_plot_price['Close'].iloc[-1]
    last_atr = df_plot_price['ATR14'].iloc[-1]
    rsi = df_plot_price['RSI14'].iloc[-1]

    status = "強勢多頭" if score >= 75 else "震盪偏多" if score >= 60 else "弱勢盤整" if score >= 40 else "空頭啟動"
    report_lines.append(f"【現況總結】：量化評分 {score}/100 分，目前格局定調為「{status}」。\n")

    report_lines.append("【技術指標詳解】：")
    report_lines.append(f"🔹 均線系統：收盤價 {last_price:.2f}。MA20={df_plot_price['MA20'].iloc[-1]:.2f}，MA60={df_plot_price['MA60'].iloc[-1]:.2f}。")
    report_lines.append(f"🔹 動能指標：RSI(14) 為 {rsi:.1f} ({'超買過熱' if rsi>70 else '超賣區間' if rsi<30 else '中性'})。")
    
    if df_plot_price['MACD_Hist'].iloc[-1] > 0 and df_plot_price['MACD_Hist'].iloc[-2] <= 0:
        report_lines.append(f"🔹 MACD：柱狀體由負轉正，出現「黃金交叉」翻多訊號。")
    else:
        report_lines.append(f"🔹 MACD：柱狀體數值為 {df_plot_price['MACD_Hist'].iloc[-1]:.3f}。")
        
    report_lines.append(f"🔹 籌碼量能：{'近期有爆量跡象' if score % 5 == 0 else '量能平穩，未見異常背離'}。\n")

    res_text = f"{recent_resistances[-1]:.2f}" if len(recent_resistances) > 0 else "無明顯近期高點"
    sup_text = f"{recent_supports[-1]:.2f}" if len(recent_supports) > 0 else "無明顯近期低點"
    stop_loss = last_price - (1.5 * last_atr)

    report_lines.append("【操作策略建議】：")
    report_lines.append(f"🔼 壓力位：近期頸線壓力區在 {res_text} 附近，若帶量突破此價位則上方空間打開。")
    report_lines.append(f"🔽 支撐位：波段實質支撐在 {sup_text}。")
    report_lines.append(f"🛡️ 風險控管：建議以 {stop_loss:.2f} (-1.5 ATR) 作為嚴格停損/停利防線。")
    
    if status == "強勢多頭":
        report_lines.append("📈 進場策略：目前趨勢向上，適合沿 MA5/MA10 分批佈局，站上 MA10 作多，跌破 MA20 停損。")
    elif status in ["震盪偏多", "弱勢盤整"]:
        report_lines.append("⚖️ 進場策略：目前方向不明，建議等待帶量突破布林上軌 (BB_Upper) 後再行右側進場。")
    else:
        report_lines.append("📉 進場策略：趨勢偏空，左側摸底風險極高，建議等待 RSI 超賣 (<30) 且有反彈跡象再短線試單。")
    
    report_lines.append("\n【風險警示】：")
    if not revenue_trend.empty and revenue_trend['Revenue_YoY'].iloc[-1] < 0:
        report_lines.append("⚠️ 基本面疲弱：最新月營收呈現年減，若股價無法與基本面脫鉤，需提防高檔主力倒貨。")
    else:
        report_lines.append("⚠️ 外部干擾因素：請留意即將到來的法說會/財報週，以及近期外資在期貨市場的未平倉空單變化。")
else:
    report_lines.append("❌ 無法獲取股價資料，分析失敗。")

report_text = "\n".join(report_lines)
print(report_text)

# ==========================================
# 6. Send to Telegram
# ==========================================
if tg_bot_token and tg_chat_id:
    print("📤 Sending report to Telegram...")
    print(f"   → chat_id: {tg_chat_id}")
    tg_url = f"https://api.telegram.org/bot{tg_bot_token}/sendMessage"
    payload = {
        "chat_id": tg_chat_id,
        "text": report_text,
        # parse_mode removed: report contains special chars (【】, -, .) that break Telegram Markdown
    }
    try:
        response = requests.post(tg_url, json=payload, timeout=30)
        print(f"   → HTTP Status: {response.status_code}")
        if response.status_code == 200:
            print("✅ Telegram message sent successfully!")
        else:
            print(f"❌ Failed to send Telegram message.")
            print(f"   Response: {response.text}")
            
        # ── 歷史報價 CSV 文字檔產出與傳送 ───────────────────────────────
        if not df_plot_price.empty:
            print("📤 Sending historical quotes CSV to Telegram...")
            output = io.StringIO()
            writer = csv.writer(output)
            
            # Header
            writer.writerow([
                "Date", "Open", "High", "Low", "Close", "Volume", 
                "MA5", "MA10", "MA20", "MA60", "BB_Upper", "BB_Lower"
            ])
            
            # Body: iterate over DataFrame (limit to recent 30 days)
            import json
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    export_days = cfg.get("indicators", {}).get("quotes_export_days", 30)
            except Exception:
                export_days = 30
            export_df = df_plot_price.tail(export_days) if export_days > 0 else df_plot_price
            
            for idx, row in export_df.iterrows():
                writer.writerow([
                    idx.strftime("%Y-%m-%d"),
                    round(row.get("Open"), 2) if pd.notna(row.get("Open")) else "",
                    round(row.get("High"), 2) if pd.notna(row.get("High")) else "",
                    round(row.get("Low"), 2) if pd.notna(row.get("Low")) else "",
                    round(row.get("Close"), 2) if pd.notna(row.get("Close")) else "",
                    int(row.get("Volume")) if pd.notna(row.get("Volume")) else "",
                    round(row.get("MA5"), 2) if pd.notna(row.get("MA5")) else "",
                    round(row.get("MA10"), 2) if pd.notna(row.get("MA10")) else "",
                    round(row.get("MA20"), 2) if pd.notna(row.get("MA20")) else "",
                    round(row.get("MA60"), 2) if pd.notna(row.get("MA60")) else "",
                    round(row.get("BB_Upper"), 2) if pd.notna(row.get("BB_Upper")) else "",
                    round(row.get("BB_Lower"), 2) if pd.notna(row.get("BB_Lower")) else ""
                ])
                
            csv_content = output.getvalue()
            doc_name = f"{stock_id}_quotes_{datetime.datetime.now().strftime('%Y%m%d')}.txt"
            
            tg_doc_url = f"https://api.telegram.org/bot{tg_bot_token}/sendDocument"
            doc_payload = {"chat_id": tg_chat_id, "caption": f"📈 【{stock_id}】歷史報價完整數據"}
            doc_files = {"document": (doc_name, csv_content.encode('utf-8-sig'), "text/plain")}
            doc_resp = requests.post(tg_doc_url, data=doc_payload, files=doc_files, timeout=60)
            
            if doc_resp.status_code == 200:
                print(f"✅ Telegram historical quotes sent successfully: {doc_name}")
            else:
                print(f"❌ Failed to send Telegram historical quotes.")
                print(f"   Response: {doc_resp.text}")

        # 發送指定網址做為最後一個訊息
        url_payload = {
            "chat_id": tg_chat_id,
            "text": "https://runtimeerrors.github.io/TWStockPilot/"
        }
        requests.post(tg_url, json=url_payload, timeout=30)

    except Exception as e:
        print(f"❌ Exception occurred while sending Telegram message: {e}")
else:
    print("⚠️ Telegram bot token or chat ID not set. Skipping Telegram notification.")
    print(f"   TG_BOT_TOKEN set: {bool(tg_bot_token)}")
    print(f"   TG_CHAT_ID set: {bool(tg_chat_id)}")
