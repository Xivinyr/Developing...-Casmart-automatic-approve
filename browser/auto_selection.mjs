const origin = 'https://docs.qq.com';
const gridMaxRow = 1_048_576;
const gridMaxCol = 16_384;
const waitMillis = 2200;

function failure(code) {
  return Object.assign(new Error(code), { code });
}

function columnNumber(column) {
  if (typeof column !== 'string' || !/^[A-Z]{1,3}$/.test(column)) return 0;
  return [...column].reduce((total, char) => total * 26 + char.charCodeAt(0) - 64, 0);
}

function cellAddress(value) {
  const parts = /^([A-Z]{1,3})([1-9]\d{0,6})$/.exec(value || '');
  if (!parts || columnNumber(parts[1]) > gridMaxCol || Number(parts[2]) > gridMaxRow) return null;
  return { col: parts[1], row: Number(parts[2]) };
}

function worksheetUrl(page) {
  try {
    const url = new URL(page.url());
    if (url.origin === origin && !url.username && !url.password && /^\/sheet\/[^/]+/.test(url.pathname)) {
      return url.href;
    }
  } catch {
    // 登录页和其他文档类型不接受表格快捷键。
  }
  throw failure('sheet_unavailable');
}

function inspectWorksheet({ href, tabs, wantBox, bounds }) {
  if (location.href !== href) return null;
  const visible = element => element.isConnected && !!element.getClientRects().length
    && !element.closest('[hidden],[inert],[aria-hidden="true"]')
    && getComputedStyle(element).visibility === 'visible';
  const selected = Array.from(document.querySelectorAll('[role="tab"][aria-selected="true"]'))
    .filter(visible).map(tab => [tab.id, tab.getAttribute('aria-controls'), tab.textContent.trim()]);
  const activeTabs = JSON.stringify(selected);
  if (tabs !== undefined && activeTabs !== tabs) return null;

  // 只有含明确地址框标识且当前显示合法坐标的文本输入才可写入。
  const names = new Set(['名称框', '单元格名称框', '单元格地址', '定位单元格',
    'name box', 'cell name box', 'cell address', 'go to cell']);
  const boxIds = new Set(['name-box', 'namebox', 'sheet-name-box', 'spreadsheet-name-box',
    'name-box-input', 'cell-address-box', 'cell-position-input']);
  const normalizeAddress = value => {
    const text = value.trim().toUpperCase().replace(/\$/g, '');
    return /^[A-Z]{1,3}[1-9]\d{0,6}(?::[A-Z]{1,3}[1-9]\d{0,6})?$/.test(text) ? text : null;
  };
  const boxes = Array.from(document.querySelectorAll('input')).filter(input => {
    if (input.type !== 'text' || input.disabled || input.readOnly || !visible(input)
        || input.getAttribute('aria-readonly') === 'true'
        || input.closest('[role="gridcell"],td,[role="dialog"],dialog,[role="alertdialog"],form')) return false;
    if (!normalizeAddress(input.value)) return false;
    const labelIds = input.getAttribute('aria-labelledby')?.split(/\s+/) || [];
    const labels = [
      input.getAttribute('aria-label'), input.getAttribute('title'), input.getAttribute('placeholder'),
      ...labelIds.map(id => document.getElementById(id)?.textContent),
      ...Array.from(input.labels || [], label => label.textContent),
    ];
    return labels.some(label => label && names.has(label.trim().toLowerCase()))
      || [input.id, input.getAttribute('data-testid')].some(value => value && boxIds.has(value.toLowerCase()));
  });
  const box = boxes.length === 1 ? boxes[0] : null;
  if (wantBox) return box;
  let ariaRangeConfirmed = false;
  if (box && bounds) {
    // 有些工作表的位置框只显示左上角。只有 ARIA 网格完整给出同一选区时才接受。
    const grids = Array.from(document.querySelectorAll('[role="grid"],[role="treegrid"]')).filter(visible);
    const activeGrids = grids.filter(grid => {
      const panel = grid.closest('[role="tabpanel"]');
      if (!panel) return true;
      const labels = panel.getAttribute('aria-labelledby')?.split(/\s+/) || [];
      return !labels.some(id => document.getElementById(id)?.getAttribute('aria-selected') === 'false');
    });
    if (activeGrids.length === 1) {
      const seen = new Set();
      const cells = activeGrids[0].querySelectorAll('[role="gridcell"][aria-selected="true"]');
      for (const cell of cells) {
        const row = Number(cell.getAttribute('aria-rowindex')
          || cell.closest('[role="row"]')?.getAttribute('aria-rowindex'));
        const col = Number(cell.getAttribute('aria-colindex'));
        if (!Number.isInteger(row) || !Number.isInteger(col)
            || row < bounds.startRow || row > bounds.endRow || col < bounds.startCol || col > bounds.endCol) {
          seen.clear();
          break;
        }
        seen.add(row + ':' + col);
      }
      ariaRangeConfirmed = seen.size === (bounds.endRow - bounds.startRow + 1) * (bounds.endCol - bounds.startCol + 1);
    }
  }
  const active = document.activeElement;
  const inEditor = active && (active.isContentEditable || active.matches('input,textarea,select,[role="textbox"],[role="searchbox"]'));
  return {
    tabs: activeTabs,
    address: box ? normalizeAddress(box.value) : null,
    boxFocused: box !== null && active === box,
    gridReady: !inEditor && !active?.closest('[role="dialog"],dialog,[role="alertdialog"]'),
    ariaRangeConfirmed,
  };
}

function pause(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

export async function copyCurrentSheetRange(page, {
  startRow = 2, endRow, startCol = 'T', endCol = 'W', maxRows = 1000, beforeCopy,
} = {}) {
  const left = columnNumber(startCol);
  const right = columnNumber(endCol);
  if (!left || !right || right < left || right > gridMaxCol || !Number.isInteger(startRow)
      || startRow < 1 || startRow > gridMaxRow || !Number.isInteger(maxRows) || maxRows < 1
      || (endRow !== undefined && (!Number.isInteger(endRow) || endRow < startRow || endRow > gridMaxRow))) {
    throw failure('invalid_range');
  }
  if (endRow !== undefined && endRow - startRow + 1 > maxRows) throw failure('range_too_large');
  const href = worksheetUrl(page);
  let tabs;
  async function state(extra = {}) {
    if (page.url() !== href) throw failure('sheet_changed');
    let current;
    try {
      current = await page.evaluate(inspectWorksheet, { href, tabs, ...extra });
    } catch {
      throw failure('sheet_changed');
    }
    if (!current) throw failure('sheet_changed');
    return current;
  }
  async function waitFor(predicate, code, extra) {
    const deadline = Date.now() + waitMillis;
    do {
      const current = await state(extra);
      if (predicate(current)) return current;
      await pause(65);
    } while (Date.now() < deadline);
    throw failure(code);
  }
  const initial = await state();
  tabs = initial.tabs;
  try {
    await page.bringToFront();
  } catch {
    throw failure('sheet_changed');
  }
  await waitFor(current => current.address !== null, 'name_box_unavailable');
  async function goTo(address, matches, bounds) {
    let handle;
    try {
      handle = await page.evaluateHandle(inspectWorksheet, { href, tabs, wantBox: true });
      const box = handle.asElement();
      if (!box) {
        await state();
        throw failure('name_box_unavailable');
      }
      await box.click({ timeout: waitMillis });
      if (!(await state()).boxFocused) throw failure('range_unverified');
      await box.fill(address, { timeout: waitMillis });
      if (!(await state()).boxFocused) throw failure('range_unverified');
      await box.press('Enter', { timeout: waitMillis });
    } catch (error) {
      await state();
      if (error.code === 'name_box_unavailable') throw error;
      throw failure('range_unverified');
    } finally {
      await handle?.dispose().catch(() => {});
    }
    return waitFor(current => current.gridReady && matches(current), 'range_unverified', bounds && { bounds });
  }
  if (endRow === undefined) {
    await goTo('A1', current => current.address === 'A1');
    try {
      await page.keyboard.press('Control+End');
    } catch {
      await state();
      throw failure('last_row_unverified');
    }
    const last = await waitFor(current => current.gridReady && current.address !== 'A1'
      && cellAddress(current.address) !== null, 'last_row_unverified');
    endRow = cellAddress(last.address).row;
    if (endRow < startRow) throw failure('last_row_unverified');
    if (endRow - startRow + 1 > maxRows) throw failure('range_too_large');
  }
  const first = startCol + startRow;
  const last = endCol + endRow;
  const range = first === last ? first : first + ':' + last;
  const bounds = { startRow, endRow, startCol: left, endCol: right };
  const rangeMatches = current => current.address === range
    || (current.address === first && current.ariaRangeConfirmed);
  await goTo(range, rangeMatches, bounds);
  let sequence;
  if (beforeCopy) {
    try {
      sequence = await beforeCopy();
    } catch {
      throw failure('clipboard_guard_failed');
    }
  }
  const ready = await state({ bounds });
  if (!ready.gridReady || !rangeMatches(ready)) throw failure('range_unverified');
  try {
    await page.keyboard.press('Control+c');
  } catch {
    await state();
    throw failure('copy_failed');
  }
  await state();
  return { startRow, endRow, range, sequence };
}
