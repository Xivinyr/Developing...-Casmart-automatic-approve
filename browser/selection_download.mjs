const docsOrigin = 'https://docs.qq.com';
const maxAttempts = 60;
const maxMillis = 60_000;

function pdfKey(name) {
  const value = name.trim();
  if (!/\.pdf$/i.test(value) || value.length < 5 || /[<>:"/\\|?*\x00-\x1f]/.test(value)) return null;
  return value.normalize('NFC').toLowerCase();
}

// 只检查浏览器公开 DOM。由 Playwright 对已核对的元素执行普通点击。
function inspectAttachment({ href, key, col, phase, file, before }) {
  if (location.href !== href || location.origin !== 'https://docs.qq.com') return null;
  const actions = 'a,button,[role="button"],[role="link"]';
  const gridCell = 'td,[role="gridcell"]';
  const activePanel = element => {
    const panel = element.closest('[role="tabpanel"]');
    if (!panel) return true;
    const labelIds = panel.getAttribute('aria-labelledby')?.split(/\s+/) || [];
    const tabs = new Set(labelIds.map(id => document.getElementById(id))
      .filter(tab => tab?.matches('[role="tab"]')));
    if (panel.id) for (const tab of document.querySelectorAll('[role="tab"][aria-controls]')) {
      if (tab.getAttribute('aria-controls').split(/\s+/).includes(panel.id)) tabs.add(tab);
    }
    return !tabs.size || Array.from(tabs).some(tab => tab.getAttribute('aria-selected') === 'true');
  };
  const shown = element => {
    if (!element.isConnected || element.closest('[hidden],[inert],[aria-hidden="true"],[aria-disabled="true"]')
        || !activePanel(element)) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight
      && rect.right > 0 && rect.left < innerWidth && style.visibility === 'visible' && style.opacity !== '0';
  };
  const labels = element => {
    const ids = element.getAttribute('aria-labelledby')?.split(/\s+/) || [];
    return [element.getAttribute('aria-label'), element.getAttribute('title'), element.innerText,
      ids.map(id => document.getElementById(id)?.innerText || '').join(' ')]
      .map(text => text?.trim()).filter(Boolean);
  };
  const fileLabel = text => text?.trim().normalize('NFC').toLowerCase() === key;
  const matchesColumn = cell => {
    const index = cell.getAttribute('aria-colindex');
    return !index || Number(index) === col;
  };
  const actionLabel = (element, accepted) => {
    const names = labels(element);
    return names.length > 0 && names.every(name => accepted.includes(name));
  };
  const interactive = (element, samePage) => {
    if (!shown(element) || element.matches(':disabled') || (element.form && element.type !== 'button')) return false;
    const href = element.getAttribute('href');
    if (href === null && !element.matches('button,[role="button"],[role="link"],a')) return false;
    if (!href) return href === null;
    try {
      const url = new URL(href, location.href);
      if (url.origin !== location.origin || url.username || url.password || (element.target && element.target !== '_self')) {
        return false;
      }
      return !samePage || (url.pathname === location.pathname && url.search === location.search);
    } catch {
      return false;
    }
  };
  const downloadWords = ['下载', '下载附件', '下载文件', 'Download', 'Download attachment'];
  const viewWords = ['查看附件', '预览附件', 'View attachment'];
  const downloadButtons = node => Array.from(node.querySelectorAll(actions))
    .filter(element => interactive(element, false) && actionLabel(element, downloadWords));

  if (phase === 'trigger') {
    const matches = Array.from(document.querySelectorAll(actions)).filter(element => {
      const names = labels(element);
      const cell = element.closest(gridCell);
      return cell && matchesColumn(cell) && interactive(element, true) && !element.hasAttribute('download')
        && names.length > 0 && names.every(fileLabel);
    });
    return matches.length === 1 ? matches[0] : null;
  }
  if (phase === 'before') return downloadButtons(document);
  if (!file?.isConnected) return null;
  const cell = file.closest(gridCell);
  if (!cell || !matchesColumn(cell)) return null;

  // 允许同一单元格中的浮层，以及名称或 ARIA 关系明确指向该附件的独立卡片。
  const targets = new Set();
  const cardSelector = 'dialog,[popover],[role="dialog"],[role="menu"],[role="toolbar"],[role="group"]';
  const labelledBy = node => node.getAttribute('aria-labelledby')?.split(/\s+/) || [];
  const controls = file.getAttribute('aria-controls')?.split(/\s+/) || [];
  const cardNames = card => [
    card.getAttribute('aria-label'), card.getAttribute('title'),
    ...labelledBy(card).map(id => document.getElementById(id)?.innerText),
    ...Array.from(card.querySelectorAll('h1,h2,h3,[role="heading"]')).filter(shown).flatMap(labels),
  ].filter(text => /\.pdf$/i.test(text?.trim() || ''));
  const cards = [cell, ...document.querySelectorAll(cardSelector)];
  for (const card of cards) {
    if (!shown(card)) continue;
    const related = card === cell || (cell.contains(card) && !card.querySelector(gridCell))
      || (card.id && controls.includes(card.id))
      || (file.id && labelledBy(card).includes(file.id))
      || labels(card).some(fileLabel)
      || Array.from(card.querySelectorAll('h1,h2,h3,[role="heading"]'))
        .some(title => shown(title) && labels(title).some(fileLabel));
    if (!related || (card !== cell && cardNames(card).some(name => !fileLabel(name)))
        || !Array.from(card.querySelectorAll(actions)).some(element => shown(element) && actionLabel(element, viewWords))) continue;
    for (const button of downloadButtons(card)) {
      const panel = button.closest(cardSelector);
      if (!before.includes(button) && (!panel || !card.contains(panel)
          || cardNames(panel).every(fileLabel))) targets.add(button);
    }
  }
  return targets.size === 1 ? targets.values().next().value : null;
}

export async function downloadVisibleSelectionInvoices(page, sheets) {
  const result = { attempted: 0, downloaded: 0 };
  let initialUrl;
  try {
    initialUrl = new URL(page.url());
  } catch {
    return result;
  }
  if (initialUrl.origin !== docsOrigin || initialUrl.username || initialUrl.password
      || !/^\/sheet(?:\/|$)/.test(initialUrl.pathname)) return result;

  const filenames = new Map();
  for (const sheet of sheets) for (const cell of sheet.cells) {
    if (cell.invoice_link !== true) continue;
    const key = pdfKey(cell.text);
    if (key) filenames.set(key, {
      count: (filenames.get(key)?.count || 0) + 1, col: cell.col, empty: cell.assets.length === 0,
    });
  }
  // 保留屏幕内已经定位的文件；不滚动、切表或推测未显示单元格的位置。
  const deadline = Date.now() + maxMillis;
  let opened = 0;
  for (const [key, { count, col, empty }] of filenames) {
    if (count !== 1 || !empty) continue;
    if (Date.now() >= deadline || opened >= maxAttempts || page.isClosed() || page.url() !== initialUrl.href) break;
    const args = { href: initialUrl.href, key, col };
    const timeLeft = max => Math.max(1, Math.min(max, deadline - Date.now()));
    let trigger;
    let before;
    let control;
    try {
      trigger = await page.evaluateHandle(inspectAttachment, { ...args, phase: 'trigger' });
      const file = trigger.asElement();
      if (!file) continue;
      before = await page.evaluateHandle(inspectAttachment, { ...args, phase: 'before' });
      opened++;
      await file.click({ timeout: timeLeft(900) });
      control = await page.waitForFunction(inspectAttachment, { ...args, phase: 'card', file, before },
        { timeout: timeLeft(1100), polling: 60 });
      if (Date.now() >= deadline || page.url() !== initialUrl.href) break;
      const button = control.asElement();
      if (!button) continue;
      const downloading = page.waitForEvent('download', {
        predicate: download => pdfKey(download.suggestedFilename()) === key, timeout: timeLeft(2400),
      }).catch(() => null);
      result.attempted++;
      await button.click({ timeout: timeLeft(900) });
      if (await downloading) result.downloaded++;
    } catch {
      // 无法核对文件名或下载按钮时跳过，保留该条记录为缺项。
    } finally {
      await Promise.allSettled([trigger, before, control].filter(Boolean).map(handle => handle.dispose()));
    }
  }
  return result;
}
