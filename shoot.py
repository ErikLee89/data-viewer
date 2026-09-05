# -*- coding: utf-8 -*-
"""CDP 截图工具：等待页面完全加载（含 async fetch）后截图"""
import subprocess, os, time, json, base64, sys, urllib.request
import websocket

CHROME = r"D:\Program Files\WPS Comate\scripts\apps\basic\tools\chromium\versions\1.61.0\chromium-1228\chrome-win64\chrome.exe"
PORT = 19222
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.mid = 0

    def send(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                return msg.get("result", {})

    def eval(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


def shoot(url, out_png, wait_js="true", timeout=60):
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                             f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
                             "--window-size=1600,1400", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        # 新建标签页（PUT 方式，新版 chromium 要求）
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
        tab = json.loads(urllib.request.urlopen(req).read())
        ws_url = tab["webSocketDebuggerUrl"]
        cdp = CDP(ws_url)
        t0 = time.time()
        # 等待 wait_js 为 true
        while time.time() - t0 < timeout:
            try:
                v = cdp.eval(wait_js)
                if v:
                    break
            except Exception:
                pass
            time.sleep(1)
        time.sleep(2)  # 渲染余量
        r = cdp.send("Page.captureScreenshot", format="png")
        with open(out_png, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print("saved", out_png, os.path.getsize(out_png) // 1024, "KB")
        cdp.ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    os.makedirs(OUTDIR, exist_ok=True)
    pages = sys.argv[1:] or ["overview"]
    for p in pages:
        wait = "document.querySelectorAll('#kpi-grid .kpi').length > 0"
        if p == "cam":
            wait = "document.querySelectorAll('#tbl-pairs tbody tr').length > 0"
        elif p == "wind":
            wait = "document.querySelectorAll('#ch-rose canvas').length > 0"
        elif p == "rotor":
            wait = "document.querySelectorAll('#rotor-kpi .kpi').length > 0"
        elif p == "quality":
            wait = "document.querySelectorAll('#tbl-stats tbody tr').length > 0"
        elif p == "track":
            wait = "document.querySelectorAll('#ch-track canvas').length > 0"
        elif p == "timeseries":
            wait = "document.querySelectorAll('#ch-ts canvas').length > 0"
        shoot(f"http://127.0.0.1:8300/?page={p}", os.path.join(OUTDIR, f"{p}.png"), wait)
