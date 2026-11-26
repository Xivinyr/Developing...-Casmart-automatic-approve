// 浏览器使用单独的附件任务配置；手动登录、导出和复制选区。
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createInterface } from 'node:readline/promises';
import { processExport, runAttachments } from './export_xlsx.mjs';
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
let needsReview = false;
context.on('close', () => closing.abort());
rl.on('close', () => closing.abort());
context.on('page', bind);
bind(page);
function bind(p) {
  p.on('download', d => {
    const job = processExport(d, { local, python, config }).then(code => {
      if (code !== 0) {
        needsReview = true;
        console.log('导出表格处理需复核，请查看上方结果或检查表格格式。');
      }
    }).catch(() => {
      needsReview = true;
      console.log('导出下载或本地分类失败。');
    });
    downloads.add(job);
    void job.finally(() => downloads.delete(job));
  });
}
try {
  await page.goto(sheetUrl.href, { waitUntil: 'domcontentloaded' });
  console.log('请在浏览器登录并完成验证。在表格菜单导出 XLSX 后会自动提取并分类。');
  while (!closing.signal.aborted) {
    const command = await rl.question('也可在表格中复制所选区域后输入 c；处理完成后输入 q 退出：', { signal: closing.signal });
    if (command.trim() === 'q') break;
    if (command.trim() !== 'c') continue;
    const prepared = path.join(local,'prepared.json');
    const preparedCode = await runAttachments(python, ['clipboard','--start-row',String(settings.start_row || 2),'--start-col',startCol,'--prepare',prepared]);
    if (preparedCode !== 0) {
      needsReview = true;
      console.log('未能读取表格选区，请重新复制后再试。');
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
    const code = await runAttachments(python, ['snapshot',captured,...(config ? ['--config',config]:[])]);
    needsReview ||= failures > 0 || code !== 0;
    console.log(`未获取资源数：${failures}；分类退出码：${code}`);
  }
} catch {
  if (!closing.signal.aborted) {
    needsReview = true;
    console.error('表格访问或附件处理失败，请检查链接、网络及输出目录。');
  }
} finally {
  await Promise.allSettled(downloads);
  rl.close();
  await context.close();
  if (needsReview) process.exitCode = 2;
}
