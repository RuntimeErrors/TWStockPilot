import os
import glob
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
import requests
import json
import concurrent.futures
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

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

    # 1. Try FRED API if API key is provided
    api_key = os.environ.get("FRED_API_KEY")
    if api_key:
        api_url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={api_key}&file_type=json&sort_order=desc&limit=1"
        try:
            res = requests.get(api_url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                observations = data.get("observations", [])
                if observations:
                    obs = observations[0]
                    val_str = obs.get("value", "")
                    if val_str != "." and val_str != "":
                        val = float(val_str)
                        cache[series_id] = {"value": val, "date": obs.get("date"), "updated": datetime.now().isoformat()}
                        cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
                        return val
        except Exception as e:
            print(f"⚠️ FRED API error for {series_id}: {e}")

    # 2. Try fetching from FRED .txt link (lightweight scraping)
    url = f"https://fred.stlouisfed.org/data/{series_id}.txt"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    try:
        res = requests.get(url, headers=headers, timeout=15)
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

    # 3. Fallback to cache
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
    fred_series = {
        "inflation_10y": "T10YIE",
        "spread_10y2y": "T10Y2Y",
        "high_yield_spread": "BAMLH0A0HYM2",
        "fed_funds": "FEDFUNDS",
        "m2": "WM2NS",
        "sentiment": "UMCSENT"
    }

    macro_data = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        future_to_fred = {executor.submit(fetch_fred_data, sid): key for key, sid in fred_series.items()}
        for future in concurrent.futures.as_completed(future_to_fred):
            key = future_to_fred[future]
            try:
                macro_data[key] = future.result()
            except Exception as e:
                print(f"⚠️ FRED Exception for {key}: {e}")
                macro_data[key] = None
    
    dashboard_results = {cat: [] for cat in categories.keys()}
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    def fetch_yahoo(idx):
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{idx['id']}"
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if 'error' in data['chart'] and data['chart']['error']:
                    print(f"⚠️ API Error for {idx['name']}: {data['chart']['error']}")
                    return None
                    
                meta = data['chart']['result'][0]['meta']
                price = float(meta['regularMarketPrice'])
                prev_close = float(meta['chartPreviousClose'])
                
                change = price - prev_close
                change_pct = (change / prev_close) * 100 if prev_close != 0 else 0
                
                return {
                    "name": idx["name"],
                    "price": price,
                    "change": change,
                    "change_pct": change_pct,
                    "unit": idx["unit"]
                }
            else:
                print(f"⚠️ Failed to fetch {idx['name']}: HTTP {res.status_code}")
        except Exception as e:
            print(f"⚠️ Exception fetching {idx['name']}: {e}")
        return None

    def fetch_taifex_night():
        url = "https://mis.taifex.com.tw/futures/api/getQuoteList"
        payload = {"MarketType":"0", "SymbolType":"F", "KindID":"1", "CID":"TXF"}
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                items = data.get("RtData", {}).get("QuoteList", [])
                for item in items:
                    # Skip spot index
                    if item.get("SymbolID", "") == "TXF-S" or item.get("SymbolID", "") == "TXF-P":
                        continue
                    price_str = item.get("CLastPrice", "").strip()
                    ref_str = item.get("CRefPrice", "").strip()
                    if price_str and ref_str:
                        price = float(price_str)
                        prev_close = float(ref_str)
                        change = price - prev_close
                        change_pct = (change / prev_close) * 100 if prev_close != 0 else 0
                        return {
                            "name": "台指期夜盤",
                            "price": price,
                            "change": change,
                            "change_pct": change_pct,
                            "unit": "點"
                        }
        except Exception as e:
            print(f"⚠️ Exception fetching TAIFEX night futures: {e}")
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        future_taifex = executor.submit(fetch_taifex_night)
        future_to_idx = {}
        for cat_name, symbols in categories.items():
            for idx in symbols:
                future = executor.submit(fetch_yahoo, idx)
                future_to_idx[future] = (cat_name, idx)
                
        for future in concurrent.futures.as_completed(future_to_idx):
            cat_name, idx = future_to_idx[future]
            try:
                result = future.result()
                if result:
                    dashboard_results[cat_name].append(result)
            except Exception as e:
                print(f"⚠️ Exception processing {idx['name']}: {e}")

    taifex_result = None
    try:
        taifex_result = future_taifex.result()
    except Exception as e:
        print(f"⚠️ Exception retrieving TAIFEX result: {e}")

    # Reorder results to match original category lists
    for cat_name, symbols in categories.items():
        ordered_results = []
        for idx in symbols:
            found = next((r for r in dashboard_results[cat_name] if r["name"] == idx["name"]), None)
            if found:
                ordered_results.append(found)
            # Insert Taiwan Night Market Futures right after "台股加權"
            if cat_name == "主要指數" and idx["name"] == "台股加權" and taifex_result:
                ordered_results.append(taifex_result)
        dashboard_results[cat_name] = ordered_results

    # Inject calculated and fetched macro data into specific categories
    for cat_name in dashboard_results:
        results = dashboard_results[cat_name]
        if cat_name == "利率與流動性":
            # 1. Yield Spread (10Y-2Y)
            if macro_data.get("spread_10y2y") is not None:
                results.append({"name": "10Y-2Y 利差", "price": macro_data["spread_10y2y"], "change": 0, "change_pct": 0, "unit": "%"})
            
            # 2. Real Interest Rate
            nominal_10y = next((x for x in results if x["name"] == "10Y. 美債"), None)
            if nominal_10y and macro_data.get("inflation_10y") is not None:
                real_rate = nominal_10y["price"] - macro_data["inflation_10y"]
                results.append({"name": "10Y. 實質利率", "price": real_rate, "change": 0, "change_pct": 0, "unit": "%"})
                results.append({"name": "10Y. 預期通膨", "price": macro_data["inflation_10y"], "change": 0, "change_pct": 0, "unit": "%"})
            
            # 3. Fed Funds & M2
            if macro_data.get("fed_funds") is not None:
                results.append({"name": "聯邦基金利率", "price": macro_data["fed_funds"], "change": 0, "change_pct": 0, "unit": "%"})
            if macro_data.get("m2") is not None:
                results.append({"name": "M2 貨幣供給", "price": macro_data["m2"], "change": 0, "change_pct": 0, "unit": "B$"})

        elif cat_name == "風險與情緒指標":
            # 1. High Yield Spread
            if macro_data.get("high_yield_spread") is not None:
                results.append({"name": "高收益債利差", "price": macro_data["high_yield_spread"], "change": 0, "change_pct": 0, "unit": "%"})
            
            # 2. Consumer Sentiment
            if macro_data.get("sentiment") is not None:
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
        
        .refresh-btn {{
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            margin-top: 1.2rem;
            margin-left: 0.8rem;
            background: rgba(96, 165, 250, 0.12);
            border: 1px solid rgba(96, 165, 250, 0.35);
            border-radius: 20px;
            padding: 0.4rem 1.2rem;
            font-size: 0.85rem;
            color: #60a5fa;
            cursor: pointer;
            transition: all 0.2s;
            font-family: inherit;
        }}
        .refresh-btn:hover {{ background: rgba(96, 165, 250, 0.22); border-color: #60a5fa; }}
        .refresh-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        
        .analyze-btn {{
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            margin-top: 1.2rem;
            margin-left: 0.5rem;
            background: rgba(245, 158, 11, 0.12);
            border: 1px solid rgba(245, 158, 11, 0.35);
            border-radius: 20px;
            padding: 0.4rem 1.2rem;
            font-size: 0.85rem;
            color: #fbd38d;
            cursor: pointer;
            transition: all 0.2s;
            font-family: inherit;
        }}
        .analyze-btn:hover {{ background: rgba(245, 158, 11, 0.22); border-color: #fbd38d; }}
        .analyze-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        
        /* 全螢幕 loading 遮罩 */
        #loading-overlay {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(13, 17, 23, 0.82);
            backdrop-filter: blur(4px);
            z-index: 999;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 1rem;
            color: #e6edf3;
            font-size: 1.1rem;
        }}
        #loading-overlay.show {{ display: flex; }}
        .spinner {{
            width: 40px; height: 40px;
            border: 3px solid #30363d;
            border-top-color: #60a5fa;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        
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
    <!-- 全螢幕 Loading 遮罩 -->
    <div id="loading-overlay">
        <div class="spinner"></div>
        <span>資料更新中，請稍候...</span>
    </div>

    <header>
        <div class="logo">TW<span>Stock</span>Pilot</div>
        <p class="subtitle">全球局勢儀表板 · 宏觀經濟與族群量化分析</p>
        <div>
            <div class="updated">最後更新：<span>{now_str}</span></div>
            <div id="refresh-actions" style="display: none; margin-top: 1rem;">
                <button class="refresh-btn" id="refreshBtn" onclick="refreshData()">🔄 更新儀表板</button>
                <button class="analyze-btn" id="analyzeBtn" onclick="analyzeIndustry()">📊 更新族群分析</button>
            </div>
        </div>
    </header>
    
    <script>
    async function refreshData() {{
        const btn = document.getElementById('refreshBtn');
        const overlay = document.getElementById('loading-overlay');
        const overlayText = overlay.querySelector('span');
        
        btn.disabled = true;
        btn.textContent = '⏳ 更新中...';
        overlayText.textContent = '資料更新中，請稍候...';
        overlay.classList.add('show');
        try {{
            const res = await fetch('/api/refresh');
            const data = await res.json();
            if (res.ok) {{
                location.reload();
            }} else {{
                alert('更新失敗：' + (data.message || '未知錯誤'));
                btn.disabled = false;
                btn.textContent = '🔄 更新儀表板';
                overlay.classList.remove('show');
            }}
        }} catch (e) {{
            alert('無法連線到本地伺服器，請確認伺服器已啟動。');
            btn.disabled = false;
            btn.textContent = '🔄 更新儀表板';
            overlay.classList.remove('show');
        }}
    }}
    
    async function analyzeIndustry() {{
        const btn = document.getElementById('analyzeBtn');
        const overlay = document.getElementById('loading-overlay');
        const overlayText = overlay.querySelector('span');
        
        btn.disabled = true;
        btn.textContent = '⏳ 分析中...';
        overlayText.textContent = '族群分析進行中（這可能需要數分鐘），請稍候...';
        overlay.classList.add('show');
        try {{
            const res = await fetch('/api/analyze');
            const data = await res.json();
            if (res.ok) {{
                location.reload();
            }} else {{
                alert('分析失敗：' + (data.message || '未知錯誤'));
                btn.disabled = false;
                btn.textContent = '📊 更新族群分析';
                overlay.classList.remove('show');
            }}
        }} catch (e) {{
            alert('無法連線到本地伺服器，請確認伺服器已啟動。');
            btn.disabled = false;
            btn.textContent = '📊 更新族群分析';
            overlay.classList.remove('show');
        }}
    }}
    
    // 只有在本地伺服器環境下，才顯示更新按鈕並啟用自動刷新
    if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {{
        document.getElementById('refresh-actions').style.display = 'block';
        
        // 背景每 5 分鐘自動刷新儀表板（不顯示全螢幕遮罩）
        setInterval(async () => {{
            try {{
                const res = await fetch('/api/refresh');
                if (res.ok) {{
                    location.reload();
                }}
            }} catch (e) {{
                console.log('Auto refresh failed');
            }}
        }}, 5 * 60 * 1000); // 5 minutes
    }}
    </script>
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
