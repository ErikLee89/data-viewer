"""Browser regression: python tests/test_history.py (requires Playwright Chromium)."""
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class HistoryDrawerTest(unittest.TestCase):
    def test_history_navigation(self):
        # Exercise the real backend with disposable files, leaving user data intact.
        with tempfile.TemporaryDirectory(prefix="data-viewer-history-") as directory:
            data_dir = Path(directory)
            originals = {name: getattr(server, name) for name in
                         ("DATA_DIR", "CACHE_DIR", "COLMETA_FILE", "SESSION_FILE", "UISTATE_DIR")}
            server.DATA_DIR = str(data_dir)
            server.CACHE_DIR = str(data_dir / "cache")
            server.COLMETA_FILE = str(data_dir / "colmeta.json")
            server.SESSION_FILE = str(data_dir / "session.json")
            server.UISTATE_DIR = str(data_dir / "uistate")
            Path(server.CACHE_DIR).mkdir()
            Path(server.UISTATE_DIR).mkdir()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            service = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error"))
            thread = threading.Thread(target=service.run, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not service.started and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(service.started)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 900})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        url = f"http://127.0.0.1:{port}"
                        page.goto(url, wait_until="networkidle")
                        trigger = page.locator("#history-trigger")
                        panel = page.locator("#history-panel")
                        expect(trigger).to_have_attribute("aria-expanded", "false")
                        expect(panel).to_be_hidden()
                        page.mouse.move(10, 450)
                        expect(panel).to_be_visible()
                        page.wait_for_function("Math.abs(document.getElementById('history-drawer').getBoundingClientRect().left) < .5")
                        page.mouse.move(100, 300)
                        expect(panel).to_be_visible()
                        expect(page.locator("#history-list")).to_contain_text("暂无历史数据文件")
                        page.mouse.move(700, 300)
                        expect(panel).to_be_hidden()

                        # Real CSV uploads populate history automatically.
                        for filename, values in [("历史甲.csv", "1,2,3"), ("历史乙.csv", "4,5,6")]:
                            rows = "time,speed\n" + "\n".join(
                                f"{1700000000 + i},{value}" for i, value in enumerate(values.split(",")))
                            with page.expect_file_chooser() as chooser:
                                page.locator("#btn-open").click()
                            chooser.value.set_files({"name": filename, "mimeType": "text/csv", "buffer": rows.encode()})
                            expect(page.locator("#file-title")).to_have_text(filename)
                            expect(page.locator("#loading")).to_be_hidden()
                        expect(page.locator("#history-count")).to_have_text("2 个文件")
                        page.locator("#cols input").check()
                        page.locator("#cols .name").dblclick()
                        page.locator("#cols .name input").fill("船速")
                        page.locator("#cols .name input").press("Enter")
                        page.evaluate("chart.dispatchAction({type: 'dataZoom', start: 20, end: 80})")
                        # Switch before the 600ms save timer: state must stay with B.
                        trigger.hover()
                        expect(panel).to_be_visible()
                        page.locator(".history-item", has_text="历史甲.csv").hover()
                        expect(trigger).to_have_attribute("aria-expanded", "true")
                        page.locator(".history-item", has_text="历史甲.csv").click()
                        expect(page.locator("#file-title")).to_have_text("历史甲.csv")
                        expect(page.locator("#loading")).to_be_hidden()
                        expect(panel).to_be_hidden()
                        self.assertEqual(page.evaluate("chart.getOption().dataZoom[1].start"), 0)
                        trigger.hover()
                        expect(page.locator(".history-item.active")).to_contain_text("历史甲.csv")
                        page.locator(".history-item", has_text="历史乙.csv").click()
                        expect(page.locator("#file-title")).to_have_text("历史乙.csv")
                        expect(page.locator("#cols input")).to_be_checked()
                        expect(page.locator("#cols .name")).to_have_text("船速")
                        self.assertEqual(page.evaluate("chart.getOption().dataZoom[1].start"), 20)
                        page.reload(wait_until="networkidle")
                        expect(page.locator("#file-title")).to_have_text("历史乙.csv")
                        expect(page.locator("#cols input")).to_be_checked()

                        # Keyboard access, Escape, and pointer leave after focus.
                        trigger.focus()
                        expect(panel).to_be_visible()
                        page.keyboard.press("Escape")
                        expect(panel).to_be_hidden()
                        page.locator("#btn-open").focus()
                        trigger.focus()
                        expect(panel).to_be_visible()
                        page.locator(".history-item.active").hover()
                        page.mouse.move(700, 300)
                        expect(panel).to_be_hidden()

                        # Failure and retry states.
                        page.route("**/api/cache", lambda route: route.fulfill(status=503, body="{}"))
                        page.evaluate("refreshHistory(true)")
                        trigger.hover()
                        expect(page.locator(".history-retry")).to_be_visible()
                        page.unroute("**/api/cache")
                        page.locator(".history-retry").click()
                        expect(page.locator("#history-count")).to_have_text("2 个文件")
                        self.assertEqual(page.request.post(url + "/api/session", data={"hash": "../invalid"}).status, 400)
                        self.assertEqual(page.request.post(url + "/api/session", data={"hash": "0" * 64}).status, 404)
                        items = page.request.get(url + "/api/cache").json()["items"]
                        current = next(item for item in items if item["file"] == "历史乙.csv")
                        Path(server.CACHE_DIR, current["hash"] + ".json").unlink()
                        # Failed selection leaves the displayed file and hash intact.
                        target = next(item for item in items if item["file"] == "历史甲.csv")
                        page.evaluate("item => openHistoryFile(item)", target)
                        expect(page.locator("#file-title")).to_have_text("历史甲.csv")
                        page.evaluate("item => openHistoryFile(item)", current)
                        expect(page.locator("#toast")).to_contain_text("加载失败")
                        expect(page.locator("#file-title")).to_have_text("历史甲.csv")
                        page.wait_for_function("historyItems.length === 1")
                        page.mouse.move(700, 300)
                        page.mouse.move(10, 450)
                        expect(panel).to_be_visible()
                        page.wait_for_function("Math.abs(document.getElementById('history-drawer').getBoundingClientRect().left) < .5")
                        page.mouse.move(100, 300)
                        expect(panel).to_be_visible()
                        page.evaluate("document.getElementById('toast').style.display = 'none'")
                        Path("shots").mkdir(exist_ok=True)
                        page.screenshot(path="shots/history-drawer.png")
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                service.should_exit = True
                thread.join(timeout=10)
                for name, value in originals.items():
                    setattr(server, name, value)


if __name__ == "__main__":
    unittest.main()
