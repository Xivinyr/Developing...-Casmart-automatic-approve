// 只用本机合成服务验证 Chromium、授权 Cookie Fetch 和正常下载事件。
const { chromium } = require('playwright');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const pdf = Buffer.from('%PDF-1.4\nsynthetic-local\n%%EOF');
const server = http.createServer((req,res)=>{
  if (req.url === '/') {
    res.setHeader('Content-Type','text/html');
    res.end('<a href="/download">Export</a>');
  } else if (req.headers.cookie !== 'synthetic=authorized') {
    res.writeHead(401);res.end('unauthorized');
  } else {
    res.setHeader('Content-Type','application/pdf');
    if (req.url === '/download') res.setHeader('Content-Disposition','attachment; filename="fixture.pdf"');
    res.end(pdf);
  }
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  let browser;
  try {
    browser = await chromium.launch({headless:true,channel:'chromium'});
    const context = await browser.newContext({acceptDownloads:true});
    const origin = `http://127.0.0.1:${server.address().port}`;
    await context.addCookies([{name:'synthetic',value:'authorized',url:origin}]);
    const page = await context.newPage();
    await page.goto(origin);
    const data = await page.evaluate(async()=>Array.from(new Uint8Array(await (await fetch('/asset',{credentials:'include'})).arrayBuffer())));
    assert.deepEqual(Buffer.from(data),pdf);
    const event = page.waitForEvent('download');
    await page.getByRole('link',{name:'Export'}).click();
    const download = await event;
    const dest = path.resolve('.local/smoke/fixture.pdf');
    await fs.mkdir(path.dirname(dest),{recursive:true});
    await download.saveAs(dest);
    assert.deepEqual(await fs.readFile(dest),pdf);
    const empty = await browser.newContext();
    const response = await empty.request.get(origin+'/asset');
    assert.equal(response.status(),401);
    console.log(JSON.stringify({browser:browser.version(),cookieFetch:'PASS',download:'PASS',unauthorized401:'PASS',scope:'localhost synthetic only'}));
  } finally { if(browser) await browser.close();server.close(); }
})().catch(()=>{console.error('Browser smoke failed; Chromium may require installation.');process.exitCode=1;});
