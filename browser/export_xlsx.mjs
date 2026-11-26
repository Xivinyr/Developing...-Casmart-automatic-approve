import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import path from 'node:path';

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

export async function processExport(download, { local, python, config, output = 'outputs' }) {
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
  return runAttachments(python, [
    'xlsx', destination, '--output', output, ...(config ? ['--config', config] : []),
  ]);
}
