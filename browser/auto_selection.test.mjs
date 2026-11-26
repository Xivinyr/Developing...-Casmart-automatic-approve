import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import test from 'node:test';
import { chromium } from 'playwright';
import { copyCurrentSheetRange } from './auto_selection.mjs';

const html = `
  <button id="this-month" role="tab" aria-selected="true">本月</button>
  <button id="last-month" role="tab" aria-selected="false">上月</button>
  <input aria-label="名称框" id="location" value="C4">
  <input aria-label="公式栏" id="formula" value="A1">
  <div id="grid" role="grid" tabindex="0"></div>
  <script>
    const box = document.getElementById('location');
    const grid = document.getElementById('grid');
    window.fixture = { actions: [], mode: 'normal' };
    box.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const request = box.value;
      fixture.actions.push(request);
      grid.focus();
      if (fixture.mode === 'reject-range' && request.includes(':')) box.value = 'T2';
      if (fixture.mode === 'aria-range' && request === 'T2:W3') {
        box.value = 'T2';
        for (const row of [2, 3]) for (const col of [20, 21, 22, 23]) {
          const cell = document.createElement('div');
          cell.setAttribute('role', 'gridcell');
          cell.setAttribute('aria-selected', 'true');
          cell.setAttribute('aria-rowindex', row);
          cell.setAttribute('aria-colindex', col);
          grid.append(cell);
        }
      }
    });
    grid.addEventListener('keydown', event => {
      if (!event.ctrlKey) return;
      if (event.key === 'End') {
        event.preventDefault();
        fixture.actions.push('end');
        box.value = fixture.mode === 'unsupported-end' ? 'A1' : 'Y34';
      }
      if (event.key.toLowerCase() === 'c') {
        event.preventDefault();
        fixture.actions.push('copy');
      }
    });
  </script>
`;

test('验证工作表和区间后才发送复制，并对不可信状态停止', { timeout: 30000 }, async () => {
  const useEdge = process.platform === 'win32' && !existsSync(chromium.executablePath());
  const browser = await chromium.launch({ headless: true, ...(useEdge ? { channel: 'msedge' } : {}) });
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => {
      if (route.request().url() === 'https://docs.qq.com/sheet/synthetic') {
        return route.fulfill({ contentType: 'text/html; charset=UTF-8', body: html });
      }
      return route.abort();
    });
    async function load(mode = 'normal') {
      await page.goto('https://docs.qq.com/sheet/synthetic');
      await page.evaluate(value => { fixture.mode = value; }, mode);
    }
    const actions = () => page.evaluate(() => fixture.actions);
    await load();
    const result = await copyCurrentSheetRange(page, {
      beforeCopy: async () => {
        assert.deepEqual(await actions(), ['A1', 'end', 'T2:W34']);
        return 721;
      },
    });
    assert.deepEqual(result, { startRow: 2, endRow: 34, range: 'T2:W34', sequence: 721 });
    assert.deepEqual(await actions(), ['A1', 'end', 'T2:W34', 'copy']);
    assert.equal(await page.locator('#formula').inputValue(), 'A1');

    await load('aria-range');
    const aria = await copyCurrentSheetRange(page, { endRow: 3 });
    assert.equal(aria.range, 'T2:W3');
    assert.deepEqual(await actions(), ['T2:W3', 'copy']);

    await load('reject-range');
    await assert.rejects(copyCurrentSheetRange(page, { endRow: 5 }), { code: 'range_unverified' });
    assert.deepEqual(await actions(), ['T2:W5']);

    await load('unsupported-end');
    await assert.rejects(copyCurrentSheetRange(page), { code: 'last_row_unverified' });
    assert.deepEqual(await actions(), ['A1', 'end']);

    await load();
    await assert.rejects(copyCurrentSheetRange(page, { endRow: 3, beforeCopy: async () => {
      await page.evaluate(() => {
        document.getElementById('this-month').setAttribute('aria-selected', 'false');
        document.getElementById('last-month').setAttribute('aria-selected', 'true');
      });
    } }), { code: 'sheet_changed' });
    assert.deepEqual(await actions(), ['T2:W3']);

    await load();
    await page.evaluate(() => document.getElementById('location').setAttribute('aria-label', '搜索'));
    await assert.rejects(copyCurrentSheetRange(page, { endRow: 3 }), { code: 'name_box_unavailable' });
    assert.deepEqual(await actions(), []);
  } finally {
    await browser.close();
  }
});
