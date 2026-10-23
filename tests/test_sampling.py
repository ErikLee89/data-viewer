"""View-dependent sampling regression: python tests/test_sampling.py."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class SamplingTest(unittest.TestCase):
    def test_large_series_zoom_and_gaps(self):
        service = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT)))
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    def api(route):
                        url = route.request.url
                        if "/api/session" in url:
                            route.fulfill(json={"has": False})
                        elif "/api/cache" in url:
                            route.fulfill(json={"items": []})
                        else:
                            route.fulfill(json={"items": [], "next": 0})
                    page.route("**/api/**", api)
                    page.goto(f"http://127.0.0.1:{service.server_port}/static/index.html", wait_until="networkidle")
                    result = page.evaluate("""() => {
                      const n = 170000, base = 1700000000000;
                      const times = Array.from({length:n}, (_,i) => base + i * 1000);
                      const values = times.map((_,i) => Math.sin(i / 50));
                      values[12345] = 200; values[12346] = -150;
                      for (let i = 30000; i < 31000; i++) values[i] = null;
                      DATA = {file:'17万点验证.csv', rows:n, tCol:'time', t:times,
                        cols:{speed:{v:values, unit:'', desc:''}}};
                      selected = new Set(['speed']);
                      window.__restoreZoom = [0,100];
                      const begin = performance.now();
                      afterLoad();
                      const points = chart.getOption().series[0].data;
                      const overview = chart.getOption().series.find(s => s.id === 'overview').data;
                      return {count:points.length, max:Math.max(...points.map(p=>p[1] ?? -Infinity)),
                        min:Math.min(...points.map(p=>p[1] ?? Infinity)),
                        gap:points.some(p=>p[1] === null), overview,
                        milliseconds:performance.now()-begin};
                    }""")
                    self.assertLess(result["count"], 5000)
                    self.assertEqual(result["max"], 200)
                    self.assertEqual(result["min"], -150)
                    self.assertTrue(result["gap"])
                    print(f"170000 points -> {result['count']} display points; initial render {result['milliseconds']:.1f} ms")
                    # Zoom to a region with < 2000 points; every local raw point must return.
                    page.evaluate("chart.dispatchAction({type:'dataZoom',start:7,end:7.5})")
                    page.wait_for_function("chart.getOption().series[0].data[0][0] > DATA.t[11000]")
                    detail = page.evaluate("""() => {
                      const points = chart.getOption().series[0].data;
                      const lo = DATA.t[0] + (DATA.t.at(-1)-DATA.t[0]) * .07;
                      const hi = DATA.t[0] + (DATA.t.at(-1)-DATA.t[0]) * .075;
                      const raw = DATA.t.map((t,i)=>[t,DATA.cols.speed.v[i]]).filter(p=>p[0]>=lo&&p[0]<=hi);
                      return {all:raw.every(p=>points.some(q=>q[0]===p[0]&&q[1]===p[1])),
                        count:points.length, zoom:currentChartZoom(),
                        overview:chart.getOption().series.find(s=>s.id==='overview').data,
                        tip:chart.getOption().tooltip[0].formatter([{value:[DATA.t[12345],0]}])};
                    }""")
                    self.assertTrue(detail["all"])
                    self.assertEqual(detail["zoom"], [7, 7.5])
                    self.assertEqual(detail["overview"], result["overview"])
                    self.assertIn("200", detail["tip"])
                    print(f"Zoomed range: {detail['count']} points; all original visible points restored")
                    slider_point = page.evaluate("""() => {
                      const r = chart.getModel().getComponent('grid').coordinateSystem.getRect();
                      const box = document.getElementById('chart').getBoundingClientRect();
                      return {x:box.left+r.x+r.width/2, y:box.top+chart.getHeight()-30,
                        expected:fmtTFull((DATA.t[0]+DATA.t.at(-1))/2)};
                    }""")
                    page.mouse.move(slider_point['x'], slider_point['y'])
                    page.wait_for_function("document.getElementById('slider-time-tooltip').style.display === 'block'")
                    self.assertEqual(page.locator('#slider-time-tooltip').inner_text(), slider_point['expected'])
                    # Repeated panning must preserve the full-domain slider coordinates.
                    for start, end in [(80, 81), (30, 30.1), (7, 7.5), (0, 100)]:
                        page.evaluate("([start,end]) => chart.dispatchAction({type:'dataZoom',start,end})", [start,end])
                        page.wait_for_timeout(150)
                        self.assertEqual(page.evaluate("currentChartZoom()"), [start,end])
                        bounds = page.evaluate("chart.getModel().getComponent('xAxis',0).axis.scale.getExtent()")
                        expected = page.evaluate("([start,end]) => [start,end].map(v => DATA.t[0]+(DATA.t.at(-1)-DATA.t[0])*v/100)", [start,end])
                        self.assertEqual(bounds, expected)
                    page.set_viewport_size({"width": 1000, "height": 700})
                    page.wait_for_timeout(200)
                    self.assertLess(page.evaluate("chart.getOption().series[0].data.length"), result["count"])
                    # M4 retains first/last and extrema in each bucket, including null runs.
                    checks = page.evaluate("""() => {
                      const t = Array.from({length:100},(_,i)=>i);
                      const v = t.map(i=>i%5);
                      v[10]=100;v[11]=-100;v[12]=null;v[13]=null;
                      const p=TimeSeriesSampling.sample(t,v,0,99,2);
                      const allNull=TimeSeriesSampling.sample(t,t.map(()=>null),0,99,2);
                      return {p, allNull, unchanged:v[12]===null&&v[10]===100};
                    }""")
                    self.assertIn([0, 0], checks["p"])
                    self.assertIn([10, 100], checks["p"])
                    self.assertIn([11, -100], checks["p"])
                    self.assertIn([12, None], checks["p"])
                    self.assertIn([14, 4], checks["p"])
                    self.assertIn([99, 4], checks["p"])
                    self.assertTrue(checks["unchanged"])
                    self.assertTrue(all(p[0] is not None and p[1] is None for p in checks["allNull"]))
                    Path(ROOT, 'shots').mkdir(exist_ok=True)
                    page.screenshot(path=str(ROOT / 'shots' / 'dynamic-sampling.png'))
                    # Empty selection and angle columns must not retain obsolete series.
                    angle = page.evaluate("""() => {
                      DATA = {file:'angle.csv', rows:3, tCol:'time', t:[1700000000000,1700000001000,1700000002000],
                        cols:{HDG:{v:[359,0,1],unit:'deg',desc:''}}};
                      selected = new Set(['HDG']); window.__restoreZoom=[0,100]; afterLoad();
                      const opt=chart.getOption();
                      const result={values:opt.series[0].data.map(p=>p[1]), axis:opt.series[0].yAxisIndex,
                        tip:opt.tooltip[0].formatter([{value:[DATA.t[1],360]}])};
                      selected.clear();renderChart();
                      result.remaining=chart.getOption().series.map(s=>s.id);
                      return result;
                    }""")
                    self.assertEqual(angle['values'], [359,360,361])
                    self.assertEqual(angle['axis'], 1)
                    self.assertIn('<b>0</b>', angle['tip'])
                    self.assertEqual(angle['remaining'], ['overview'])
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
        finally:
            service.shutdown()
            service.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
