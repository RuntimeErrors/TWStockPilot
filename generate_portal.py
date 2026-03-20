import os
import glob
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
import requests
import json

# ==========================================
# 1. Configuration & Initialization
# ==========================================
TW_TZ = timezone(timedelta(hours=8))
now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M (UTC+8)")
report_dir = Path("reports")
report_dir.mkdir(exist_ok=True)

# ==========================================
# 2. Macro Data Fetching (FRED)
# ==========================================
def fetch_fred_data(series_id):
    """
    Fetches the latest value for a given FRED series ID.
    Includes a local cache mechanism for robustness.
    """
    cache_dir = Path("cache")
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / "macro_cache.json"
    
    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except:
            pass

    # Try fetching from FRED .txt link (lightweight)
    url = f"https://fred.stlouisfed.org/data/{series_id}.txt"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            content = res.text.strip()
            lines = content.split('\n')
            for line in reversed(lines):
                match = re.search(r'(\d{4}-\d{2}-\d{2})\s+([\d\.]+)', line)
                if match:
                    val = float(match.group(2))
                    cache[series_id] = {"value": val, "date": match.group(1), "updated": datetime.now().isoformat()}
                    cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
                    return val
        else:
            print(f"⚠️ FRED HTTP {res.status_code} for {series_id}")
    except Exception as e:
        print(f"⚠️ FRED fetch error for {series_id}: {e}")

    # Fallback to cache
    if series_id in cache:
        print(f"ℹ️ Using cached value for {series_id} (from {cache[series_id]['date']})")
        return cache[series_id]["value"]
    
    return None

# ==========================================
# 3. Market Data Fetching (via Yahoo Finance)
# ==========================================
def fetch_dashboard_data():
    categories = {
        "主要指數": [
            {"name": "台股加權", "id": "^TWII", "unit": "點"},
            {"name": "台股櫃買", "id": "^TWOII", "unit": "點"},
            {"name": "S&P 500", "id": "^GSPC", "unit": "點"},
            {"name": "那斯達克", "id": "^IXIC", "unit": "點"},
            {"name": "道瓊工業", "id": "^DJI", "unit": "點"},
            {"name": "費城半導體", "id": "^SOX", "unit": "點"},
        ],
        "貨幣與資產": [
            {"name": "美元兌台幣", "id": "TWD=X", "unit": "元"},
            {"name": "日圓兌台幣", "id": "JPYTWD=X", "unit": "元"},
            {"name": "比特幣", "id": "BTC-USD", "unit": "USD"},
            {"name": "以太坊", "id": "ETH-USD", "unit": "USD"},
        ],
        "關鍵商品": [
            {"name": "WTI 原油", "id": "CL=F", "unit": "USD/桶"},
            {"name": "布蘭特原油", "id": "BZ=F", "unit": "USD/桶"},
            {"name": "黃金", "id": "GC=F", "unit": "USD/盎司"},
            {"name": "白銀", "id": "SI=F", "unit": "USD/盎司"},
            {"name": "高階銅", "id": "HG=F", "unit": "USD/磅"},
        ],
        "利率與流動性": [
            {"name": "10Y. 美債", "id": "^TNX", "unit": "%"},
            {"name": "5Y. 美債", "id": "^FVX", "unit": "%"},
        ],
        "風險與情緒指標": [
            {"name": "VIX 恐慌指數", "id": "^VIX", "unit": "點"},
            {"name": "美元指數", "id": "DX-Y.NYB", "unit": "點"},
        ]
    }
    
    # Pre-fetch Macro data from FRED
    macro_data = {
        "inflation_10y": fetch_fred_data("T10YIE"),
        "spread_10y2y": fetch_fred_data("T10Y2Y"),
        "high_yield_spread": fetch_fred_data("BAMLH0A0HYM2"),
        "fed_funds": fetch_fred_data("FEDFUNDS"),
        "m2": fetch_fred_data("WM2NS"),
        "sentiment": fetch_fred_data("UMCSENT")
    }
    
    dashboard_results = {}
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    for cat_name, symbols in categories.items():
        results = []
        for idx in symbols:
            try:
                url = f"https://query2.finance.yahoo.com/v8/finance/chart/{idx['id']}"
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    if 'error' in data['chart'] and data['chart']['error']:
                        print(f"⚠️ API Error for {idx['name']}: {data['chart']['error']}")
                        continue
                        
                    meta = data['chart']['result'][0]['meta']
                    price = float(meta['regularMarketPrice'])
                    prev_close = float(meta['chartPreviousClose'])
                    
                    change = price - prev_close
                    change_pct = (change / prev_close) * 100
                    
                    results.append({
                        "name": idx["name"],
                        "price": price,
                        "change": change,
                        "change_pct": change_pct,
                        "unit": idx["unit"]
                    })
                else:
                    print(f"⚠️ Failed to fetch {idx['name']}: HTTP {res.status_code}")
            except Exception as e:
                print(f"⚠️ Exception fetching {idx['name']}: {e}")
        dashboard_results[cat_name] = results
        
        # Inject calculated and fetched macro data into specific categories
        if cat_name == "利率與流動性":
            # 1. Yield Spread (10Y-2Y)
            if macro_data["spread_10y2y"] is not None:
                results.append({"name": "10Y-2Y 利差", "price": macro_data["spread_10y2y"], "change": 0, "change_pct": 0, "unit": "%"})
            
            # 2. Real Interest Rate
            nominal_10y = next((x for x in results if x["name"] == "10Y. 美債"), None)
            if nominal_10y and macro_data["inflation_10y"] is not None:
                real_rate = nominal_10y["price"] - macro_data["inflation_10y"]
                results.append({"name": "10Y. 實質利率", "price": real_rate, "change": 0, "change_pct": 0, "unit": "%"})
                results.append({"name": "10Y. 預期通膨", "price": macro_data["inflation_10y"], "change": 0, "change_pct": 0, "unit": "%"})
            
            # 3. Fed Funds & M2
            if macro_data["fed_funds"] is not None:
                results.append({"name": "聯邦基金利率", "price": macro_data["fed_funds"], "change": 0, "change_pct": 0, "unit": "%"})
            if macro_data["m2"] is not None:
                results.append({"name": "M2 貨幣供給", "price": macro_data["m2"], "change": 0, "change_pct": 0, "unit": "B$"})

        elif cat_name == "風險與情緒指標":
            # 1. High Yield Spread
            if macro_data["high_yield_spread"] is not None:
                results.append({"name": "高收益債利差", "price": macro_data["high_yield_spread"], "change": 0, "change_pct": 0, "unit": "%"})
            
            # 2. Consumer Sentiment
            if macro_data["sentiment"] is not None:
                results.append({"name": "消費者信心", "price": macro_data["sentiment"], "change": 0, "change_pct": 0, "unit": "點"})
    
    return dashboard_results

# ==========================================
# 3. HTML Generation
# ==========================================
def generate_html():
    dashboard_data = fetch_dashboard_data()
    
    # Generate dashboard sections HTML
    dashboard_html = ""
    for cat_name, data in dashboard_data.items():
        if not data: continue
        
        cards = ""
        for idx in data:
            color = "#3fb950" if idx["change"] >= 0 else "#f85149"
            sign = "+" if idx["change"] >= 0 else ""
            cards += f"""
            <div class="index-card">
                <div class="index-name">{idx['name']}</div>
                <div class="index-price">{idx['price']:,.2f} <span class="unit">{idx['unit']}</span></div>
                <div class="index-change" style="color: {color}">
                    {sign}{idx['change']:,.2f} ({sign}{idx['change_pct']:.2f}%)
                </div>
            </div>"""
        
        dashboard_html += f"""
        <div class="section-header">
            <span class="section-title">{cat_name}</span>
        </div>
        <div class="index-grid">
            {cards}
        </div>"""

    # Parse industry reports
    html_files = sorted(report_dir.glob("*.html"), key=lambda p: p.name)
    items = []
    for path in html_files:
        if path.name == "index.html": continue
        m = re.match(r"(.+)_(\d{8})\.html", path.name)
        if m:
            raw_name = m.group(1)
            date_str = m.group(2)
            display = raw_name.replace("_", " ")
            items.append({"display": display, "file": path.name, "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" })

    items.sort(key=lambda x: x["date"], reverse=True)
    report_cards = ""
    for it in items:
        report_cards += f"""
        <a href="{it['file']}" class="card">
            <div class="card-icon">📊</div>
            <div class="card-title">{it['display']}</div>
            <div class="card-date">{it['date']}</div>
        </a>"""

    no_reports = "<p class='no-report'>尚未產生任何族群報告。</p>" if not report_cards else ""

    html_template = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TWStockPilot — 全球局勢儀表板</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', -apple-system, system-ui, sans-serif;
            background: #0d1117;
            color: #e6edf3;
            min-height: 100vh;
            line-height: 1.6;
        }}
        header {{
            background: linear-gradient(135deg, #1a2236 0%, #0d1117 100%);
            border-bottom: 1px solid #21262d;
            padding: 3rem 1.5rem 2.5rem;
            text-align: center;
        }}
        .logo {{ font-size: 2.8rem; font-weight: 700; letter-spacing: -1.5px; margin-bottom: 0.5rem; }}
        .logo span {{ color: #60a5fa; }}
        .subtitle {{ color: #8b949e; font-size: 1.1rem; }}
        .updated {{
            display: inline-block;
            margin-top: 1.5rem;
            background: rgba(48, 54, 61, 0.4);
            border: 1px solid #30363d;
            border-radius: 20px;
            padding: 0.4rem 1.2rem;
            font-size: 0.85rem;
            color: #8b949e;
        }}
        .updated span {{ color: #3fb950; font-weight: 600; }}
        
        main {{ max-width: 1240px; margin: 0 auto; padding: 3rem 1.5rem; }}
        
        .section-header {{
            display: flex;
            align-items: center;
            margin: 2rem 0 1.2rem;
            padding-bottom: 0.8rem;
            border-bottom: 1px solid #21262d;
        }}
        .section-header:first-child {{ margin-top: 0; }}
        .section-title {{
            font-size: 1.1rem;
            font-weight: 700;
            color: #f0f6fc;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }}
        
        .index-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 1.2rem;
        }}
        .index-card {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 1.5rem;
            text-align: center;
            transition: all 0.2s;
        }}
        .index-card:hover {{ transform: translateY(-3px); border-color: #444c56; background: #1c2128; }}
        .index-name {{ font-size: 0.85rem; color: #8b949e; font-weight: 600; margin-bottom: 0.5rem; }}
        .index-price {{ font-size: 1.5rem; font-weight: 700; color: #f0f6fc; margin-bottom: 0.3rem; }}
        .index-price .unit {{ font-size: 0.85rem; font-weight: 400; color: #8b949e; margin-left: 4px; }}
        .index-change {{ font-size: 0.9rem; font-weight: 600; }}

        .report-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 1.2rem;
        }}
        .card {{
            display: flex;
            flex-direction: column;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 1.8rem;
            text-decoration: none;
            color: inherit;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        }}
        .card:hover {{
            border-color: #60a5fa;
            transform: translateY(-4px);
            background: #1c2128;
            box-shadow: 0 12px 24px rgba(0,0,0,0.3);
        }}
        .card-icon {{ font-size: 2.2rem; margin-bottom: 1rem; }}
        .card-title {{ font-size: 1.3rem; font-weight: 700; color: #f0f6fc; margin-bottom: 0.6rem; }}
        .card-date {{ font-size: 0.9rem; color: #8b949e; }}
        
        .no-report {{ color: #8b949e; text-align: center; padding: 4rem; font-style: italic; }}
        
        footer {{
            text-align: center;
            padding: 4rem 1.5rem;
            color: #484f58;
            font-size: 0.9rem;
            border-top: 1px solid #21262d;
            margin-top: 4rem;
        }}
        
        @media (max-width: 768px) {{
            .logo {{ font-size: 2.2rem; }}
            header {{ padding: 2.5rem 1.2rem; }}
            .index-grid {{ grid-template-columns: 1fr 1fr; gap: 0.8rem; }}
            .index-price {{ font-size: 1.2rem; }}
            .card {{ padding: 1.4rem; }}
        }}
    </style>
</head>
<body>
    <header>
        <div class="logo">TW<span>Stock</span>Pilot</div>
        <p class="subtitle">全球局勢儀表板 · 宏觀經濟與族群量化分析</p>
        <div class="updated">最後更新：<span>{now_str}</span></div>
    </header>
    <main>
        {dashboard_html}

        <div class="section-header">
            <span class="section-title">族群分析報告</span>
        </div>
        <div class="report-grid">
            {report_cards}
        </div>
        {no_reports}
    </main>
    <footer>
        ⚠️ 本報告為量化模型輸出，僅供 AI 輔助分析參考，不構成投資建議。<br>
        Data provided by Yahoo Finance & FinMind API
    </footer>
</body>
</html>"""
    
    (report_dir / "index.html").write_text(html_template, encoding="utf-8")
    print(f"✅ index.html generated successfully with dashboard and {len(items)} reports.")

if __name__ == "__main__":
    generate_html()
