// 浏览器使用单独的附件任务配置；手动登录、导出和复制选区。
import { createRequire } from 'node:module';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createInterface } from 'node:readline/promises';
import { processExport, runAttachments, saveInvoiceDownload } from './export_xlsx.mjs';
import { hydrateVisibleSelectionInvoices } from './selection_invoices.mjs';
import { downloadVisibleSelectionInvoices } from './selection_download.mjs';
import { copyCurrentSheetRange } from './auto_selection.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const launchArgs = process.argv.slice(2);
const selectionOnly = launchArgs.includes('--selection-only');
const [url, python = 'python', config, startCol = 'T', batchName = ''] = launchArgs.filter(arg => arg !== '--selection-only');
let sheetUrl;
try { sheetUrl = new URL(url); } catch { /* 下方统一提示地址无效。 */ }
if (!sheetUrl || sheetUrl.origin !== 'https://docs.qq.com' || sheetUrl.username || sheetUrl.password) {
  console.error('请提供 https://docs.qq.com 的腾讯文档表格链接。');
  process.exit(2);
}
const local = path.resolve('.local');
// 批次名只决定 outputs 下的子目录；前缀与净化后的单段名称避免路径穿越。
const batch = batchName.trim().replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 40).replace(/^[. ]+|[. ]+$/g, '');
const selectionOutput = batch ? path.join('outputs', '批次-' + batch) : 'outputs';
const settings = config ? JSON.parse((await readFile(config,'utf8')).replace(/^\uFEFF/,'')) : {};
await mkdir(local,{recursive:true});
const useEdge = process.platform === 'win32' && !existsSync(chromium.executablePath());
const profile = path.join(local, useEdge ? 'edge-attachments-profile' : 'chromium-profile');
let context;
try {
  context = await chromium.launchPersistentContext(profile, {
    headless: false, acceptDownloads: true, ...(useEdge ? { channel: 'msedge' } : {}),
  });
} catch {
  console.error('无法打开附件浏览器。请确认 Edge 或 Chromium 已安装，并关闭重复打开的附件窗口。');
  process.exit(2);
}
const page = context.pages()[0] || await context.newPage();
const rl = createInterface({input:process.stdin,output:process.stdout});
const downloads = new Set();
const closing = new AbortController();
const invoiceDir = path.join(local, 'invoice-downloads', crypto.randomUUID());
const configArgs = config ? ['--config', config] : [];
const maxAutoRows = 1000;
const execFileAsync = promisify(execFile);
const autoSelectionHints = {
  sheet_unavailable: '请先在附件浏览器中打开当月工作表并确认已加载。',
  sheet_changed: '选区期间工作表发生变化，请回到目标工作表后重试。',
  name_box_unavailable: '当前网页没有可确认的单元格地址框。',
  last_row_unverified: '无法确认工作表末行。若已知行号，可输入 a 加行号，例如 a 35。',
  range_too_large: '自动选区超出 1000 行限制。',
  range_unverified: '网页没有确认选中了预期范围。',
  clipboard_guard_failed: '无法确认剪贴板可以接收本次复制。',
};
let pendingInvoices = Promise.resolve();
let invoiceConflict = false;
let lastCapture = null;
let lastExport = null;
let clipboardNeedsReview = false;
let exportNeedsReview = false;
context.on('close', () => closing.abort());
rl.on('close', () => closing.abort());
context.on('page', bind);
bind(page);

async function clipboardSequence() {
  const { stdout } = await execFileAsync(
    python, ['-m', 'attachments', 'clipboard-sequence'], { encoding: 'utf8', timeout: 5000, windowsHide: true },
  );
  const sequence = stdout.trim();
  if (!/^[1-9][0-9]{0,9}$/.test(sequence)) throw Error('clipboard_sequence_unavailable');
  return sequence;
}

function bind(p) {
  p.on('download', d => {
    if (/\.pdf$/i.test(d.suggestedFilename())) {
      // 串行写入同一任务的 PDF，避免同名的重复下载还未写完就被判为冲突。
      pendingInvoices = pendingInvoices.then(() => saveInvoiceDownload(d, invoiceDir)).then(status => {
        if (status === 'saved' || status === 'already_saved') {
          console.log('已收到发票 PDF。下载完毕后输入 p 归类。');
        } else if (status === 'name_conflict') {
          invoiceConflict = true;
          console.log('收到内容不同的同名 PDF，请核对并选择已确认的发票文件夹。');
        } else {
          console.log('下载内容无法作为 PDF 发票收集，请检查原文件。');
        }
      }).catch(() => console.log('发票 PDF 保存失败，请使用浏览器正常下载后选择本地文件夹。'));
      return;
    }
    if (selectionOnly) {
      console.log('网页选区任务仅处理已复制的选区和发票 PDF。整本工作簿可从主菜单单独处理。');
      return;
    }
    const job = processExport(d, {
      local, python, config, page, onSnapshot: prepared => { lastExport = prepared; },
    }).then(code => {
      if (code !== 0) {
        exportNeedsReview = true;
        console.log('导出表格处理需复核，请查看上方结果或检查表格格式。');
      }
    }).catch(() => {
      exportNeedsReview = true;
      console.log('导出下载或本地分类失败。');
    });
    downloads.add(job);
    void job.finally(() => downloads.delete(job));
  });
}
try {
  await page.goto(sheetUrl.href, { waitUntil: 'domcontentloaded' });
  if (selectionOnly) {
    console.log('请在网页打开要报账的工作表。输入 a 自动选择并复制 T 到 W 列；手动复制的选区可用 c。');
  } else {
    console.log('请在浏览器登录并完成验证。导出 XLSX 后会自动分类，并尝试下载表格保留的发票链接。');
  }
  while (!closing.signal.aborted) {
    const prompt = selectionOnly
      ? '自动选区 a；从已复制选区处理 c；补齐发票 p；退出 q：'
      : '复制表格选区后输入 c；下载发票后输入 p；退出输入 q：';
    const command = (await rl.question(prompt, { signal: closing.signal })).trim();
    if (command === 'q') break;
    if (command === 'p') {
      const capture = lastCapture || lastExport;
      if (!capture) {
        console.log('请先处理表格选区。程序需要选区中的发票文件名来匹配 PDF。');
        continue;
      }
      const input = (await rl.question('直接回车使用本任务下载的 PDF，或粘贴 PDF 所在文件夹路径：', { signal: closing.signal })).trim();
      const folder = input.replace(/^"(.*)"$/, '$1');
      await pendingInvoices;
      if (!folder && invoiceConflict) {
        console.log('请核对内容不同的同名发票，再输入 p 并选择已确认的文件夹。');
        continue;
      }
      const output = capture === lastCapture ? selectionOutput : 'outputs';
      const code = await runAttachments(python, ['snapshot', capture, ...configArgs, '--output', output, '--invoice-dir', folder || invoiceDir, '--human']);
      if (capture === lastCapture) clipboardNeedsReview = code !== 0;
      else exportNeedsReview = code !== 0;
      continue;
    }
    const autoCommand = selectionOnly && /^a(?:\s+([1-9][0-9]{0,6}))?$/.exec(command);
    if (command !== 'c' && !autoCommand) continue;
    lastCapture = null;
    const prepared = path.join(local,'prepared.json');
    const selectionArgs = selectionOnly ? [...configArgs, '--invoice-links'] : [];
    const startRow = settings.start_row ?? 2;
    let autoRange;
    const copyArgs = [];
    if (autoCommand) {
      try {
        const endRow = autoCommand[1] ? Number(autoCommand[1]) : settings.end_row ?? undefined;
        if (startCol !== 'T' || (settings.end_row != null && endRow > settings.end_row)) {
          throw Error('unsupported_auto_range');
        }
        console.log('正在确认当前工作表的 T 到 W 列范围。');
        autoRange = await copyCurrentSheetRange(page, {
          startRow, endRow, startCol, endCol: 'W', maxRows: maxAutoRows, beforeCopy: clipboardSequence,
        });
        if (!autoRange.sequence) throw Error('clipboard_sequence_unavailable');
        console.log(`已在当前工作表选中并复制 ${autoRange.range}，正在核对剪贴板。`);
        copyArgs.push('--after-sequence', autoRange.sequence,
          '--expected-end-row', String(autoRange.endRow), '--expected-end-col', 'W', '--trim-trailing-empty');
      } catch (error) {
        clipboardNeedsReview = true;
        const hint = autoSelectionHints[error.code];
        if (hint) console.log(hint);
        console.log('已停止本次自动处理。可手动复制 T 到 W 列后输入 c。');
        continue;
      }
    }
    const preparedCode = await runAttachments(python, ['clipboard', '--start-row', String(startRow),
      '--start-col', startCol, '--prepare', prepared, '--human', ...selectionArgs, ...copyArgs]);
    if (preparedCode !== 0) {
      clipboardNeedsReview = true;
      continue;
    }
    const sheets = JSON.parse(await readFile(prepared,'utf8'));
    if (autoRange) {
      const lastRow = sheets.flatMap(sheet => sheet.cells).reduce((last, cell) => Math.max(last, cell.row), 0);
      if (lastRow < startRow) {
        clipboardNeedsReview = true;
        console.log('本次选区没有可处理的非空行。');
        continue;
      }
      console.log(`选区中最后一个非空行是第 ${lastRow} 行，开始分类。`);
    }
    if (selectionOnly) console.log('已触发自动检查，正在核对本次选区的发票链接和下载控件。');
    let failures = 0;
    for (const s of sheets) for (const c of s.cells) for (const a of c.assets) {
      if (a.base64 !== null) continue;
      try {
        const parsed = new URL(a.source, page.url());
        if (!['https:','blob:'].includes(parsed.protocol)) throw Error('scheme');
        // 浏览器原生 Fetch 遵循 CORS 和既有登录授权；不注入认证头，不绕过验证。
        a.base64 = await page.evaluate(async url => {
          const r = await fetch(url,{credentials:'include',signal:AbortSignal.timeout(30000)});
          if (!r.ok) throw Error('download_failed');
          const bytes = new Uint8Array(await r.arrayBuffer());
          if (bytes.length > 30*1024*1024) throw Error('too_large');
          let text = ''; for (let i=0;i<bytes.length;i+=8192) text += String.fromCharCode(...bytes.subarray(i,i+8192));
          return btoa(text);
        }, parsed.href);
      } catch { failures++; }
    }
    const captured = path.join(local,`capture-${crypto.randomUUID()}.json`);
    if (selectionOnly) {
      for (const sheet of sheets) sheet.name = '网页选区';
      const links = await hydrateVisibleSelectionInvoices(page, sheets);
      if (links.eligible) {
        const native = await downloadVisibleSelectionInvoices(page, sheets);
        console.log(
          `发票检查完成。待匹配 ${links.eligible} 份，找到 ${links.matched} 个直链（取得 ${links.downloaded} 份），`
          + `尝试网页下载 ${native.attempted} 次（收到 ${native.downloaded} 个下载事件）。`,
        );
        if (!links.matched && !native.attempted) {
          console.log('网页未提供可确认的发票下载入口，已识别的图片仍会分类。');
        }
      } else {
        console.log('发票检查完成。选区里没有待按文件名匹配的 PDF。');
      }
      await pendingInvoices;
    }
    await writeFile(captured,JSON.stringify(sheets));
    lastCapture = captured;
    const invoiceArgs = selectionOnly && !invoiceConflict && existsSync(invoiceDir) ? ['--invoice-dir', invoiceDir] : [];
    const code = await runAttachments(python, ['snapshot', captured, ...configArgs, '--output', selectionOutput, ...invoiceArgs, '--human']);
    clipboardNeedsReview = failures > 0 || code !== 0;
    if (failures) console.log(`另有 ${failures} 个显式附件链接未获取。`);
  }
} catch {
  if (!closing.signal.aborted) {
    exportNeedsReview = true;
    console.error('表格访问或附件处理失败，请检查链接、网络及输出目录。');
  }
} finally {
  await Promise.allSettled([...downloads, pendingInvoices]);
  rl.close();
  await context.close();
  if (clipboardNeedsReview || exportNeedsReview) process.exitCode = 2;
}
