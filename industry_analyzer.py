import os
import io
import csv
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

import json as _json

TW_TZ         = datetime.timezone(datetime.timedelta(hours=8))
START_DATE    = (datetime.datetime.now(TW_TZ) - datetime.timedelta(days=3 * 365)).strftime("%Y-%m-%d")
REPORT_DIR    = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)
CACHE_DIR     = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ── 載入設定檔 config.json ─────────────────────────────────────────
DEFAULT_CONFIG = {
  "scoring": {
    "tech": {
      "ma_bullish_score": 10, "ma_bearish_score": -15,
      "macd_cross_score": 10, "macd_bear_score": -5,
      "volume_breakout_multiplier": 1.5, "volume_breakout_score": 5,
      "price_new_high_days": 60, "price_new_high_score": 5
    },
    "institutional": {
      "it_buy_score": 10, "it_sell_score": -5,
      "foreign_buy_score": 10, "foreign_sell_score": -8,
      "all_inst_agree_score": 5
    },
    "margin": {
      "margin_drop_threshold": -500, "margin_drop_score": 5,
      "margin_surge_threshold": 2000, "margin_surge_score": -5,
      "short_ratio_threshold": 20, "short_ratio_score": -5
    },
    "chip": {"tdcc_high_threshold": 60, "tdcc_high_score": 10, "tdcc_mid_threshold": 40, "tdcc_mid_score": 5, "tdcc_low_threshold": 20, "tdcc_low_score": -5},
    "fundamental": {
      "rev_yoy_growth_score": 10, "rev_yoy_drop_score": -10,
      "eps_yoy_growth_score": 10, "eps_yoy_drop_score": -5,
      "gross_margin_threshold": 30, "gross_margin_score": 5,
      "op_margin_threshold": 10, "op_margin_score": 5
    }
  },
  "status_thresholds": {
    "strong_bull": 65, "bull_bias": 50, "weak_consolidation": 40, "bear_start": 0
  },
  "indicators": {
    "rsi_overbought": 70, "rsi_oversold": 30, "stop_loss_atr_multiple": 1.5
  }
}

config_path = Path("config.json")
if config_path.exists():
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            CONFIG = dict(DEFAULT_CONFIG)
            user_config = _json.load(f)
            # 支援部分覆蓋 (簡單第一層 or 第二層更新)
            for k, v in user_config.items():
                if isinstance(v, dict) and k in CONFIG:
                    CONFIG[k].update(v)
                else:
                    CONFIG[k] = v
    except Exception as e:
        print(f"⚠️  讀取 config.json 失敗 ({e})，使用預設值")
        CONFIG = DEFAULT_CONFIG
else:
    CONFIG = DEFAULT_CONFIG

START_DATE    = (datetime.datetime.now(TW_TZ) - datetime.timedelta(days=3 * 365)).strftime("%Y-%m-%d")
REPORT_DIR    = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)
CACHE_DIR     = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

TODAY_STR     = datetime.datetime.now(TW_TZ).strftime("%Y%m%d")
TIMESTAMP_STR = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M (UTC+8)")

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


def _disk_cache_load(stock_id: str) -> dict | None:
    """讀取當日磁碟快取；若沒有則回傳 None。"""
    path = CACHE_DIR / f"{stock_id}_{TODAY_STR}.json"
    if path.exists():
        try:
            return _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _disk_cache_save(stock_id: str, result: dict):
    """將分析結果寫入磁碟（price_history 直接可序列化）。"""
    path = CACHE_DIR / f"{stock_id}_{TODAY_STR}.json"
    try:
        path.write_text(_json.dumps(result, ensure_ascii=False, default=str),
                        encoding="utf-8")
    except Exception as e:
        print(f"   ⚠️  [{stock_id}] 磁碟快取寫入失敗: {e}")

# ============================================================
# 3.5 族群 Config 合併工具
# ============================================================
import copy as _copy

def _merge_group_config(overrides: dict) -> dict:
    """將族群的 config_overrides 深度合併到全域 CONFIG 的副本並回傳。
    只合併第三層（scoring/section/key），不影響全域設定。"""
    if not overrides:
        return CONFIG
    merged = _copy.deepcopy(CONFIG)
    for section, section_val in overrides.items():          # e.g. "scoring"
        if section not in merged:
            merged[section] = {}
        if isinstance(section_val, dict):
            for sub, sub_val in section_val.items():         # e.g. "fundamental"
                if sub not in merged[section]:
                    merged[section][sub] = {}
                if isinstance(sub_val, dict):
                    merged[section][sub].update(sub_val)     # 更新 key-value
                else:
                    merged[section][sub] = sub_val
        else:
            merged[section] = section_val
    return merged


# ============================================================
# 4. 單股分析 — 資料清洗 + 技術指標 + 基本面 + 評分
# ============================================================
def analyze_single_stock(stock_id: str, stock_name: str,
                          group_config: dict | None = None) -> dict:
    """分析單一股票。group_config 為可選的族群覆蓋 config（由 _merge_group_config 產生）。"""
    # 若有族群 config 則使用，否則用全域 CONFIG
    effective_config = group_config if group_config is not None else CONFIG
    # 1. 記憶體快取（同一與行程內）
    with _cache_lock:
        if stock_id in _stock_cache:
            print(f"   ⚡ [{stock_id}] 記憶體快取命中，跳過重複下載")
            return dict(_stock_cache[stock_id])

    # 2. 磁碟快取（同日不重複下載）
    disk = _disk_cache_load(stock_id)
    if disk is not None:
        print(f"   💾 [{stock_id}] 磁碟快取命中（{TODAY_STR}），跳過下載")
        disk.setdefault("stock_name", stock_name)
        with _cache_lock:
            _stock_cache[stock_id] = disk
        return dict(disk)

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

        # EPS YoY：從財務報表取最近8季EPS，比較最新季與去年同季
        eps_yoy = None
        if not df_fs.empty and 'type' in df_fs.columns and 'date' in df_fs.columns:
            eps_mask = df_fs['type'].str.contains('EPS|每股盈餘|基本每股盈餘', na=False, case=False)
            df_eps_hist = df_fs[eps_mask].copy()
            if not df_eps_hist.empty:
                df_eps_hist['date'] = pd.to_datetime(df_eps_hist['date'])
                df_eps_hist['value'] = pd.to_numeric(df_eps_hist['value'], errors='coerce')
                df_eps_hist = df_eps_hist.dropna(subset=['value']).sort_values('date')
                if len(df_eps_hist) >= 5:  # 至少需要5筆才能算去年同季
                    latest_eps_val = float(df_eps_hist['value'].iloc[-1])
                    yoy_eps_val    = float(df_eps_hist['value'].iloc[-5])  # 去年同季（約4季前）
                    if yoy_eps_val != 0:
                        eps_yoy = round((latest_eps_val - yoy_eps_val) / abs(yoy_eps_val) * 100, 1)

        # -- margin data (融資融券) --
        margin_5d = sr_ratio = None
        df_margin = data.get("margin", pd.DataFrame())
        if not df_margin.empty:
            mb_col = next((c for c in ['MarginPurchaseTodayBalance', 'margin_purchase_today_balance']
                           if c in df_margin.columns), None)
            sb_col = next((c for c in ['ShortSaleTodayBalance', 'short_sale_today_balance']
                           if c in df_margin.columns), None)
            if mb_col:
                mb = pd.to_numeric(df_margin[mb_col], errors='coerce').dropna()
                if len(mb) >= 6:
                    margin_5d = int(mb.iloc[-1] - mb.iloc[-6])
                if sb_col and not mb.empty and float(mb.iloc[-1]) > 0:
                    sb = pd.to_numeric(df_margin[sb_col], errors='coerce').dropna()
                    if not sb.empty:
                        sr_ratio = round(float(sb.iloc[-1]) / float(mb.iloc[-1]) * 100, 1)

        # ── 估值指標（P/E, P/B）──────────────────────────────
        df_val = data.get("valuation", pd.DataFrame())
        # FinMind valuation_indicator 欄位名稱: PER, PBR
        pe = _extract_valuation(df_val, "PER")
        pb = _extract_valuation(df_val, "PBR")
        # 若 API 無資料，以股價/EPS 自算 P/E
        if pe is None and eps and eps != 0:
            pe = round(float(last['Close']) / abs(eps), 1)

        # ── TDCC 籌碼清洗（千張大戶持股比例與日期）──────────────
        tdcc_1k_ratio = None
        tdcc_date = None
        df_tdcc = data.get("tdcc", pd.DataFrame())
        if df_tdcc is None or not isinstance(df_tdcc, pd.DataFrame):
            df_tdcc = pd.DataFrame()

        # 動態尋找比例與日期欄位
        ratio_col = next((c for c in ['占總計比例', '占集保庫存數比例%', '比例'] if c in df_tdcc.columns), None)
        date_col = next((c for c in ['資料日期', '日期', 'Date'] if c in df_tdcc.columns), None)

        if not df_tdcc.empty and '持股分級' in df_tdcc.columns and ratio_col:
            # 持股分級 15 通常代表 1,000,000 股（1,000張）以上
            df_tdcc['持股分級'] = pd.to_numeric(df_tdcc['持股分級'], errors='coerce')
            df_tdcc[ratio_col] = pd.to_numeric(df_tdcc[ratio_col], errors='coerce')
            # 取出分級 15（千張大戶）的資料
            df_1k = df_tdcc[df_tdcc['持股分級'] == 15]
            if not df_1k.empty:
                # TDCC 開放資料通常只有最新一週，直接取最新一筆
                tdcc_1k_ratio = float(df_1k[ratio_col].iloc[-1])
                if date_col:
                    tdcc_date = str(df_1k[date_col].iloc[-1])

        # ── 多因子評分 ────────────────────────────────────────
        score = 50
        c_score = effective_config["scoring"]

        # 【技術面】
        # 均線多空排列
        if last['Close'] > last['MA20'] and last['MA20'] > last['MA60']:
            score += c_score["tech"]["ma_bullish_score"]
        elif last['Close'] < last['MA20'] and last['MA20'] < last['MA60']:
            score += c_score["tech"]["ma_bearish_score"]

        # MACD
        if last['MACD_Hist'] > 0 and dp['MACD_Hist'].iloc[-2] <= 0:
            score += c_score["tech"]["macd_cross_score"]
        elif last['MACD_Hist'] < 0:
            score += c_score["tech"]["macd_bear_score"]

        # 帶量突破
        v5_mean = dp['Volume'].tail(5).mean()
        if v5_mean > 0 and last['Volume'] > v5_mean * c_score["tech"]["volume_breakout_multiplier"] and last['Close'] > last['Open']:
            score += c_score["tech"]["volume_breakout_score"]

        # ★ 股價 N 日新高（突破前高）
        nh_days = int(c_score["tech"].get("price_new_high_days", 60))
        price_is_new_high = False
        if len(dp) >= nh_days:
            recent_high = dp['High'].iloc[-(nh_days+1):-1].max()  # 排除今日
            if float(last['Close']) >= recent_high:
                score += c_score["tech"]["price_new_high_score"]
                price_is_new_high = True

        # 【法人面】
        # 投信
        score += c_score["institutional"]["it_buy_score"] if it_5d > 0 else c_score["institutional"]["it_sell_score"]
        # ★ 外資
        score += c_score["institutional"]["foreign_buy_score"] if fi_5d > 0 else c_score["institutional"]["foreign_sell_score"]
        # ★ 三大法人同步買超（外資+投信同步）
        if fi_5d > 0 and it_5d > 0:
            score += c_score["institutional"]["all_inst_agree_score"]

        # 【融資融券面】（雙向邏輯）
        if margin_5d is not None:
            if margin_5d < c_score["margin"]["margin_drop_threshold"]:
                # ★ 融資縮減：籌碼趨乾淨，正向加分
                score += c_score["margin"]["margin_drop_score"]
            elif margin_5d > c_score["margin"].get("margin_surge_threshold", 2000):
                # ★ 融資暴增：散戶追高，負向扣分
                score += c_score["margin"]["margin_surge_score"]
        if sr_ratio is not None and sr_ratio > c_score["margin"]["short_ratio_threshold"]:
            score += c_score["margin"]["short_ratio_score"]

        # 【籌碼面】TDCC 千張大戶
        if tdcc_1k_ratio is not None:
            if tdcc_1k_ratio >= c_score["chip"]["tdcc_high_threshold"]:
                score += c_score["chip"]["tdcc_high_score"]    # 籌碼非常集中
            elif tdcc_1k_ratio >= c_score["chip"]["tdcc_mid_threshold"]:
                score += c_score["chip"]["tdcc_mid_score"]     # 籌碼偏集中
            elif tdcc_1k_ratio < c_score["chip"]["tdcc_low_threshold"]:
                score += c_score["chip"]["tdcc_low_score"]     # 籌碼渙散

        # 【基本面】
        # 月營收 YoY
        if rev_yoy is not None:
            if rev_yoy > 0:   score += c_score["fundamental"]["rev_yoy_growth_score"]
            elif rev_yoy < 0: score += c_score["fundamental"]["rev_yoy_drop_score"]
        # ★ EPS YoY（季度比較）
        if eps_yoy is not None:
            if eps_yoy > 0:   score += c_score["fundamental"].get("eps_yoy_growth_score", 10)
            elif eps_yoy < 0: score += c_score["fundamental"].get("eps_yoy_drop_score", -5)
        # 毛利率 / 營益率
        if gross_margin is not None and gross_margin > c_score["fundamental"]["gross_margin_threshold"]:
            score += c_score["fundamental"]["gross_margin_score"]
        if op_margin is not None and op_margin > c_score["fundamental"]["op_margin_threshold"]:
            score += c_score["fundamental"]["op_margin_score"]

        score = max(0, min(100, score))

        c_status = CONFIG["status_thresholds"]
        status_map = [
            (c_status["strong_bull"], "強勢多頭"), (c_status["bull_bias"], "震盪偏多"),
            (c_status["weak_consolidation"], "弱勢盤整"), (c_status["bear_start"],  "空頭啟動"),
        ]
        status_map.sort(key=lambda x: x[0], reverse=True)
        status = next((v for threshold, v in status_map if score >= threshold), "資料不足")

        # ── 封裝清洗後純量結果 ────────────────────────────────
        def _r(v, n=2):
            return round(float(v), n) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None

        # ── 歷史價格序列（供 HTML 走勢圖使用，最近 180 筆）────
        hist_cols = ['Open', 'High', 'Low', 'Close', 'Volume',
                     'MA5', 'MA10', 'MA20', 'MA60', 'BB_Upper', 'BB_Lower']
        dp_hist = dp[hist_cols].tail(180).copy()
        # 把 NaN 換成 None，以便 JSON 序列化
        dp_hist = dp_hist.where(dp_hist.notna(), other=None)
        price_history = [
            {
                "date":     idx.strftime("%Y-%m-%d"),
                "open":     round(float(row['Open']),  2) if row['Open']     is not None else None,
                "high":     round(float(row['High']),  2) if row['High']     is not None else None,
                "low":      round(float(row['Low']),   2) if row['Low']      is not None else None,
                "close":    round(float(row['Close']), 2) if row['Close']    is not None else None,
                "volume":   int(row['Volume'])              if row['Volume']   is not None else None,
                "ma5":      round(float(row['MA5']),   2) if row['MA5']      is not None else None,
                "ma10":     round(float(row['MA10']),  2) if row['MA10']     is not None else None,
                "ma20":     round(float(row['MA20']),  2) if row['MA20']     is not None else None,
                "ma60":     round(float(row['MA60']),  2) if row['MA60']     is not None else None,
                "bb_upper": round(float(row['BB_Upper']), 2) if row['BB_Upper'] is not None else None,
                "bb_lower": round(float(row['BB_Lower']), 2) if row['BB_Lower'] is not None else None,
            }
            for idx, row in dp_hist.iterrows()
        ]

        result.update({
            "score":    score,
            "status":   status,
            # 技術
            "close":          _r(last['Close']),
            "ma20":           _r(last['MA20']),
            "ma60":           _r(last['MA60']),
            "rsi14":          _r(last['RSI14'], 1),
            "macd_hist":      _r(last['MACD_Hist'], 3),
            "atr14":          _r(last['ATR14']),
            "bb_upper":       _r(last['BB_Upper']),
            "bb_lower":       _r(last['BB_Lower']),
            "volume_ratio":   _r(last['Volume'] / v5_mean, 2) if v5_mean > 0 else None,
            "resistance":     _r(resistances[-1]) if len(resistances) > 0 else None,
            "support":        _r(supports[-1])    if len(supports)    > 0 else None,
            "stop_loss":      _r(float(last['Close']) - effective_config["indicators"]["stop_loss_atr_multiple"] * float(last['ATR14']))
                               if _r(last['ATR14']) else None,
            "macd_cross":     bool(last['MACD_Hist'] > 0 and dp['MACD_Hist'].iloc[-2] <= 0),
            "price_is_new_high": price_is_new_high,          # ★ N日新高
            # 法人
            "fi_5d": round(fi_5d / 1000, 1),
            "it_5d": round(it_5d / 1000, 1),
            # 月營收
            "latest_rev": _r(latest_rev / 1e8, 2) if latest_rev else None,
            "rev_yoy":    _r(rev_yoy, 1)           if rev_yoy is not None else None,
            # 基本面
            "eps":          _r(eps, 2)     if eps     is not None else None,
            "eps_yoy":      _r(eps_yoy, 1) if eps_yoy is not None else None,  # ★ EPS YoY
            "gross_margin": gross_margin,
            "op_margin":    op_margin,
            # 估值
            "pe": _r(pe, 1) if pe is not None else None,
            "pb": _r(pb, 2) if pb is not None else None,
            # 融資融券
            "margin_5d": margin_5d,
            "sr_ratio":  sr_ratio,
            # TDCC 籌碼
            "tdcc_1k_ratio": _r(tdcc_1k_ratio, 2),
            "tdcc_date":     tdcc_date,
            # 歷史走勢序列
            "price_history": price_history,
        })

    except Exception as e:
        result["error"] = str(e)

    # 寫入記憶體 + 磁碟快取
    with _cache_lock:
        _stock_cache[stock_id] = dict(result)
    if not result.get("error"):
        _disk_cache_save(stock_id, result)
    return result


# ============================================================
# 5. 單股報告文字生成（AI 可讀）
# ============================================================
def _rsi_label(rsi):
    if rsi is None: return "N/A"
    if rsi > CONFIG["indicators"]["rsi_overbought"]: return f"{rsi}（超買過熱⚠️）"
    if rsi < CONFIG["indicators"]["rsi_oversold"]: return f"{rsi}（超賣區間💡）"
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
        f"  量比（vs 5MA）：{r['volume_ratio']}x"
        + ("  📈 股價創60日新高" if r.get('price_is_new_high') else ""),
        f"  布林通道：上軌 {r['bb_upper']}  下軌 {r['bb_lower']}",
        f"  近期壓力：{r['resistance'] or 'N/A'}  近期支撐：{r['support'] or 'N/A'}",
        f"  建議停損：{r['stop_loss'] or 'N/A'}（-{CONFIG['indicators']['stop_loss_atr_multiple']} ATR）",
        f"  ── 法人籌碼 ──",
        f"  外資5日：{r['fi_5d']} 張  投信5日：{r['it_5d']} 張",
        f"  融資增減：{(str(r['margin_5d'])+' 張') if r.get('margin_5d') is not None else 'N/A'}",
        f"  券資比：{(str(r['sr_ratio'])+'%') if r.get('sr_ratio') is not None else 'N/A'}",
        f"  ── 基本面 ──",
        f"  EPS：{r['eps'] or 'N/A'}  EPS YoY：{(str(r['eps_yoy'])+'%') if r.get('eps_yoy') is not None else 'N/A'}",
        f"  毛利率：{r['gross_margin'] or 'N/A'}%  營益率：{r['op_margin'] or 'N/A'}%",
        f"  P/E：{r['pe'] or 'N/A'}  P/B：{r['pb'] or 'N/A'}",
        f"  最新月營收：{(str(r['latest_rev'])+' 億') if r['latest_rev'] else 'N/A'}"
        f"  年增率：{(str(r['rev_yoy'])+'%') if r['rev_yoy'] is not None else 'N/A'}",
        f"  ── 籌碼集中度 ──",
        f"  千張大戶持股比例：{(str(r['tdcc_1k_ratio'])+'%') if r.get('tdcc_1k_ratio') is not None else 'N/A'} " + (f"(資料日期: {r['tdcc_date']})" if r.get('tdcc_date') else ""),
    ]

    if   r['status'] == "強勢多頭": lines.append("  📈 建議：趨勢向上，沿 MA5/MA10 分批佈局，跌破 MA20 停損。")
    elif r['status'] == "空頭啟動": lines.append(f"  📉 建議：趨勢偏空，等待 RSI<{CONFIG['indicators']['rsi_oversold']} 且反彈跡象再短線試單。")
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
# 7. 族群 HTML 視覺化比較表（含走勢圖）
# ============================================================
import json as _json

def _score_color(score) -> str:
    if score is None: return "#6c757d"
    c_status = CONFIG["status_thresholds"]
    if score >= c_status["strong_bull"]:   return "#198754"
    if score >= c_status["bull_bias"]:   return "#0d6efd"
    if score >= c_status["weak_consolidation"]:   return "#ffc107"
    return "#dc3545"

def _rsi_color(rsi) -> str:
    if rsi is None: return ""
    if rsi > CONFIG["indicators"]["rsi_overbought"]: return "color:#ffc107;font-weight:600;"
    if rsi < CONFIG["indicators"]["rsi_oversold"]: return "color:#0d6efd;font-weight:600;"
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

    # ── table rows + chart sections ─────────────────────────
    rows_html = ""
    charts_html = ""
    for r in results:
        sid, sname = r['stock_id'], r['stock_name']
        chart_row_id = f"chart-row-{sid}"
        chart_div_id = f"chart-{sid}"

        if r.get("error"):
            rows_html += (
                f"<tr><td><b>{sid}</b></td><td>{sname}</td>"
                f"<td colspan='22' style='color:#dc3545;'>⚠️ {r['error']}</td></tr>\n"
            )
            continue

        sc    = r['score']
        sbg   = _score_color(sc)
        medal = medals.get(sid, "")

        macd_str = "🔔 黃金叉" if r.get('macd_cross') else _na(r['macd_hist'])
        vr = r['volume_ratio']
        vr_thm = CONFIG["scoring"]["tech"]["volume_breakout_multiplier"]
        vr_str = f"<span style='color:#6f42c1;font-weight:600;'>{vr}x</span>" if vr and vr >= vr_thm else _na(vr, "x")

        tdcc_1k_html = _na(r.get('tdcc_1k_ratio'), '%')
        if r.get('tdcc_date'):
            tdcc_1k_html += f"<br><span style='font-size:0.75em;color:#cfcfcf;font-weight:normal;'>{r['tdcc_date']}</span>"

        rows_html += f"""
        <tr class='data-row' onclick="toggleChart('{sid}')" ontouchend="toggleChart('{sid}')" style='cursor:pointer; -webkit-tap-highlight-color: transparent;'>
          <td style='font-weight:700;'>{medal} {sid} <span style='font-size:.75em;color:#60a5fa;'>▼</span></td>
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
          <td style='text-align:right;{("color:#dc3545;font-weight:600;" if (r.get("margin_5d") or 0) < CONFIG["scoring"]["margin"]["margin_drop_threshold"] else "color:#198754;font-weight:600;" if (r.get("margin_5d") or 0) > 0 else "")}'>{_na(r.get('margin_5d'), ' 張')}</td>
          <td style='text-align:right;{("color:#dc3545;font-weight:600;" if (r.get("sr_ratio") or 0) > CONFIG["scoring"]["margin"]["short_ratio_threshold"] else "")}'>{_na(r.get('sr_ratio'), '%')}</td>
          <td style='text-align:right;{("color:#198754;font-weight:700;" if (r.get("tdcc_1k_ratio") or 0) >= CONFIG["scoring"]["chip"]["tdcc_high_threshold"] else "color:#dc3545;font-weight:700;" if (r.get("tdcc_1k_ratio") or 0) < CONFIG["scoring"]["chip"]["tdcc_low_threshold"] else "")}'>{tdcc_1k_html}</td>
        </tr>
        <tr id='{chart_row_id}' style='display:none;'>
          <td colspan='21' style='padding:0;background:#131722;'>
            <div id='{chart_div_id}' style='height:480px;'></div>
          </td>
        </tr>"""

        # 序列化歷史資料並產生 Plotly 初始化 JS
        hist = r.get('price_history', [])
        hist_json = _json.dumps(hist, ensure_ascii=False)
        charts_html += f"""
    chartData['{sid}'] = {hist_json};"""

    # 讀取本地端 Lightweight Charts 的 JS 檔案直接塞入，確保完全離線可用
    script_dir = Path(__file__).parent
    lw_js_path = script_dir / "lightweight-charts.standalone.production.js"
    lw_js_content = ""
    if lw_js_path.exists():
        lw_js_content = lw_js_path.read_text(encoding="utf-8")
    else:
        # 如果沒有本地檔案當備用，還是給一個基本提示
        lw_js_content = "console.error('Local lightweight-charts JS file not found!');"

    # ── HTML skeleton ────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>{group_name} 族群分析 {TODAY_STR}</title>
<script>
{lw_js_content}
</script>
<noscript><p style="color:#f87171;text-align:center;padding:20px;font-size:1.2rem;font-weight:bold;background:#2a1a1a;border:2px solid #f87171;margin:20px;">⚠️ 您的檢視器已停用 JavaScript！<br><br>iPad 的預設「檔案(Files)」或郵件預覽會阻擋 JavaScript 執行，導致圖表無法顯示。<br>請使用「Safari 瀏覽器」或第三方 App (如 Documents by Readdle) 開啟此檔案。</p></noscript>
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
  tr.data-row:hover td {{ background: #1e2a45; }}
  tr.data-row:active td {{ background: #1e2a45; }} /* 增加 active 狀態讓觸控有回饋 */
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
<div class="meta">產生時間：{TIMESTAMP_STR} &nbsp;|&nbsp; 資料來源：FinMind / TDCC &nbsp;|&nbsp; 點擊任意列展開走勢圖</div>
"""

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

    html += f"""<div class="table-wrap">
<table>
<thead>
<tr>
  <th>代號</th><th>名稱</th>
  <th>評分</th><th>格局</th>
  <th>收盤</th><th>MA20</th><th>MA60</th>
  <th>RSI</th><th>MACD</th><th>量比</th>
  <th>營收YoY</th><th>EPS</th><th>毛利率</th><th>營益率</th>
  <th>P/E</th><th>P/B</th><th>投信5日</th><th>外資5日</th><th>融資增減</th><th>券資比</th><th>千張大戶</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</div>
<div class="legend">
  <span><span class="dot" style="background:#198754"></span>強勢多頭 (≥{CONFIG["status_thresholds"]["strong_bull"]})</span>
  <span><span class="dot" style="background:#0d6efd"></span>震盪偏多 ({CONFIG["status_thresholds"]["bull_bias"]}~{CONFIG["status_thresholds"]["strong_bull"]-1})</span>
  <span><span class="dot" style="background:#ffc107"></span>弱勢盤整 ({CONFIG["status_thresholds"]["weak_consolidation"]}~{CONFIG["status_thresholds"]["bull_bias"]-1})</span>
  <span><span class="dot" style="background:#dc3545"></span>空頭啟動 (&lt;{CONFIG["status_thresholds"]["weak_consolidation"]})</span>
  <span>🟡 RSI&gt;{CONFIG["indicators"]["rsi_overbought"]} 注意過熱 &nbsp; 🔵 RSI&lt;{CONFIG["indicators"]["rsi_oversold"]} 超賣zone</span>
</div>
<p style="margin-top:16px;color:#4b5563;font-size:.78rem;">⚠️ 本報告為量化模型輸出，僅供 AI 輔助分析參考，不構成投資建議。</p>

<script>
// ── 歷史資料全域快取 ────────────────────────────────────────────
const chartData = {{}};
const rendered  = new Set();
const chartsMap = {{}};
{charts_html}

// ── 切換走勢圖顯示 ──────────────────────────────────────────────
function toggleChart(sid) {{
  // 防抖動，避免觸控裝置同時觸發 ontouchend 與 onclick
  if (window['_toggling_' + sid]) return;
  window['_toggling_' + sid] = true;
  setTimeout(() => window['_toggling_' + sid] = false, 300);

  const row = document.getElementById('chart-row-' + sid);
  if (!row) return;
  const visible = row.style.display !== 'none';
  row.style.display = visible ? 'none' : 'table-row';
  if (!visible) {{
    if (!rendered.has(sid)) {{
      rendered.add(sid);
      // It's critical to wait for the browser to render the table row before initializing the chart
      // otherwise container.clientWidth will be 0
      setTimeout(() => {{
        renderChart(sid);
      }}, 50);
    }} else if (chartsMap[sid]) {{
      // Force resize check if container has changed dimensions
      const container = document.getElementById('chart-' + sid);
      if (container) {{
         chartsMap[sid].applyOptions({{ width: container.clientWidth }});
      }}
      setTimeout(() => chartsMap[sid].timeScale().fitContent(), 10);
    }}
  }}
}}

// ── 繪製 TradingView Lightweight Charts 走勢圖 ──────────────────────────────────────────
function renderChart(sid) {{
  const data = chartData[sid] || [];
  if (!data.length) return;

  const container = document.getElementById('chart-' + sid);
  container.innerHTML = ''; // 清空
  let initWidth = container.clientWidth;
  if(initWidth === 0) initWidth = container.parentElement.clientWidth;

  // 1. 初始化圖表
  const chart = LightweightCharts.createChart(container, {{
    width: initWidth,
    height: 480,
    layout: {{
      background: {{ type: 'solid', color: '#131722' }},
      textColor: '#9ca3af',
    }},
    grid: {{
      vertLines: {{ color: '#1e2130' }},
      horzLines: {{ color: '#1e2130' }},
    }},
    crosshair: {{
      mode: LightweightCharts.CrosshairMode.Normal,
    }},
    rightPriceScale: {{
      borderColor: '#2d3250',
    }},
    timeScale: {{
      borderColor: '#2d3250',
      timeVisible: true,
    }},
  }});
  
  chartsMap[sid] = chart;

  // 2. 加入 K線
  const mainSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {{
    upColor: '#ef5350',
    downColor: '#26a69a',
    borderVisible: false,
    wickUpColor: '#ef5350',
    wickDownColor: '#26a69a',
  }});

  // TradingView 需要的資料結構: time, open, high, low, close
  const candleData = data.map(d => ({{
    time: d.date,
    open: d.open,
    high: d.high,
    low: d.low,
    close: d.close
  }}));
  mainSeries.setData(candleData);

  // 3. 加入成交量 (Histogram Overlay)
  const volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {{
    color: '#26a69a',
    priceFormat: {{ type: 'volume' }},
    priceScaleId: '', // 空字串代表不與主圖標尺共用
    scaleMargins: {{
      top: 0.8,
      bottom: 0,
    }},
  }});
  const volumeData = data.map(d => ({{
    time: d.date,
    value: d.volume,
    color: d.close >= d.open ? 'rgba(239,83,80,0.4)' : 'rgba(38,166,154,0.4)'
  }}));
  volumeSeries.setData(volumeData);

  // 4. 加入均線輔助函數
  const addLine = (key, color, lineWidth, lineStyle, title) => {{
    const series = chart.addSeries(LightweightCharts.LineSeries, {{
      color,
      lineWidth,
      lineStyle,
      title,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false
    }});
    const lineData = data.filter(d => d[key] !== null && d[key] !== undefined).map(d => ({{
      time: d.date,
      value: d[key]
    }}));
    if (lineData.length > 0) series.setData(lineData);
  }};

  addLine('ma5', '#f59e0b', 1, LightweightCharts.LineStyle.Solid, 'MA5');
  addLine('ma10', '#fb923c', 1, LightweightCharts.LineStyle.Solid, 'MA10');
  addLine('ma20', '#34d399', 1.5, LightweightCharts.LineStyle.Solid, 'MA20');
  addLine('ma60', '#60a5fa', 1.5, LightweightCharts.LineStyle.Solid, 'MA60');
  addLine('bb_upper', '#a78bfa', 1, LightweightCharts.LineStyle.Dotted, 'BB Upper');
  addLine('bb_lower', '#a78bfa', 1, LightweightCharts.LineStyle.Dotted, 'BB Lower');

  // 5. 製作浮動提示框 (Legend / Tooltip)
  const legend = document.createElement('div');
  legend.style.position = 'absolute';
  legend.style.left = '12px';
  legend.style.top = '12px';
  legend.style.zIndex = 1;
  legend.style.fontSize = '12px';
  legend.style.fontFamily = 'monospace';
  legend.style.lineHeight = '1.5';
  legend.style.color = '#e4e6ea';
  legend.style.pointerEvents = 'none';
  container.style.position = 'relative';
  container.appendChild(legend);

  // 當滑鼠移動時更新提示框內的數據
  chart.subscribeCrosshairMove((param) => {{
    if (!param.time || param.point.x < 0 || param.point.x > container.clientWidth || param.point.y < 0 || param.point.y > container.clientHeight) {{
      legend.innerHTML = '';
      return;
    }}
    
    // 從回傳的 Map 中找到 K 線對應的數據
    const candleInfo = param.seriesData.get(mainSeries);
    if (candleInfo) {{
      let htmlStr = `<div style="font-size: 14px; font-weight: bold; margin-bottom: 4px; color: #fff;">${{sid}} - ${{param.time}}</div>`;
      htmlStr += `O: <span>${{candleInfo.open}}</span>  `;
      htmlStr += `H: <span>${{candleInfo.high}}</span>  `;
      htmlStr += `L: <span>${{candleInfo.low}}</span>  `;
      htmlStr += `C: <span>${{candleInfo.close}}</span><br>`;

      // 可以利用原本的 full data 來顯示均線等，如果要在 seriesData 收尋也可以
      // 這裡直接取 index 去 data 裡找，對齊更簡單
      const index = data.findIndex(d => d.date === param.time);
      if (index !== -1) {{
        const fullNode = data[index];
        htmlStr += `<span style="color:#f59e0b">MA5: ${{fullNode.ma5 || '-'}}</span> | `;
        htmlStr += `<span style="color:#fb923c">MA10: ${{fullNode.ma10 || '-'}}</span> | `;
        htmlStr += `<span style="color:#34d399">MA20: ${{fullNode.ma20 || '-'}}</span> | `;
        htmlStr += `<span style="color:#60a5fa">MA60: ${{fullNode.ma60 || '-'}}</span>`;
      }}
      legend.innerHTML = htmlStr;
    }}
  }});

  // 6. RWD 自適應大小
  new ResizeObserver(entries => {{
    if (entries.length === 0 || entries[0].target !== container) return;
    const newRect = entries[0].contentRect;
    if (newRect.width > 0) {{
      chart.applyOptions({{ width: newRect.width }});
    }}
  }}).observe(container);

  chart.timeScale().fitContent();
}}
</script>
</body>
</html>"""
    return html


# ============================================================
# 8. 歷史報價 CSV 文字檔產出
# ============================================================
def generate_group_quotes_text(results: list) -> str:
    """將族群內的所有個股 price_history 攤平並轉為 CSV 格式字串"""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        "StockID", "StockName", "Date", "Open", "High", "Low", 
        "Close", "Volume", "MA5", "MA10", "MA20", "MA60", 
        "BB_Upper", "BB_Lower"
    ])
    
    # Body
    for r in results:
        if r.get("error"):
            continue
        sid = r["stock_id"]
        sname = r["stock_name"]
        hist = r.get("price_history", [])
        
        for row in hist:
            writer.writerow([
                sid, sname, row.get("date", ""),
                row.get("open", ""), row.get("high", ""), row.get("low", ""),
                row.get("close", ""), row.get("volume", ""), row.get("ma5", ""),
                row.get("ma10", ""), row.get("ma20", ""), row.get("ma60", ""),
                row.get("bb_upper", ""), row.get("bb_lower", "")
            ])
            
    return output.getvalue()


# ============================================================
# 9. Telegram 傳送（sendDocument）
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

    for group_name, group_data in INDUSTRY_GROUPS.items():
        # ── 解構新格式（相容舊格式）──────────────────────────────
        if isinstance(group_data, dict) and "stocks" in group_data:
            stocks   = group_data["stocks"]
            overrides = group_data.get("config_overrides", {})
        else:
            # 向下相容：若仍為舊 {sid: sname} 格式
            stocks   = group_data
            overrides = {}

        # 產生此族群的合併 config（不影響全域 CONFIG）
        group_cfg = _merge_group_config(overrides)
        if overrides:
            print(f"   ⚙️  套用產業門檻覆蓋：{list(overrides.get('scoring', {}).keys())}")

        print(f"\n📊 開始分析族群：【{group_name}】（共 {len(stocks)} 支）")
        # ── 優化③：族群內個股並行分析 ───────────────────────────
        # 有 Token 用 3 執行緒；無 Token 降為 1（序列）避免限流
        workers = 3 if FINMIND_TOKEN else 1
        results_map: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            fut_to_sid = {
                pool.submit(analyze_single_stock, sid, sname, group_cfg): sid
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

        # ── 歷史報價 CSV TXT 報告 ───────────────────────────────
        quotes_txt_path = REPORT_DIR / f"{safe_name}_quotes_{TODAY_STR}.txt"
        quotes_txt_path.write_text(generate_group_quotes_text(results), encoding="utf-8-sig")
        print(f"   💾 Quotes 儲存：{quotes_txt_path}")
        _tg_send(quotes_txt_path, f"📈 【{group_name}】歷史報價完整數據 {TIMESTAMP_STR}")

    print(f"\n{'='*60}")
    print(f"  ✅ 所有族群分析完成！")
    print(f"  報告目錄：{REPORT_DIR.resolve()}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
