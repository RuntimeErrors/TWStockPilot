import os
import io
import time
import threading
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
# 2. 資料下載 helpers
# ============================================================
def _fetch(label: str, func, stock_id: str):
    """FinMind 資料下載，失敗立即回傳空 DataFrame（不重試）。"""
    try:
        df = func(stock_id=stock_id, start_date=START_DATE)
        return label, df if (df is not None and not df.empty) else pd.DataFrame()
    except Exception as e:
        print(f"   ⚠️  [{stock_id}] {label} 下載失敗: {e}")
        return label, pd.DataFrame()


def _fetch_per(stock_id: str):
    """直接呼叫 FinMind REST API 取得 TaiwanStockPER（P/E、P/B）。
    SDK 目前無對應方法，故改用 HTTP。"""
    params = {
        "dataset": "TaiwanStockPER",
        "data_id": stock_id,
        "start_date": START_DATE,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    try:
        resp = requests.get("https://api.finmindtrade.com/api/v4/data",
                            params=params, timeout=30)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("status") == 200 and body.get("data"):
                return "valuation", pd.DataFrame(body["data"])
    except Exception as e:
        print(f"   ⚠️  [{stock_id}] valuation(PER) 下載失敗: {e}")
    return "valuation", pd.DataFrame()


# ── 優化①：TDCC 全域快取（只下載一次，所有股票共用）──────────
_tdcc_all_df: pd.DataFrame | None = None
_tdcc_lock = threading.Lock()

def _get_tdcc_all() -> pd.DataFrame:
    """下載一次完整 TDCC CSV 並快取，後續直接從記憶體篩選。"""
    global _tdcc_all_df
    if _tdcc_all_df is not None:
        return _tdcc_all_df
    with _tdcc_lock:                       # 防止多執行緒同時下載
        if _tdcc_all_df is not None:       # double-check after lock
            return _tdcc_all_df
        url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
        try:
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
            if res.status_code == 200:
                df = pd.read_csv(io.BytesIO(res.content), encoding='utf-8-sig', dtype=str)
                df.columns = [c.strip() for c in df.columns]
                if '證券代號' in df.columns:
                    df['證券代號'] = df['證券代號'].str.strip()
                    _tdcc_all_df = df
                    print("   📦 TDCC 全量資料已快取")
                    return _tdcc_all_df
        except Exception as e:
            print(f"   ⚠️  TDCC 全量下載失敗: {e}")
        _tdcc_all_df = pd.DataFrame()     # 失敗時快取空 df 避免重試
        return _tdcc_all_df

def _fetch_tdcc(stock_id: str):
    df = _get_tdcc_all()
    if df.empty or '證券代號' not in df.columns:
        return "tdcc", pd.DataFrame()
    return "tdcc", df[df['證券代號'] == str(stock_id).strip()].copy()


# ============================================================
# 3. 基本面 helpers
# ============================================================
def _extract_fs_value(df_fs: pd.DataFrame, type_keywords: list) -> float | None:
    """從 financial_statement 找符合關鍵字的最新 value（試多個關鍵字）。"""
    if df_fs.empty or 'type' not in df_fs.columns:
        return None
    for kw in type_keywords:
        mask = df_fs['type'].str.contains(kw, na=False, case=False)
        sub = df_fs[mask]
        if not sub.empty:
            val = pd.to_numeric(sub['value'], errors='coerce').dropna()
            if not val.empty:
                return float(val.iloc[-1])
    return None


def _extract_valuation(df_val: pd.DataFrame, col: str) -> float | None:
    """從 valuation_indicator 取最新一筆指定欄位。"""
    if df_val.empty or col not in df_val.columns:
        return None
    s = pd.to_numeric(df_val[col], errors='coerce').dropna()
    return float(s.iloc[-1]) if not s.empty else None


# ── 優化②：跨族群股票結果快取（同一 stock_id 不重複下載）────────
_stock_cache: dict = {}
_cache_lock  = threading.Lock()

# ============================================================
# 4. 單股分析 — 資料清洗 + 技術指標 + 基本面 + 評分
# ============================================================
def analyze_single_stock(stock_id: str, stock_name: str) -> dict:
    # 命中快取則直接回傳（copy 避免外部修改快取）
    with _cache_lock:
        if stock_id in _stock_cache:
            print(f"   ⚡ [{stock_id}] 快取命中，跳過重複下載")
            return dict(_stock_cache[stock_id])

    result: dict = {
        "stock_id": stock_id, "stock_name": stock_name,
        "score": 50, "status": "資料不足", "error": None,
    }
    try:
        # ── 並行下載 ──────────────────────────────────────────
        data: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
            futs = [
                ex.submit(_fetch, "price",        api.taiwan_stock_daily,                     stock_id),
                ex.submit(_fetch, "financial",    api.taiwan_stock_financial_statement,        stock_id),
                ex.submit(_fetch_per,             stock_id),
                ex.submit(_fetch, "revenue",      api.taiwan_stock_month_revenue,              stock_id),
                ex.submit(_fetch, "institutional", api.taiwan_stock_institutional_investors,   stock_id),
                ex.submit(_fetch, "margin",       api.taiwan_stock_margin_purchase_short_sale, stock_id),
                ex.submit(_fetch_tdcc,            stock_id),
            ]
            for f in concurrent.futures.as_completed(futs):
                label, df = f.result()
                data[label] = df

        # ── 價格清洗 ──────────────────────────────────────────
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

        # 移動平均 & 布林
        dp['MA5']      = dp['Close'].rolling(5).mean()
        dp['MA10']     = dp['Close'].rolling(10).mean()
        dp['MA20']     = dp['Close'].rolling(20).mean()
        dp['MA60']     = dp['Close'].rolling(60).mean()
        dp['STD20']    = dp['Close'].rolling(20).std()
        dp['BB_Upper'] = dp['MA20'] + 2 * dp['STD20']
        dp['BB_Lower'] = dp['MA20'] - 2 * dp['STD20']

        # ATR
        dp['H-L']   = dp['High'] - dp['Low']
        dp['H-PC']  = abs(dp['High'] - dp['Close'].shift(1))
        dp['L-PC']  = abs(dp['Low']  - dp['Close'].shift(1))
        dp['TR']    = dp[['H-L', 'H-PC', 'L-PC']].max(axis=1)
        dp['ATR14'] = dp['TR'].rolling(14).mean()

        # RSI
        delta = dp['Close'].diff()
        gain  = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
        loss  = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        dp['RSI14'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

        # MACD
        dp['EMA12']       = dp['Close'].ewm(span=12, adjust=False).mean()
        dp['EMA26']       = dp['Close'].ewm(span=26, adjust=False).mean()
        dp['MACD']        = dp['EMA12'] - dp['EMA26']
        dp['MACD_Signal'] = dp['MACD'].ewm(span=9, adjust=False).mean()
        dp['MACD_Hist']   = dp['MACD'] - dp['MACD_Signal']

        # 支撐/壓力
        local_max   = argrelextrema(dp['High'].values, np.greater_equal, order=15)[0]
        local_min   = argrelextrema(dp['Low'].values,  np.less_equal,    order=15)[0]
        resistances = dp['High'].iloc[local_max].tail(2).values
        supports    = dp['Low'].iloc[local_min].tail(2).values

        last = dp.iloc[-1]

        # ── 法人清洗 ──────────────────────────────────────────
        fi_5d = it_5d = 0.0
        df_inst = data.get("institutional", pd.DataFrame())
        if not df_inst.empty:
            di = df_inst.rename(columns={'date': 'Date'}).copy()
            di['Date'] = pd.to_datetime(di['Date'])
            di['net']  = pd.to_numeric(di['buy'], errors='coerce') - pd.to_numeric(di['sell'], errors='coerce')
            ip = di.pivot_table(index='Date', columns='name', values='net', aggfunc='sum').fillna(0)
            for c in ['Foreign_Investor', 'Investment_Trust']:
                if c not in ip.columns: ip[c] = 0
            fi_5d = float(ip['Foreign_Investor'].tail(5).sum())
            it_5d = float(ip['Investment_Trust'].tail(5).sum())

        # ── 月營收清洗 ────────────────────────────────────────
        rev_yoy = latest_rev = None
        df_rev = data.get("revenue", pd.DataFrame())
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

        # ── 基本面清洗（財務報表）────────────────────────────
        df_fs = data.get("financial", pd.DataFrame())
        eps          = _extract_fs_value(df_fs, ["EPS", "每股盈餘", "基本每股盈餘"])
        gross_profit = _extract_fs_value(df_fs, ["GrossProfit", "毛利", "營業毛利"])
        revenue_fs   = _extract_fs_value(df_fs, ["Revenue", "營業收入", "收入"])
        op_income    = _extract_fs_value(df_fs, ["OperatingIncome", "營業利益", "營業淨利"])

        gross_margin = round(gross_profit / revenue_fs * 100, 1) if gross_profit and revenue_fs else None
        op_margin    = round(op_income    / revenue_fs * 100, 1) if op_income    and revenue_fs else None

        # ── 估值指標（P/E, P/B）──────────────────────────────
        df_val = data.get("valuation", pd.DataFrame())
        # FinMind valuation_indicator 欄位名稱: PER, PBR
        pe = _extract_valuation(df_val, "PER")
        pb = _extract_valuation(df_val, "PBR")
        # 若 API 無資料，以股價/EPS 自算 P/E
        if pe is None and eps and eps != 0:
            pe = round(float(last['Close']) / abs(eps), 1)

        # ── 多因子評分 ────────────────────────────────────────
        score = 50

        # 技術面
        if last['Close'] > last['MA20'] and last['MA20'] > last['MA60']: score += 10
        elif last['Close'] < last['MA20'] and last['MA20'] < last['MA60']: score -= 15

        if last['MACD_Hist'] > 0 and dp['MACD_Hist'].iloc[-2] <= 0:  score += 10
        elif last['MACD_Hist'] < 0: score -= 5

        v5_mean = dp['Volume'].tail(5).mean()
        if v5_mean > 0 and last['Volume'] > v5_mean * 1.5 and last['Close'] > last['Open']:
            score += 5

        # 法人面
        score += 10 if it_5d > 0 else -5

        # 基本面
        if rev_yoy is not None:
            score += 15 if rev_yoy > 0 else (-10 if rev_yoy < 0 else 0)
        if gross_margin is not None and gross_margin > 30: score += 5
        if op_margin    is not None and op_margin    > 10: score += 5

        score = max(0, min(100, score))

        status_map = [
            (75, "強勢多頭"), (60, "震盪偏多"),
            (40, "弱勢盤整"), (0,  "空頭啟動"),
        ]
        status = next(v for threshold, v in status_map if score >= threshold)

        # ── 封裝清洗後純量結果 ────────────────────────────────
        def _r(v, n=2):
            return round(float(v), n) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None

        result.update({
            "score":    score,
            "status":   status,
            # 技術
            "close":       _r(last['Close']),
            "ma20":        _r(last['MA20']),
            "ma60":        _r(last['MA60']),
            "rsi14":       _r(last['RSI14'], 1),
            "macd_hist":   _r(last['MACD_Hist'], 3),
            "atr14":       _r(last['ATR14']),
            "bb_upper":    _r(last['BB_Upper']),
            "bb_lower":    _r(last['BB_Lower']),
            "volume_ratio":_r(last['Volume'] / v5_mean, 2) if v5_mean > 0 else None,
            "resistance":  _r(resistances[-1]) if len(resistances) > 0 else None,
            "support":     _r(supports[-1])    if len(supports)    > 0 else None,
            "stop_loss":   _r(float(last['Close']) - 1.5 * float(last['ATR14']))
                            if _r(last['ATR14']) else None,
            "macd_cross":  bool(last['MACD_Hist'] > 0 and dp['MACD_Hist'].iloc[-2] <= 0),
            # 法人
            "fi_5d": round(fi_5d / 1000, 1),
            "it_5d": round(it_5d / 1000, 1),
            # 月營收
            "latest_rev": _r(latest_rev / 1e8, 2) if latest_rev else None,
            "rev_yoy":    _r(rev_yoy, 1)           if rev_yoy is not None else None,
            # 基本面
            "eps":          _r(eps, 2)          if eps is not None else None,
            "gross_margin": gross_margin,
            "op_margin":    op_margin,
            # 估值
            "pe": _r(pe, 1) if pe is not None else None,
            "pb": _r(pb, 2) if pb is not None else None,
        })

    except Exception as e:
        result["error"] = str(e)

    # 寫入快取
    with _cache_lock:
        _stock_cache[stock_id] = dict(result)
    return result


# ============================================================
# 5. 單股報告文字生成（AI 可讀）
# ============================================================
def _rsi_label(rsi):
    if rsi is None: return "N/A"
    if rsi > 70: return f"{rsi}（超買過熱⚠️）"
    if rsi < 30: return f"{rsi}（超賣區間💡）"
    return f"{rsi}（中性）"

def generate_stock_text(r: dict) -> str:
    sid, sname = r['stock_id'], r['stock_name']
    if r.get("error"):
        return f"[{sid} {sname}]\n  ⚠️  分析失敗：{r['error']}\n"

    lines = [
        f"[{sid} {sname}]  評分：{r['score']}/100  格局：{r['status']}",
        f"  ── 技術面 ──",
        f"  收盤價：{r['close']}  MA20：{r['ma20']}  MA60：{r['ma60']}",
        f"  RSI14：{_rsi_label(r['rsi14'])}",
        f"  MACD：{'🔔 黃金交叉翻多' if r.get('macd_cross') else str(r['macd_hist'])}",
        f"  量比（vs 5MA）：{r['volume_ratio']}x",
        f"  布林通道：上軌 {r['bb_upper']}  下軌 {r['bb_lower']}",
        f"  近期壓力：{r['resistance'] or 'N/A'}  近期支撐：{r['support'] or 'N/A'}"
        f"  建議停損：{r['stop_loss'] or 'N/A'}（-1.5 ATR）",
        f"  ── 籌碼面 ──",
        f"  外資近5日：{r['fi_5d']} 張  投信近5日：{r['it_5d']} 張",
        f"  ── 基本面 ──",
        f"  EPS：{r['eps'] or 'N/A'}  毛利率：{r['gross_margin'] or 'N/A'}%  營益率：{r['op_margin'] or 'N/A'}%",
        f"  P/E：{r['pe'] or 'N/A'}  P/B：{r['pb'] or 'N/A'}",
        f"  最新月營收：{(str(r['latest_rev'])+' 億') if r['latest_rev'] else 'N/A'}"
        f"  年增率：{(str(r['rev_yoy'])+'%') if r['rev_yoy'] is not None else 'N/A'}",
    ]

    if   r['status'] == "強勢多頭": lines.append("  📈 建議：趨勢向上，沿 MA5/MA10 分批佈局，跌破 MA20 停損。")
    elif r['status'] == "空頭啟動": lines.append("  📉 建議：趨勢偏空，等待 RSI<30 且反彈跡象再短線試單。")
    else:                            lines.append("  ⚖️  建議：方向不明，待帶量突破布林上軌後右側進場。")

    return "\n".join(lines)


# ============================================================
# 6. 族群 TXT 報告組合
# ============================================================
def generate_group_report(group_name: str, results: list) -> str:
    sep = "=" * 55
    lines = [sep, f"  族群分析報告：{group_name}", f"  產生時間：{TIMESTAMP_STR}", sep, ""]

    valid = [r for r in results if not r.get("error")]
    if valid:
        ranked = sorted(valid, key=lambda x: x['score'], reverse=True)
        lines.append("【族群評分排行】")
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(ranked, 1):
            icon = medals[i-1] if i <= 3 else f"  {i}."
            lines.append(f"  {icon} {r['stock_id']} {r['stock_name']}  "
                         f"評分：{r['score']}/100  格局：{r['status']}")
        lines.append("")

        avg = round(sum(r['score'] for r in valid) / len(valid), 1)
        bull = sum(1 for r in valid if r['status'] in ("強勢多頭", "震盪偏多"))
        bear = sum(1 for r in valid if r['status'] == "空頭啟動")
        lines += ["【族群整體訊號】",
                  f"  平均評分：{avg}/100",
                  f"  偏多：{bull} 支  偏空：{bear} 支  中性：{len(valid)-bull-bear} 支", ""]

    lines.append("【個股詳細分析】\n")
    for r in results:
        lines.append(generate_stock_text(r))
        lines.append("")

    lines += [sep, "⚠️  本報告為量化模型輸出，僅供 AI 輔助分析參考，不構成投資建議。", sep]
    return "\n".join(lines)


# ============================================================
# 7. 族群 HTML 視覺化比較表
# ============================================================
def _score_color(score) -> str:
    if score is None: return "#6c757d"
    if score >= 75:   return "#198754"   # green
    if score >= 60:   return "#0d6efd"   # blue
    if score >= 40:   return "#ffc107"   # yellow
    return "#dc3545"                      # red

def _rsi_color(rsi) -> str:
    if rsi is None: return ""
    if rsi > 70: return "background:#fff3cd;"  # warm
    if rsi < 30: return "background:#cfe2ff;"  # cool
    return ""

def _yoy_color(yoy) -> str:
    if yoy is None: return ""
    return "color:#198754;font-weight:600;" if yoy > 0 else ("color:#dc3545;font-weight:600;" if yoy < 0 else "")

def _na(v, suffix="") -> str:
    return f"{v}{suffix}" if v is not None else "<span style='color:#aaa'>—</span>"

def generate_group_html(group_name: str, results: list) -> str:
    valid  = [r for r in results if not r.get("error")]
    ranked = sorted(valid, key=lambda x: x['score'], reverse=True) if valid else []
    medals = {r['stock_id']: "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else ""
              for i, r in enumerate(ranked)}

    # ── table rows ──────────────────────────────────────────
    rows_html = ""
    for r in results:
        sid, sname = r['stock_id'], r['stock_name']
        if r.get("error"):
            rows_html += (
                f"<tr><td><b>{sid}</b></td><td>{sname}</td>"
                f"<td colspan='14' style='color:#dc3545;'>⚠️ {r['error']}</td></tr>\n"
            )
            continue

        sc    = r['score']
        sbg   = _score_color(sc)
        medal = medals.get(sid, "")

        # MACD 標示
        macd_str = "🔔 黃金叉" if r.get('macd_cross') else _na(r['macd_hist'])

        # Volume ratio badge
        vr = r['volume_ratio']
        vr_str = f"<span style='color:#6f42c1;font-weight:600;'>{vr}x</span>" if vr and vr >= 1.5 else _na(vr, "x")

        rows_html += f"""
        <tr>
          <td style='font-weight:700;'>{medal} {sid}</td>
          <td>{sname}</td>
          <td style='background:{sbg};color:#fff;font-weight:700;text-align:center;border-radius:4px;'>{sc}</td>
          <td style='text-align:center;'><span style='background:{sbg};color:#fff;padding:2px 6px;border-radius:10px;font-size:.8em;'>{r['status']}</span></td>
          <td style='text-align:right;font-weight:600;'>{_na(r['close'])}</td>
          <td style='text-align:right;'>{_na(r['ma20'])}</td>
          <td style='text-align:right;'>{_na(r['ma60'])}</td>
          <td style='text-align:right;{_rsi_color(r["rsi14"])}'>{_na(r['rsi14'])}</td>
          <td style='text-align:right;'>{macd_str}</td>
          <td style='text-align:right;'>{vr_str}</td>
          <td style='text-align:right;{_yoy_color(r["rev_yoy"])}'>{_na(r['rev_yoy'], '%')}</td>
          <td style='text-align:right;'>{_na(r['eps'])}</td>
          <td style='text-align:right;'>{_na(r['gross_margin'], '%')}</td>
          <td style='text-align:right;'>{_na(r['op_margin'], '%')}</td>
          <td style='text-align:right;'>{_na(r['pe'])}</td>
          <td style='text-align:right;'>{_na(r['pb'])}</td>
          <td style='text-align:right;'>{r['it_5d']} 張</td>
          <td style='text-align:right;'>{r['fi_5d']} 張</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>{group_name} 族群分析 {TODAY_STR}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', 'PingFang TC', sans-serif;
    background: #0f1117;
    color: #e4e6ea;
    padding: 24px;
    font-size: 14px;
  }}
  h1 {{
    font-size: 1.5rem;
    font-weight: 700;
    margin-bottom: 6px;
    background: linear-gradient(90deg, #4fc3f7, #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }}
  .meta {{ color: #9ca3af; font-size: .85rem; margin-bottom: 20px; }}
  .summary-row {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
  .card {{
    background: #1e2130;
    border-radius: 10px;
    padding: 14px 20px;
    min-width: 130px;
    text-align: center;
    border: 1px solid #2d3250;
  }}
  .card .val {{ font-size: 1.6rem; font-weight: 700; }}
  .card .lbl {{ color: #9ca3af; font-size: .78rem; margin-top: 2px; }}
  .green {{ color: #34d399; }} .blue {{ color: #60a5fa; }}
  .yellow {{ color: #fbbf24; }} .red {{ color: #f87171; }}
  .table-wrap {{ overflow-x: auto; border-radius: 10px; background: #1e2130; border: 1px solid #2d3250; }}
  table {{ border-collapse: collapse; width: 100%; white-space: nowrap; }}
  th {{
    background: #262b40;
    color: #9ca3af;
    font-weight: 600;
    font-size: .78rem;
    text-transform: uppercase;
    letter-spacing: .05em;
    padding: 10px 12px;
    border-bottom: 1px solid #2d3250;
    text-align: left;
  }}
  td {{
    padding: 9px 12px;
    border-bottom: 1px solid #1a1d2e;
    vertical-align: middle;
    font-size: .88rem;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #252b3d; }}
  .legend {{
    margin-top: 18px;
    font-size: .8rem;
    color: #6b7280;
    display: flex; gap: 20px; flex-wrap: wrap;
  }}
  .legend span {{ display: flex; align-items: center; gap: 6px; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
</style>
</head>
<body>
<h1>📊 {group_name} 族群分析報告</h1>
<div class="meta">產生時間：{TIMESTAMP_STR} &nbsp;|&nbsp; 資料來源：FinMind / TDCC</div>
"""

    # 統計摘要卡片
    if valid:
        avg_sc = round(sum(r['score'] for r in valid) / len(valid), 1)
        bull   = sum(1 for r in valid if r['status'] in ("強勢多頭", "震盪偏多"))
        bear   = sum(1 for r in valid if r['status'] == "空頭啟動")
        top    = ranked[0] if ranked else None
        top_str= f"{top['stock_id']} {top['stock_name']} ({top['score']})" if top else "—"
        avg_cls = "green" if avg_sc >= 65 else ("yellow" if avg_sc >= 50 else "red")
        html += f"""<div class="summary-row">
  <div class="card"><div class="val {avg_cls}">{avg_sc}</div><div class="lbl">族群平均評分</div></div>
  <div class="card"><div class="val green">{bull}</div><div class="lbl">偏多個股</div></div>
  <div class="card"><div class="val red">{bear}</div><div class="lbl">偏空個股</div></div>
  <div class="card"><div class="val blue">{len(valid)-bull-bear}</div><div class="lbl">中性個股</div></div>
  <div class="card" style="min-width:200px"><div class="val green" style="font-size:1rem;">{top_str}</div><div class="lbl">族群領頭羊</div></div>
</div>"""

    # 比較表
    html += f"""<div class="table-wrap">
<table>
<thead>
<tr>
  <th>代號</th><th>名稱</th>
  <th>評分</th><th>格局</th>
  <th>收盤</th><th>MA20</th><th>MA60</th>
  <th>RSI</th><th>MACD</th><th>量比</th>
  <th>營收YoY</th><th>EPS</th><th>毛利率</th><th>營益率</th>
  <th>P/E</th><th>P/B</th><th>投信5日</th><th>外資5日</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</div>
<div class="legend">
  <span><span class="dot" style="background:#198754"></span>強勢多頭 (≥75)</span>
  <span><span class="dot" style="background:#0d6efd"></span>震盪偏多 (60~74)</span>
  <span><span class="dot" style="background:#ffc107"></span>弱勢盤整 (40~59)</span>
  <span><span class="dot" style="background:#dc3545"></span>空頭啟動 (&lt;40)</span>
  <span>🟡 RSI&gt;70 注意過熱 &nbsp; 🔵 RSI&lt;30 超賣zone</span>
</div>
<p style="margin-top:16px;color:#4b5563;font-size:.78rem;">⚠️ 本報告為量化模型輸出，僅供 AI 輔助分析參考，不構成投資建議。</p>
</body>
</html>"""
    return html


# ============================================================
# 8. Telegram 傳送（sendDocument）
# ============================================================
def _tg_send(file_path: Path, caption: str, mime: str = "text/plain") -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("   ⚠️  未設定 Telegram 環境變數，跳過傳送。")
        return False
    url  = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument"
    data = {"chat_id": TG_CHAT_ID, "caption": caption}
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(url, data=data,
                                 files={"document": (file_path.name, f, mime)},
                                 timeout=60)
        if resp.status_code == 200:
            print(f"   📤 Telegram 傳送成功：{file_path.name}")
            return True
        print(f"   ❌ Telegram 失敗 [{resp.status_code}]：{resp.text[:200]}")
    except Exception as e:
        print(f"   ❌ Telegram 例外：{e}")
    return False

def _tg_msg(text: str) -> bool:
    """透過 Telegram sendMessage 傳送純文字訊息。"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    url  = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url,
                             json={"chat_id": TG_CHAT_ID, "text": text},
                             timeout=30)
        return resp.status_code == 200
    except Exception:
        return False


def main():
    print(f"\n{'='*60}")
    print(f"  🚀 TwinStock 族群分析系統啟動")
    print(f"  分析時間：{TIMESTAMP_STR}")
    print(f"  分析族群數：{len(INDUSTRY_GROUPS)}")
    print(f"{'='*60}\n")

    for group_name, stocks in INDUSTRY_GROUPS.items():
        print(f"\n📊 開始分析族群：【{group_name}】（共 {len(stocks)} 支）")
        # ── 優化③：族群內個股並行分析 ───────────────────────────
        # 有 Token 用 3 執行緒；無 Token 降為 1（序列）避免限流
        workers = 3 if FINMIND_TOKEN else 1
        results_map: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            fut_to_sid = {
                pool.submit(analyze_single_stock, sid, sname): sid
                for sid, sname in stocks.items()
            }
            for fut in concurrent.futures.as_completed(fut_to_sid):
                r = fut.result()
                results_map[r['stock_id']] = r
                if r.get("error"):
                    err_msg = (
                        f"⚠️ TwinStock 下載失敗\n"
                        f"族群：{group_name}\n"
                        f"股票：{r['stock_id']} {r['stock_name']}\n"
                        f"錯誤：{r['error']}\n"
                        f"時間：{TIMESTAMP_STR}"
                    )
                    print(f"      ⚠️  {r['stock_id']} {r['stock_name']} 失敗，已通知 TG")
                    _tg_msg(err_msg)
                else:
                    print(f"      ✓ {r['stock_id']} {r['stock_name']} → 評分:{r['score']}/100 格局:{r['status']}")

        # 保持 industry_groups 定義的順序
        results = [results_map[sid] for sid in stocks if sid in results_map]

        safe_name = group_name.replace("/", "_").replace("\\", "_")

        # ── TXT 報告 ──────────────────────────────────────────
        txt_path = REPORT_DIR / f"{safe_name}_{TODAY_STR}.txt"
        txt_path.write_text(generate_group_report(group_name, results), encoding="utf-8-sig")
        print(f"   💾 TXT 儲存：{txt_path}")
        _tg_send(txt_path, f"📄 【{group_name}】AI 分析原文 {TIMESTAMP_STR}")

        # ── HTML 視覺化報告 ───────────────────────────────────
        html_path = REPORT_DIR / f"{safe_name}_{TODAY_STR}.html"
        html_path.write_text(generate_group_html(group_name, results), encoding="utf-8")
        print(f"   🌐 HTML 儲存：{html_path}")
        _tg_send(html_path, f"📊 【{group_name}】視覺化比較表 {TIMESTAMP_STR}", mime="text/html")

    print(f"\n{'='*60}")
    print(f"  ✅ 所有族群分析完成！")
    print(f"  報告目錄：{REPORT_DIR.resolve()}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
