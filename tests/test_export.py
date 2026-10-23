"""PNG export regression, including the visible HTML tooltip."""
import base64
from functools import partial
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright, expect
from test_sampling import QuietHandler, ROOT


class ExportTest(unittest.TestCase):
    def test_shortcut_exports_hover_tooltip(self):
        service = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT)))
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright, tempfile.TemporaryDirectory() as directory:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1200, "height": 800}, accept_downloads=True)
                    errors, downloads = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('download', lambda download: downloads.append(download))
                    def api(route):
                        url = route.request.url
                        route.fulfill(json={"has": False} if '/api/session' in url else {"items": [], "next": 0})
                    page.route('**/api/**', api)
                    page.goto(f'http://127.0.0.1:{service.server_port}/static/index.html', wait_until='networkidle')
                    page.keyboard.press('Control+Shift+E')
                    self.assertEqual(downloads, [])
                    point = page.evaluate("""() => {
                      DATA={file:'export-test.csv',rows:100,tCol:'time',
                        t:Array.from({length:100},(_,i)=>1700000000000+i*1000),
                        cols:{speed:{v:Array.from({length:100},(_,i)=>i%20),unit:'',desc:''}}};
                      selected=new Set(['speed']);window.__restoreZoom=[0,100];afterLoad();
                      const point=chart.convertToPixel({xAxisIndex:0,yAxisIndex:0},[DATA.t[40],DATA.cols.speed.v[40]]);
                      const box=document.getElementById('chart').getBoundingClientRect();
                      return {x:point[0]+box.left,y:point[1]+box.top};
                    }""")
                    page.mouse.move(point['x'], point['y']-20)
                    tip=page.locator('#chart .chart-tooltip')
                    expect(tip).to_be_visible()
                    expect(tip).to_contain_text('speed')
                    snapshot=page.evaluate("""() => {
                      const tip=document.querySelector('#chart .chart-tooltip');
                      const box=document.getElementById('chart').getBoundingClientRect();
                      const rect=tip.getBoundingClientRect();
                      const marker=tip.querySelector('span');const m=marker.getBoundingClientRect();
                      return {baseline:chart.renderToCanvas({pixelRatio:2,backgroundColor:'#fff'}).toDataURL(),
                        rect:[rect.left-box.left,rect.top-box.top,rect.width,rect.height],
                        marker:[m.left-box.left+m.width/2,m.top-box.top+m.height/2]};
                    }""")
                    with page.expect_download() as event:
                        page.keyboard.press('Control+Shift+E')
                    download=event.value
                    self.assertTrue(download.suggested_filename.startswith('export-test_'))
                    filename=Path(directory)/'hover.png'
                    download.save_as(str(filename))
                    exported=Image.open(filename).convert('RGB')
                    original=Image.open(BytesIO(base64.b64decode(snapshot['baseline'].split(',')[1]))).convert('RGB')
                    self.assertEqual(exported.size,original.size)
                    self.assertIsNotNone(ImageChops.difference(exported,original).getbbox())
                    mx,my=snapshot['marker']
                    self.assertEqual(exported.getpixel((round(mx*2),round(my*2))), (22,119,255))
                    # Pixels outside the tooltip and its shadow retain the original chart.
                    x,y,w,h=snapshot['rect']
                    delta=ImageChops.difference(exported,original).getbbox()
                    self.assertGreaterEqual(delta[0],max(0,int((x-20)*2)))
                    self.assertLessEqual(delta[2],int((x+w+20)*2))
                    Path(ROOT,'shots').mkdir(exist_ok=True)
                    exported.save(ROOT/'shots'/'export-with-tooltip.png')
                    page.locator('#cols .name').dblclick()
                    page.locator('#cols .name input').focus()
                    count=len(downloads)
                    page.keyboard.press('Control+Shift+E')
                    page.wait_for_timeout(150)
                    self.assertEqual(len(downloads),count)
                    page.locator('#cols .name input').press('Escape')
                    page.mouse.move(1100,20)
                    expect(tip).to_be_hidden()
                    with page.expect_download():
                        page.locator('#btn-export').click()
                    self.assertEqual(errors,[])
                finally:
                    browser.close()
        finally:
            service.shutdown();service.server_close();thread.join(timeout=5)


if __name__=='__main__':
    unittest.main()
