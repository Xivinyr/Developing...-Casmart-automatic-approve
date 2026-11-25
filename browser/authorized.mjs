// 独立 Chromium；手动登录/验证/导出/选区复制。只下载用户操作产生的文件。
import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline/promises';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const [url, python = 'python', config, startCol = 'T'] = process.argv.slice(2);
if (!url || new URL(url).hostname !== 'docs.qq.com') throw Error('请提供 docs.qq.com 表格链接');
const local = path.resolve('.local');
const settings = config ? JSON.parse((await readFile(config,'utf8')).replace(/^\uFEFF/,'')) : {};
await mkdir(local,{recursive:true});
const context = await chromium.launchPersistentContext(path.join(local,'chromium-profile'),{headless:false,acceptDownloads:true});
const page = context.pages()[0] || await context.newPage();
const rl = createInterface({input:process.stdin,output:process.stdout});
const downloads = [];
context.on('page', bind);
bind(page);
function bind(p) {
  p.on('download', d => {
    // 不信任远程文件名，统一随机命名，保留可识别扩展名。
    const suffix = path.extname(d.suggestedFilename()).replace(/[^.a-z0-9]/gi,'');
    const job = d.saveAs(path.join(local,`download-${crypto.randomUUID()}${suffix}`));
    downloads.push(job);
    job.then(()=>console.log('下载已保存在 .local')).catch(()=>console.log('下载失败'));
  });
}
function run(args) {
  const result = spawnSync(python,['-m','attachments',...args],{encoding:'utf8'});
  if (result.status !== 0) throw Error('本地解析失败，请独立运行 Python 入口检查');
}
try {
  await page.goto(url);
  console.log('请手动登录并完成人机验证。可在网页正常导出 XLSX。');
  while (true) {
    const command = await rl.question('完成选区 T:W 并 Ctrl+C 后输入 c；等待下载后 q 退出：');
    if (command.trim() === 'q') break;
    if (command.trim() !== 'c') continue;
    const prepared = path.join(local,'prepared.json');
    run(['clipboard','--start-row',String(settings.start_row || 2),'--start-col',startCol,'--prepare',prepared]);
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
    const result = spawnSync(python,['-m','attachments','snapshot',captured,...(config ? ['--config',config]:[])],{stdio:'inherit'});
    console.log(`未获取资源数：${failures}；分类退出码：${result.status}`);
  }
  await Promise.allSettled(downloads);
} finally { rl.close(); await context.close(); }
