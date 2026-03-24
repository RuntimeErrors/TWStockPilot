"""
TwinStock 本地端儀表板伺服器
=============================
使用方式：
  python server.py          # 預設 port 8080
  PORT=9000 python server.py  # 指定 port

功能：
  - 靜態服務 reports/ 目錄（儀表板 + 族群報告）
  - GET /api/refresh  → 重新執行 generate_portal.py，更新儀表板資料
  - GET /api/status   → 回傳伺服器狀態 JSON
"""

import os
import sys
import json
import subprocess
import threading
import webbrowser
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse


# ── 設定 ────────────────────────────────────────────────────────────
PORT        = int(os.environ.get("PORT", 8080))
REPORTS_DIR = Path(__file__).parent / "reports"
PROJECT_DIR = Path(__file__).parent

# 確保 reports/ 存在
REPORTS_DIR.mkdir(exist_ok=True)

# ── 刷新狀態旗標（避免重複觸發）────────────────────────────────────
_refresh_lock    = threading.Lock()
_is_refreshing   = False


def run_generate_portal() -> tuple[bool, str]:
    """執行 generate_portal.py，回傳 (成功, 訊息)。"""
    global _is_refreshing
    with _refresh_lock:
        if _is_refreshing:
            return False, "資料更新中，請稍後再試"
        _is_refreshing = True

    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_DIR / "generate_portal.py")],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_DIR),
            timeout=120,
        )
        if result.returncode == 0:
            return True, "儀表板資料已更新"
        else:
            return False, result.stderr or "執行失敗"
    except subprocess.TimeoutExpired:
        return False, "執行逾時（120s）"
    except Exception as e:
        return False, str(e)
    finally:
        with _refresh_lock:
            _is_refreshing = False

def run_industry_analyzer() -> tuple[bool, str]:
    """執行 industry_analyzer.py，回傳 (成功, 訊息)。"""
    global _is_refreshing
    with _refresh_lock:
        if _is_refreshing:
            return False, "伺服器忙碌中（更新儀表板或分析中），請稍後再試"
        _is_refreshing = True

    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_DIR / "industry_analyzer.py")],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_DIR),
            timeout=600, # 族群分析需要較長的時間
        )
        if result.returncode == 0:
            # 分析完畢後，也自動更新一次儀表板（產生最新的 html）
            subprocess.run(
                [sys.executable, str(PROJECT_DIR / "generate_portal.py")],
                capture_output=True,
                cwd=str(PROJECT_DIR),
                timeout=120,
            )
            return True, "族群分析已完成"
        else:
            return False, result.stderr or "執行族群分析失敗"
    except subprocess.TimeoutExpired:
        return False, "執行逾時（600s）"
    except Exception as e:
        return False, str(e)
    finally:
        with _refresh_lock:
            _is_refreshing = False


# ── 自訂 HTTP Handler ─────────────────────────────────────────────
class DashboardHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        # 靜態根目錄指向 reports/
        super().__init__(*args, directory=str(REPORTS_DIR), **kwargs)

    def log_message(self, format, *args):
        # 過濾靜態資源的雜訊 log
        if any(ext in args[0] for ext in [".js", ".css", ".png", ".ico"]):
            return
        super().log_message(format, *args)

    def do_GET(self):
        parsed = urlparse(self.path)

        # ── /api/refresh ────────────────────────────────────────────
        if parsed.path == "/api/refresh":
            self._send_json_async(run_generate_portal)
            return

        # ── /api/analyze ────────────────────────────────────────────
        if parsed.path == "/api/analyze":
            self._send_json_async(run_industry_analyzer)
            return

        # ── /api/status ─────────────────────────────────────────────
        if parsed.path == "/api/status":
            self._send_json(200, {
                "status": "running",
                "port": PORT,
                "refreshing": _is_refreshing,
                "reports_dir": str(REPORTS_DIR),
            })
            return

        # ── 靜態檔案（reports/ 目錄）────────────────────────────────
        # 根路徑 / → 重定向到 index.html
        if parsed.path == "/":
            self.path = "/index.html"

        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            # 忽略客戶端中途斷開連線產生的錯誤
            pass

    def _send_json_async(self, run_func):
        """在背景執行指定函數，完成後讓前端 reload。"""
        global _is_refreshing

        if _is_refreshing:
            self._send_json(409, {"status": "busy", "message": "伺服器忙碌中，請稍後再試"})
            return

        # 同步執行（等待完成後回傳結果，讓前端能順序 reload）
        success, message = run_func()
        code = 200 if success else 500
        self._send_json(code, {"status": "ok" if success else "error", "message": message})

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


# ── 啟動伺服器 ───────────────────────────────────────────────────────
def main():
    # 首次啟動時先更新儀表板資料
    print("🔄 啟動中：正在更新儀表板資料...")
    success, msg = run_generate_portal()
    if success:
        print(f"✅ {msg}")
    else:
        print(f"⚠️  資料更新失敗：{msg}（仍可瀏覽既有報告）")

    # 啟動 HTTP 伺服器，若 port 被佔用則自動遞增
    current_port = PORT
    server = None
    while current_port < PORT + 10:
        try:
            server = HTTPServer(("", current_port), DashboardHandler)
            break
        except OSError as e:
            print(f"⚠️  Port {current_port} 已被佔用，嘗試 {current_port + 1}...")
            current_port += 1
            
    if not server:
        print("❌ 無法找到可用的 Port，啟動失敗。")
        sys.exit(1)

    url = f"http://localhost:{current_port}"
    print(f"\n🌐 儀表板伺服器已啟動：{url}")
    print("   按 Ctrl+C 停止伺服器\n")

    # 自動開啟瀏覽器（延遲 0.8 秒讓伺服器就緒）
    def _open_browser():
        import time
        time.sleep(0.8)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n🛑 伺服器已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
