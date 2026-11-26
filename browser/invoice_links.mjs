const docsOrigin = 'https://docs.qq.com';
const maxPdfBytes = 30 * 1024 * 1024;
const maxTotalBytes = 120 * 1024 * 1024;
const maxRequests = 100;
const totalMillis = 120_000;

function docsLink(source) {
  try {
    // XLSX 只接受真正指向腾讯文档的绝对链接。文件名、预览文字和内网地址不参与。
    const url = new URL(source);
    const onDocs = url.hostname === 'docs.qq.com' || url.hostname.endsWith('.docs.qq.com');
    if (onDocs && url.protocol === 'https:' && !url.port && !url.username && !url.password) {
      url.hash = '';
      return url.href;
    }
  } catch {
    // 解析失败的来源保留为清单缺项。
  }
  return null;
}

async function fetchPdf(page, url, maxBytes, timeout) {
  return page.evaluate(async ({ url, maxBytes, timeout, origin }) => {
    if (window.location.origin !== origin) throw Error('unexpected_page');
    const response = await fetch(url, {
      credentials: 'same-origin', redirect: 'error', signal: AbortSignal.timeout(timeout),
    });
    if (!response.ok || !response.body || Number(response.headers.get('Content-Length')) > maxBytes) {
      throw Error('pdf_unavailable');
    }
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > maxBytes) throw Error('pdf_too_large');
        chunks.push(value);
      }
    } catch (error) {
      await reader.cancel().catch(() => {});
      throw error;
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    if (String.fromCharCode(...bytes.subarray(0, 5)) !== '%PDF-') throw Error('not_pdf');
    let binary = '';
    for (let start = 0; start < bytes.length; start += 8192) {
      binary += String.fromCharCode(...bytes.subarray(start, start + 8192));
    }
    return { base64: btoa(binary), size };
  }, { url, maxBytes, timeout, origin: docsOrigin });
}

export async function hydrateInvoiceLinks(page, sheets) {
  const result = { downloaded: 0, unavailable: 0 };
  const fetched = new Map();
  const deadline = Date.now() + totalMillis;
  let left = maxTotalBytes;
  for (const sheet of sheets) for (const cell of sheet.cells) {
    if (cell.invoice_link !== true) continue;
    for (const asset of cell.assets) {
      if (asset.base64 !== null) continue;
      const url = docsLink(asset.source);
      const maxBytes = Math.min(maxPdfBytes, left);
      const timeout = Math.min(12_000, deadline - Date.now());
      if (!url || page.isClosed()) {
        result.unavailable++;
        continue;
      }
      if (!fetched.has(url) && fetched.size < maxRequests && maxBytes > 0 && timeout > 0) {
        try {
          const pdf = await fetchPdf(page, url, maxBytes, timeout);
          fetched.set(url, pdf);
        } catch {
          fetched.set(url, null);
        }
      }
      const pdf = fetched.get(url);
      if (pdf && pdf.size <= left) {
        asset.base64 = pdf.base64;
        left -= pdf.size;
        result.downloaded++;
      } else {
        result.unavailable++;
      }
    }
  }
  return result;
}
