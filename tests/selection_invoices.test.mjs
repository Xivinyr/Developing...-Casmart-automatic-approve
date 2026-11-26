import assert from 'node:assert/strict';
import test from 'node:test';
import { chromium } from 'playwright';
import { hydrateVisibleSelectionInvoices } from '../browser/selection_invoices.mjs';

test('下载仅限当前网页中和选区唯一匹配的可见发票', async () => {
  const browser = await chromium.launch(process.platform === 'win32' ? { channel: 'msedge' } : {});
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    const requests = [];
    const pdf = '%PDF-1.7 synthetic fixture';
    const inCell = (label, href, col = 22) => (
      `<div role="gridcell" aria-colindex="${col}"><a href="${href}">${label}</a></div>`
    );
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/sheet/synthetic') {
        return route.fulfill({
          contentType: 'text/html; charset=utf-8',
          body: `
            <nav><a href="/invoice/无关.pdf">无关.pdf</a></nav>
            <button id="old" role="tab" aria-selected="false">之前月份</button>
            <section role="tabpanel" aria-labelledby="old"><div role="grid">
              ${inCell('旧月.pdf', '/invoice/旧月.pdf')}
            </div></section>
            <section role="grid">
              ${inCell('Cafe\u0301.PDF', '/invoice/caf%C3%A9.pdf#view')}
              ${inCell('不同列.pdf', '/invoice/不同列.pdf', 23)}
              ${inCell('冲突.pdf', '/a/冲突.pdf')}
              ${inCell('冲突.pdf', '/b/冲突.pdf')}
              ${inCell('同名.pdf', '/invoice/同名.pdf')}
              <div style="margin-left: 2000px">${inCell('离屏.pdf', '/invoice/离屏.pdf')}</div>
              ${inCell('无PDF.pdf', '/invoice/无PDF.pdf')}
              ${inCell('改名.pdf', '/invoice/其他名字.pdf')}
              ${inCell('只有预览.pdf', '/invoice/preview')}
              ${inCell('外站.pdf', 'https://example.invalid/外站.pdf')}
            </section>
            <div role="dialog">
              <button>查看附件</button><a download="浮层.pdf" href="/download/fixed">下载</a>
            </div>`,
        });
      }
      requests.push(url.pathname);
      return route.fulfill({
        contentType: url.pathname.includes(encodeURIComponent('无PDF')) ? 'text/html' : 'application/pdf',
        body: url.pathname.includes(encodeURIComponent('无PDF')) ? '<html>登录</html>' : pdf,
      });
    });
    await page.goto('https://docs.qq.com/sheet/synthetic');
    const makeCell = text => ({ text, col: 22, invoice_link: true, assets: [] });
    const cells = [
      'Café.pdf', '浮层.pdf', '旧月.pdf', '无关.pdf', '不同列.pdf', '冲突.pdf',
      '同名.pdf', '同名.PDF', '离屏.pdf', '无PDF.pdf', '改名.pdf', '只有预览.pdf', '外站.pdf',
    ].map(makeCell);
    cells[7].assets.push({ source: 'existing', base64: 'existing' });
    const result = await hydrateVisibleSelectionInvoices(page, [{ cells }]);
    assert.deepEqual(result, { eligible: cells.length - 1, matched: 4, downloaded: 2, unavailable: 2 });
    assert.deepEqual(requests, [
      '/invoice/caf%C3%A9.pdf', '/download/fixed', '/invoice/' + encodeURIComponent('无PDF') + '.pdf',
    ]);
    assert.deepEqual(cells[0].assets, [{ source: 'visible_invoice_link', base64: Buffer.from(pdf).toString('base64') }]);
    assert.deepEqual(cells[1].assets, cells[0].assets);
    assert.equal(cells[7].assets[0].source, 'existing');
    assert.ok(cells.slice(2).every((cell, index) => index === 5 || cell.assets.length === 0));
  } finally {
    await browser.close();
  }
});
