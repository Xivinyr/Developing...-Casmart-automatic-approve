import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { hydrateInvoiceLinks } from './invoice_links.mjs';

const maxPdfBytes = 30 * 1024 * 1024;

export async function saveInvoiceDownload(download, directory) {
  const name = download.suggestedFilename();
  // 发票使用原文件名匹配，拒绝路径、保留名和异常内容。
  if (!/\.pdf$/i.test(name) || name.length <= 4 || name !== name.trim()
      || /[<>:"/\\|?*\x00-\x1f]/.test(name)
      || /^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])\s*\./i.test(name)) {
    return 'invalid';
  }
  const source = await download.path();
  if ((await stat(source)).size > maxPdfBytes) return 'invalid';
  const bytes = await readFile(source);
  if (bytes.length > maxPdfBytes || !bytes.subarray(0, 5).equals(Buffer.from('%PDF-'))) return 'invalid';
  await mkdir(directory, { recursive: true });
  const destination = path.join(directory, name);
  try {
    await writeFile(destination, bytes, { flag: 'wx' });
    return 'saved';
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    return bytes.equals(await readFile(destination)) ? 'already_saved' : 'name_conflict';
  }
}

export function runAttachments(python, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-m', 'attachments', ...args], {
      stdio: ['ignore', 'inherit', 'inherit'],
      env: { ...process.env, PYTHONUTF8: '1' },
    });
    child.once('error', reject);
    child.once('close', code => resolve(code ?? 1));
  });
}

export async function processExport(download, { local, python, config, output = 'outputs', page, onSnapshot }) {
  // 用随机文件名接收正常导出。只把 XLSX 交给已有离线分类器。
  const extension = path.extname(download.suggestedFilename()).toLowerCase();
  const suffix = /^\.[a-z0-9]{1,12}$/.test(extension) ? extension : '';
  const destination = path.join(local, `download-${randomUUID()}${suffix}`);
  await download.saveAs(destination);
  if (suffix !== '.xlsx') {
    console.log('下载已保存在 .local，当前格式需要手动处理。');
    return 0;
  }

  console.log('已收到 XLSX，正在提取并分类表格附件。');
  const configArgs = config ? ['--config', config] : [];
  const offlineArgs = ['xlsx', destination, '--output', output, ...configArgs];
  if (!page) return runAttachments(python, offlineArgs);

  // 沿用分类器解析 XLSX，从发票列读取表格实际保留的链接。
  const prepared = path.join(local, `export-${randomUUID()}.json`);
  try {
    const code = await runAttachments(python, ['xlsx', destination, ...configArgs, '--prepare', prepared, '--invoice-links', '--human']);
    if (code !== 0) return runAttachments(python, [...offlineArgs, '--human']);
    const sheets = JSON.parse(await readFile(prepared, 'utf8'));
    const { downloaded, unavailable } = await hydrateInvoiceLinks(page, sheets);
    if (downloaded || unavailable) {
      console.log(`从 XLSX 发票链接取得 ${downloaded} 项，仍有 ${unavailable} 项无法读取。`);
      if (downloaded) await writeFile(prepared, JSON.stringify(sheets));
    }
  } catch {
    console.log('未能整理导出里的发票链接，正在直接提取表格内嵌附件。');
    return runAttachments(python, [...offlineArgs, '--human']);
  }
  onSnapshot?.(prepared);
  return runAttachments(python, ['snapshot', prepared, '--output', output, ...configArgs, '--human']);
}
