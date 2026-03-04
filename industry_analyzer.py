import os
import io
import requests
import pandas as pd
import numpy as np
import datetime
import concurrent.futures
from pathlib import Path
from scipy.signal import argrelextrema
from FinMind.data import DataLoader

from industry_groups import INDUSTRY_GROUPS

# ============================================================
# 1. 環境初始化
# ============================================================
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN")
TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID")

START_DATE    = (datetime.datetime.now() - datetime.timedelta(days=3 * 365)).strftime("%Y-%m-%d")
REPORT_DIR    = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)

TODAY_STR     = datetime.datetime.now().strftime("%Y%m%d")
TIMESTAMP_STR = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

api = DataLoader()
if FINMIND_TOKEN:
    api.login_by_token(api_token=FINMIND_TOKEN)
    print("✅ FinMind API 登入成功")
else:
    print("⚠️  未設定 FINMIND_TOKEN，使用公開額度（可能受速率限制）")

# ============================================================
# 2. 單股資料下載（並行用）
# ============================================================
def _fetch(label, func, stock_id):
    try:
        df = func(stock_id=stock_id, start_date=START_DATE)
        return label, df if df is not None and not df.empty else pd.DataFrame()
    except Exception as e:
        print(f"   ⚠️  [{stock_id}] {label} 下載失敗: {e}")
        return label, pd.DataFrame()

def _fetch_tdcc(stock_id):
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
        if res.status_code == 200:
            df = pd.read_csv(io.BytesIO(res.content), encoding='utf-8-sig', dtype=str)
            df.columns = [c.strip() for c in df.columns]
            if '證券代號' not in df.columns:
                return "tdcc", pd.DataFrame()
            df['證券代號'] = df['證券代號'].str.strip()
            return "tdcc", df[df['證券代號'] == str(stock_id).strip()].copy()
    except Exception as e:
        print(f"   ⚠️  [{stock_id}] TDCC 下載失敗: {e}")
    return "tdcc", pd.DataFrame()

# ============================================================
# 3. 單股分析 — 資料清洗 + 技術指標 + 評分
# ============================================================
def analyze_single_stock(stock_id: str, stock_name: str) -> dict:
    """
    下載並清洗單支股票資料，回傳結構化分析結果 dict。
    所有數值已清洗為 Python 純量（float/int/str），適合直接寫入文字報告。
    """
    result = {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "score": 50,
        "status": "資料不足",
        "error": None,
    }

    try:
        # --- 並行下載 ---
        data = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futs = [
                ex.submit(_fetch, "price",       api.taiwan_stock_daily,                        stock_id),
                ex.submit(_fetch, "financial",   api.taiwan_stock_financial_statement,           stock_id),
                ex.submit(_fetch, "revenue",     api.taiwan_stock_month_revenue,                 stock_id),
                ex.submit(_fetch, "institutional",api.taiwan_stock_institutional_investors,      stock_id),
                ex.submit(_fetch, "margin",      api.taiwan_stock_margin_purchase_short_sale,    stock_id),
                ex.submit(_fetch_tdcc,           stock_id),
            ]
            for f in concurrent.futures.as_completed(futs):
                label, df = f.result()
                data[label] = df

        # --- 價格清洗 ---
        df_price = data.get("price", pd.DataFrame())
        if df_price.empty:
            result["error"] = "無法取得股價資料"
            return result

        dp = df_price.rename(columns={
            'date': 'Date', 'close': 'Close', 'open': 'Open',
            'max': 'High', 'min': 'Low', 'Trading_Volume': 'Volume'
        }).copy()
        dp['Date'] = pd.to_datetime(dp['Date'])
        dp = dp.set_index('Date').sort_index()
        cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        dp[cols] = dp[cols].apply(pd.to_numeric, errors='coerce')
        dp.dropna(subset=cols, inplace=True)

        if len(dp) < 30:
            result["error"] = "股價資料不足（< 30 筆）"
            return result

        # 移動平均
        dp['MA5']  = dp['Close'].rolling(5).mean()
        dp['MA10'] = dp['Close'].rolling(10).mean()
        dp['MA20'] = dp['Close'].rolling(20).mean()
        dp['MA60'] = dp['Close'].rolling(60).mean()

        # 布林通道
        dp['STD20']    = dp['Close'].rolling(20).std()
        dp['BB_Upper'] = dp['MA20'] + 2 * dp['STD20']
        dp['BB_Lower'] = dp['MA20'] - 2 * dp['STD20']

        # ATR
        dp['H-L']  = dp['High'] - dp['Low']
        dp['H-PC'] = abs(dp['High'] - dp['Close'].shift(1))
        dp['L-PC'] = abs(dp['Low']  - dp['Close'].shift(1))
        dp['TR']   = dp[['H-L','H-PC','L-PC']].max(axis=1)
        dp['ATR14']= dp['TR'].rolling(14).mean()

        # RSI
        delta = dp['Close'].diff()
        gain  = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
        loss  = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        dp['RSI14'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

        # MACD
        dp['EMA12']      = dp['Close'].ewm(span=12, adjust=False).mean()
        dp['EMA26']      = dp['Close'].ewm(span=26, adjust=False).mean()
        dp['MACD']       = dp['EMA12'] - dp['EMA26']
        dp['MACD_Signal']= dp['MACD'].ewm(span=9, adjust=False).mean()
        dp['MACD_Hist']  = dp['MACD'] - dp['MACD_Signal']

        # 支撐 / 壓力
        local_max = argrelextrema(dp['High'].values, np.greater_equal, order=15)[0]
        local_min = argrelextrema(dp['Low'].values,  np.less_equal,    order=15)[0]
        resistances = dp['High'].iloc[local_max].tail(2).values
        supports    = dp['Low'].iloc[local_min].tail(2).values

        last = dp.iloc[-1]

        # --- 法人清洗 ---
        df_inst = data.get("institutional", pd.DataFrame())
        inst_pivot = pd.DataFrame()
        fi_5d = it_5d = 0
        if not df_inst.empty:
            di = df_inst.rename(columns={'date': 'Date'}).copy()
            di['Date'] = pd.to_datetime(di['Date'])
            di['net']  = pd.to_numeric(di['buy'], errors='coerce') - pd.to_numeric(di['sell'], errors='coerce')
            inst_pivot = di.pivot_table(index='Date', columns='name', values='net', aggfunc='sum').fillna(0)
            for c in ['Foreign_Investor', 'Investment_Trust']:
                if c not in inst_pivot.columns:
                    inst_pivot[c] = 0
            fi_5d = float(inst_pivot['Foreign_Investor'].tail(5).sum())
            it_5d = float(inst_pivot['Investment_Trust'].tail(5).sum())

        # --- 月營收清洗 ---
        df_rev = data.get("revenue", pd.DataFrame())
        rev_yoy = None
        latest_rev = None
        if not df_rev.empty:
            dr = df_rev.rename(columns={'date': 'Date', 'revenue': 'Revenue'}).copy()
            dr['Date']    = pd.to_datetime(dr['Date'])
            dr['Revenue'] = pd.to_numeric(dr['Revenue'], errors='coerce')
            dr = dr.set_index('Date').sort_index()
            if 'revenue_year_growth' in dr.columns:
                dr['Revenue_YoY'] = pd.to_numeric(dr['revenue_year_growth'], errors='coerce')
            else:
                dr['Revenue_YoY'] = dr['Revenue'].pct_change(periods=12) * 100
            if not dr.empty:
                latest_rev = float(dr['Revenue'].iloc[-1])
                rev_yoy    = float(dr['Revenue_YoY'].iloc[-1])

        # --------------------------------------------------------
        # 4. 多因子評分
        # --------------------------------------------------------
        score = 50

        # 技術面
        if last['Close'] > last['MA20'] and last['MA20'] > last['MA60']:
            score += 10
        elif last['Close'] < last['MA20'] and last['MA20'] < last['MA60']:
            score -= 15

        if last['MACD_Hist'] > 0 and dp['MACD_Hist'].iloc[-2] <= 0:
            score += 10
        elif last['MACD_Hist'] < 0:
            score -= 5

        if last['Volume'] > dp['Volume'].tail(5).mean() * 1.5 and last['Close'] > last['Open']:
            score += 5

        # 法人面
        if it_5d > 0:
            score += 10
        else:
            score -= 5

        # 基本面
        if rev_yoy is not None:
            if rev_yoy > 0:
                score += 15
            elif rev_yoy < 0:
                score -= 10

        score = max(0, min(100, score))

        # --------------------------------------------------------
        # 5. 封裝清洗後的純量結果
        # --------------------------------------------------------
        status_map = {
            range(75, 101): "強勢多頭",
            range(60, 75):  "震盪偏多",
            range(40, 60):  "弱勢盤整",
            range(0,  40):  "空頭啟動",
        }
        status = next((v for k, v in status_map.items() if score in k), "未知")

        result.update({
            "score": score,
            "status": status,
            # 價格技術
            "close":       round(float(last['Close']),  2),
            "ma20":        round(float(last['MA20']),   2) if not pd.isna(last['MA20'])   else None,
            "ma60":        round(float(last['MA60']),   2) if not pd.isna(last['MA60'])   else None,
            "rsi14":       round(float(last['RSI14']),  1) if not pd.isna(last['RSI14'])  else None,
            "macd_hist":   round(float(last['MACD_Hist']), 3) if not pd.isna(last['MACD_Hist']) else None,
            "atr14":       round(float(last['ATR14']),  2) if not pd.isna(last['ATR14'])  else None,
            "bb_upper":    round(float(last['BB_Upper']),2) if not pd.isna(last['BB_Upper']) else None,
            "bb_lower":    round(float(last['BB_Lower']),2) if not pd.isna(last['BB_Lower']) else None,
            "volume_ratio": round(float(last['Volume']) / float(dp['Volume'].tail(5).mean()), 2)
                            if dp['Volume'].tail(5).mean() > 0 else None,
            "resistance":  round(float(resistances[-1]), 2) if len(resistances) > 0 else None,
            "support":     round(float(supports[-1]),    2) if len(supports)    > 0 else None,
            "stop_loss":   round(float(last['Close']) - 1.5 * float(last['ATR14']), 2)
                            if not pd.isna(last['ATR14']) else None,
            # MACD 黃金交叉
            "macd_cross":  (last['MACD_Hist'] > 0 and dp['MACD_Hist'].iloc[-2] <= 0),
            # 法人
            "fi_5d":       round(fi_5d / 1000, 1),   # 換算成張（千股→張）
            "it_5d":       round(it_5d / 1000, 1),
            # 基本面
            "latest_rev":  round(latest_rev / 1e8, 2) if latest_rev else None,  # 億元
            "rev_yoy":     round(rev_yoy, 1) if rev_yoy is not None else None,
        })

    except Exception as e:
        result["error"] = str(e)

    return result


# ============================================================
# 4. 單股報告文字生成
# ============================================================
def _rsi_label(rsi):
    if rsi is None: return "N/A"
    if rsi > 70: return f"{rsi}（超買過熱⚠️）"
    if rsi < 30: return f"{rsi}（超賣區間💡）"
    return f"{rsi}（中性）"

def generate_stock_text(r: dict) -> str:
    """將單股分析結果 dict 轉為 AI 可讀的純文字段落。"""
    sid   = r['stock_id']
    sname = r['stock_name']

    if r.get("error"):
        return f"[{sid} {sname}]\n  ⚠️  分析失敗：{r['error']}\n"

    lines = []
    lines.append(f"[{sid} {sname}]  評分：{r['score']}/100  格局：{r['status']}")
    lines.append(f"  收盤價：{r['close']}  MA20：{r['ma20']}  MA60：{r['ma60']}")
    lines.append(f"  RSI14：{_rsi_label(r['rsi14'])}")

    if r.get("macd_cross"):
        lines.append(f"  MACD：柱狀體由負轉正，出現「黃金交叉」翻多訊號🔔")
    else:
        lines.append(f"  MACD 柱狀體：{r['macd_hist']}")

    lines.append(f"  量比（vs 5MA）：{r['volume_ratio']}x")
    lines.append(f"  布林通道：上軌 {r['bb_upper']}  下軌 {r['bb_lower']}")

    res_str = f"{r['resistance']}" if r['resistance'] else "無明顯高點"
    sup_str = f"{r['support']}"    if r['support']    else "無明顯低點"
    sl_str  = f"{r['stop_loss']}"  if r['stop_loss']  else "N/A"
    lines.append(f"  近期壓力：{res_str}  近期支撐：{sup_str}  建議停損：{sl_str}（-1.5 ATR）")

    lines.append(f"  外資近5日淨買超：{r['fi_5d']} 張  投信近5日淨買超：{r['it_5d']} 張")

    rev_str = f"{r['latest_rev']} 億" if r['latest_rev'] else "N/A"
    yoy_str = f"{r['rev_yoy']}%"      if r['rev_yoy'] is not None else "N/A"
    lines.append(f"  最新月營收：{rev_str}  年增率：{yoy_str}")

    # 操作建議
    if r['status'] == "強勢多頭":
        lines.append("  📈 建議：趨勢向上，可沿 MA5/MA10 分批佈局，跌破 MA20 停損。")
    elif r['status'] in ("震盪偏多", "弱勢盤整"):
        lines.append("  ⚖️  建議：方向不明，待帶量突破布林上軌後右側進場。")
    else:
        lines.append("  📉 建議：趨勢偏空，等待 RSI<30 且反彈跡象再短線試單。")

    return "\n".join(lines)


# ============================================================
# 5. 族群報告組合
# ============================================================
def generate_group_report(group_name: str, results: list) -> str:
    """
    將一個族群所有個股的分析結果組合成完整的 AI 可讀報告。
    results: list of result dicts（由 analyze_single_stock 回傳）
    """
    lines = []
    sep   = "=" * 50

    lines.append(sep)
    lines.append(f"  族群分析報告：{group_name}")
    lines.append(f"  產生時間：{TIMESTAMP_STR}")
    lines.append(sep)
    lines.append("")

    # 族群排行（依評分由高到低，過濾掉有 error 的）
    valid = [r for r in results if not r.get("error")]
    if valid:
        ranked = sorted(valid, key=lambda x: x['score'], reverse=True)
        lines.append("【族群評分排行】")
        for i, r in enumerate(ranked, 1):
            star = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"  {i}."
            lines.append(f"  {star} {r['stock_id']} {r['stock_name']}  "
                         f"評分：{r['score']}/100  格局：{r['status']}")
        lines.append("")

        # 族群整體訊號摘要
        avg_score  = round(sum(r['score'] for r in valid) / len(valid), 1)
        bull_count = sum(1 for r in valid if r['status'] in ("強勢多頭", "震盪偏多"))
        bear_count = sum(1 for r in valid if r['status'] in ("空頭啟動",))
        lines.append("【族群整體訊號】")
        lines.append(f"  平均評分：{avg_score}/100")
        lines.append(f"  偏多個股：{bull_count} 支  偏空個股：{bear_count} 支  neutral：{len(valid)-bull_count-bear_count} 支")
        lines.append("")

    # 各股詳細分析
    lines.append("【個股詳細分析】")
    lines.append("")
    for r in results:
        lines.append(generate_stock_text(r))
        lines.append("")

    lines.append(sep)
    lines.append("⚠️  本報告為量化模型輸出，僅供 AI 輔助分析參考，不構成投資建議。")
    lines.append(sep)

    return "\n".join(lines)


# ============================================================
# 6. Telegram 傳送（以檔案方式 sendDocument）
# ============================================================
def send_txt_to_telegram(file_path: Path, caption: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("   ⚠️  未設定 Telegram 環境變數，跳過傳送。")
        return False

    url     = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument"
    payload = {"chat_id": TG_CHAT_ID, "caption": caption}

    try:
        with open(file_path, "rb") as f:
            resp = requests.post(url, data=payload,
                                 files={"document": (file_path.name, f, "text/plain")},
                                 timeout=60)
        if resp.status_code == 200:
            print(f"   📤 Telegram 傳送成功：{file_path.name}")
            return True
        else:
            print(f"   ❌ Telegram 傳送失敗 [{resp.status_code}]：{resp.text[:200]}")
            return False
    except Exception as e:
        print(f"   ❌ Telegram 傳送例外：{e}")
        return False


# ============================================================
# 7. 主流程
# ============================================================
def main():
    print(f"\n{'='*60}")
    print(f"  🚀 TwinStock 族群分析系統啟動")
    print(f"  分析時間：{TIMESTAMP_STR}")
    print(f"  分析族群數：{len(INDUSTRY_GROUPS)}")
    print(f"{'='*60}\n")

    group_names = list(INDUSTRY_GROUPS.keys())

    for group_name in group_names:
        stocks = INDUSTRY_GROUPS[group_name]
        count  = len(stocks)
        print(f"\n📊 開始分析族群：【{group_name}】（共 {count} 支）")

        results = []

        # 族群內各股序列分析（避免 FinMind 頻繁並行觸發速率限制）
        for sid, sname in stocks.items():
            print(f"   🔍 分析 {sid} {sname}...")
            r = analyze_single_stock(sid, sname)
            if r.get("error"):
                print(f"      ⚠️  結果：{r['error']}")
            else:
                print(f"      ✅ 評分：{r['score']}/100  格局：{r['status']}")
            results.append(r)

        # 組合族群報告
        report_text = generate_group_report(group_name, results)

        # 儲存 txt 檔（族群名_日期.txt）
        safe_name   = group_name.replace("/", "_").replace("\\", "_")
        txt_path    = REPORT_DIR / f"{safe_name}_{TODAY_STR}.txt"
        txt_path.write_text(report_text, encoding="utf-8-sig")
        print(f"   💾 報告已儲存：{txt_path}")

        # 傳送至 Telegram
        caption = f"📊 【{group_name}】族群分析報告 {TIMESTAMP_STR}"
        send_txt_to_telegram(txt_path, caption)

    print(f"\n{'='*60}")
    print(f"  ✅ 所有族群分析完成！")
    print(f"  報告目錄：{REPORT_DIR.resolve()}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
