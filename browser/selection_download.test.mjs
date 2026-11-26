import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import test from 'node:test';
import { chromium } from 'playwright';
import { downloadVisibleSelectionInvoices } from './selection_download.mjs';

test('只点击当前工作表里能够核对文件归属的下载按钮', { timeout: 25_000 }, async () => {
  const useEdge = process.platform === 'win32' && !existsSync(chromium.executablePath());
  const browser = await chromium.launch({ headless: true, ...(useEdge ? { channel: 'msedge' } : {}) });
  try {
    const page = await browser.newPage({ acceptDownloads: true });
    const files = ['good', 'ambiguous', 'mismatch', 'duplicate', 'differentColumn', 'notSelected'];
    const trigger = name => '<button type="button" data-file="' + name + '">' + name + '.pdf</button>';
    const pdf = '%PDF-1.4\nsynthetic only\n%%EOF';
    const table = '<table><tbody>' + files.map(name => '<tr><td aria-colindex="'
      + (name === 'differentColumn' ? '23' : '22') + '">' + trigger(name) + '</td></tr>').join('') + '</tbody></table>';
    const html = '<main><button role="tab" id="past-tab" aria-controls="past" aria-selected="false">上月</button>'
      + '<section role="tabpanel" id="past" aria-labelledby="past-tab"><table><tr><td aria-colindex="22">'
      + trigger('past') + '</td></tr></table></section>' + table + '</main>'
      + '<script>window.opened = []; window.deleted = 0; window.otherDownloads = 0; '
      + "window.savePdf = () => { const a = document.createElement('a'); a.download = 'good.pdf'; a.href = "
      + "URL.createObjectURL(new Blob([" + JSON.stringify(pdf) + "], {type:'application/pdf'})); a.click(); };"
      + "document.querySelectorAll('[data-file]').forEach(button => button.addEventListener('click', () => {"
      + "const file = button.dataset.file; window.opened.push(file); document.querySelector('[role=toolbar]')?.remove();"
      + "const toolbar = document.createElement('aside'); toolbar.setAttribute('role','toolbar');"
      + "toolbar.setAttribute('aria-label', (file === 'mismatch' ? 'other' : file) + '.pdf');"
      + "toolbar.innerHTML = '<button type=\"button\">查看附件</button>'"
      + "+ '<button type=\"button\" onclick=\"window.savePdf()\" aria-label=\"下载\"></button>'"
      + "+ '<button type=\"button\" onclick=\"window.deleted++\" aria-label=\"删除\"></button>';"
      + "if (file === 'ambiguous') toolbar.innerHTML += '<button type=\"button\" title=\"下载附件\" onclick=\"window.otherDownloads++\"></button>';"
      + "button.closest('td').append(toolbar); document.querySelectorAll('[aria-label]').forEach(el => {"
      + "if (el.tagName === 'A' || el.tagName === 'BUTTON') { el.style.width='28px'; el.style.height='22px'; el.style.display='inline-block'; }"
      + "}); }));</script>";
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== 'https://docs.qq.com') return route.abort();
      if (url.pathname === '/sheet/synthetic') return route.fulfill({ contentType: 'text/html; charset=UTF-8', body: html });
      return route.abort();
    });
    await page.goto('https://docs.qq.com/sheet/synthetic');
    const downloads = [];
    page.on('download', download => downloads.push(download));
    const selected = (name, assets = []) => ({ text: name + '.pdf', col: 22, invoice_link: true, assets });
    const cells = ['good', 'ambiguous', 'mismatch', 'duplicate', 'differentColumn', 'past'].map(name => selected(name));
    cells.push(selected('duplicate', [{ base64: 'example', source: 'example' }]));
    const result = await downloadVisibleSelectionInvoices(page, [{ cells }]);
    const activity = await page.evaluate(() => ({ opened, deleted, otherDownloads }));
    assert.deepEqual(activity, { opened: ['good', 'ambiguous', 'mismatch'], deleted: 0, otherDownloads: 0 });
    assert.deepEqual(result, { attempted: 1, downloaded: 1 });
    assert.equal(downloads.length, 1);
    assert.equal(readFileSync(await downloads[0].path(), 'utf8'), pdf);
  } finally {
    await browser.close();
  }
});
