import { hydrateInvoiceLinks } from './invoice_links.mjs';

const docsOrigin = 'https://docs.qq.com';

function invoiceName(value) {
  if (typeof value !== 'string') return null;
  const name = value.trim().normalize('NFC').toLowerCase().normalize('NFC');
  return name.length > 4 && name.endsWith('.pdf') && !/[<>:"/\\|?*\x00-\x1f]/.test(name)
    ? name : null;
}

async function visibleLinks(page, names) {
  return page.evaluate(({ origin, names }) => {
    if (location.origin !== origin) return [];
    const wanted = new Set(names);
    const nameKey = value => value?.trim().normalize('NFC').toLowerCase().normalize('NFC');
    function visible(element) {
      if (element.closest('[hidden], [inert], [aria-hidden="true"]')
          || !element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0
        && rect.left < innerWidth && rect.top < innerHeight;
    }
    const links = [];
    for (const anchor of document.querySelectorAll('a[href]')) {
      const href = anchor.getAttribute('href').trim();
      if (!href || href.startsWith('#') || !visible(anchor)) continue;
      const label = anchor.innerText;
      const download = anchor.getAttribute('download');
      if (!wanted.has(nameKey(label)) && !wanted.has(nameKey(download))) continue;
      const panel = anchor.closest('[role="tabpanel"]');
      const tabIds = panel?.getAttribute('aria-labelledby')?.split(/\s+/) || [];
      if (tabIds.some(id => document.getElementById(id)?.getAttribute('aria-selected') === 'false')) continue;

      // 只处理工作表数据格或已经打开的附件浮层，排除导航、文档列表等同名链接。
      const cell = anchor.closest('[role="gridcell"], td');
      const grid = cell?.closest('[role="grid"], [role="treegrid"]');
      const popover = anchor.closest('[role="dialog"], [role="menu"], [role="tooltip"]');
      const actions = popover?.querySelectorAll('button, a, [role="button"], [role="menuitem"]') || [];
      if (!grid && !Array.from(actions).some(action => (action.innerText || '').trim() === '查看附件' && visible(action))) {
        continue;
      }
      const col = grid ? cell.getAttribute('aria-colindex') : null;
      links.push({ label, download, href: anchor.href, col });
    }
    return links;
  }, { origin: docsOrigin, names });
}

function invoiceLinkEvidence(link) {
  try {
    const url = new URL(link.href);
    if (url.protocol !== 'https:' || url.username || url.password) return null;
    const basename = invoiceName(decodeURIComponent(url.pathname.split('/').at(-1)));
    // 下载属性或 PDF 路径是直接文件链接的证据。仅文本相同的预览入口不读取。
    if (!basename && link.download === null) return null;
    const displayNames = [invoiceName(link.label), invoiceName(link.download)].filter(Boolean);
    const name = displayNames[0];
    if (!name || displayNames.some(value => value !== name) || (basename && basename !== name)) return null;
    url.hash = '';
    return { name, href: url.href };
  } catch {
    return null;
  }
}

export async function hydrateVisibleSelectionInvoices(page, sheets) {
  const counts = { eligible: 0, matched: 0, downloaded: 0, unavailable: 0 };
  const selected = new Map();
  for (const sheet of sheets) for (const cell of sheet.cells) {
    if (cell.invoice_link !== true) continue;
    const name = invoiceName(cell.text);
    if (!name) continue;
    const cells = selected.get(name) || [];
    cells.push(cell);
    selected.set(name, cells);
    if (!cell.assets.length) counts.eligible++;
  }
  if (!counts.eligible || page.isClosed()) return counts;
  const documentUrl = page.url();
  let links;
  try {
    if (new URL(documentUrl).origin !== docsOrigin) return counts;
    links = await visibleLinks(page, [...selected.keys()]);
  } catch {
    return counts;
  }
  if (page.url() !== documentUrl) return counts;

  const byName = new Map();
  const byUrl = new Map();
  for (const link of links) {
    const evidence = invoiceLinkEvidence(link);
    if (!evidence || !selected.has(evidence.name)) continue;
    const { name, href } = evidence;
    if (link.col !== null && !selected.get(name).some(cell => String(cell.col) === link.col)) continue;
    if (!byName.has(name)) byName.set(name, new Set());
    if (!byUrl.has(href)) byUrl.set(href, new Set());
    byName.get(name).add(href);
    byUrl.get(href).add(name);
  }

  const candidates = [];
  for (const [name, cells] of selected) {
    const links = byName.get(name);
    if (cells.length !== 1 || cells[0].assets.length || links?.size !== 1) continue;
    const [source] = links;
    if (byUrl.get(source).size !== 1) continue;
    // 下载失败时保留原来的纯文件名，仍可用本地 PDF 补齐。
    candidates.push({ original: cells[0], invoice_link: true, assets: [{ source, base64: null }] });
  }
  counts.matched = candidates.length;
  if (!counts.matched) return counts;

  await hydrateInvoiceLinks(page, [{ cells: candidates }]);
  if (!page.isClosed() && page.url() === documentUrl) {
    for (const cell of candidates) {
      if (cell.assets[0].base64 === null) continue;
      cell.original.assets.push({ source: 'visible_invoice_link', base64: cell.assets[0].base64 });
      counts.downloaded++;
    }
  }
  counts.unavailable = counts.matched - counts.downloaded;
  return counts;
}
