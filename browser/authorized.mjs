// 浏览器使用单独的附件任务配置；手动登录、导出和复制选区。
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createInterface } from 'node:readline/promises';
import { processExport, runAttachments, saveInvoiceDownload } from './export_xlsx.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const [url, python = 'python', config, startCol = 'T'] = process.argv.slice(2);
let sheetUrl;
try { sheetUrl = new URL(url); } catch { /* 下方统一提示地址无效。 */ }
if (!sheetUrl || sheetUrl.origin !== 'https://docs.qq.com' || sheetUrl.username || sheetUrl.password) {
  console.error('请提供 https://docs.qq.com 的腾讯文档表格链接。');
  process.exit(2);
}
const local = path.resolve('.local');
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
  console.log('请在浏览器登录并完成验证。导出 XLSX 后会自动分类，并尝试下载表格保留的发票链接。');
  while (!closing.signal.aborted) {
    const command = (await rl.question('复制表格选区后输入 c；下载发票后输入 p；退出输入 q：', { signal: closing.signal })).trim();
    if (command === 'q') break;
    if (command === 'p') {
      const capture = lastCapture || lastExport;
      if (!capture) {
        console.log('请先导出 XLSX，或复制表格数据行后输入 c。程序需要表格中的发票文件名匹配。');
        continue;
      }
      const input = (await rl.question('直接回车使用本任务下载的 PDF，或粘贴 PDF 所在文件夹路径：', { signal: closing.signal })).trim();
      const folder = input.replace(/^"(.*)"$/, '$1');
      await pendingInvoices;
      if (!folder && invoiceConflict) {
        console.log('请核对内容不同的同名发票，再输入 p 并选择已确认的文件夹。');
        continue;
      }
      const code = await runAttachments(python, ['snapshot', capture, ...configArgs, '--invoice-dir', folder || invoiceDir, '--human']);
      if (capture === lastCapture) clipboardNeedsReview = code !== 0;
      else exportNeedsReview = code !== 0;
      continue;
    }
    if (command !== 'c') continue;
    lastCapture = null;
    const prepared = path.join(local,'prepared.json');
    const preparedCode = await runAttachments(python, ['clipboard','--start-row',String(settings.start_row || 2),'--start-col',startCol,'--prepare',prepared,'--human']);
    if (preparedCode !== 0) {
      clipboardNeedsReview = true;
      continue;
    }
    const sheets = JSON.parse(await readFile(prepared,'utf8'));
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
    await writeFile(captured,JSON.stringify(sheets));
    lastCapture = captured;
    const code = await runAttachments(python, ['snapshot', captured, ...configArgs, '--human']);
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
