import argparse
import base64
from collections import Counter
import json
from pathlib import Path
from .core import Asset, Cell, Sheet, classify, column, invoice_link_cells
from .xlsx import read_xlsx
from .clipboard import ClipboardInputError, clipboard_sequence, read_html, read_windows_html


CLIPBOARD_MESSAGES = {
    'clipboard_busy': '剪贴板暂时被占用，请回到表格重新复制后再试。',
    'clipboard_has_no_html': '剪贴板没有表格格式。请在腾讯表格中选中单元格并按 Ctrl+C，再回到任务窗口。',
    'clipboard_lock_failed': '无法读取剪贴板，请重新复制选区。',
    'clipboard_sequence_unavailable': '无法确认剪贴板更新，请在网页手动复制选区后输入 c。',
    'clipboard_not_updated': '没有收到本次自动复制的新内容，请在网页手动复制选区后输入 c。',
    'clipboard_range_mismatch': '收到的表格范围与自动选区不一致，请在网页手动复制选区后输入 c。',
    'clipboard_empty_selection': '自动复制的范围没有检测到文字或附件。',
    'invalid_cf_html': '读到的剪贴板表格片段不完整，请重新复制选区或导出 XLSX。',
    'clipboard_no_table': '复制内容没有可读取的表格单元格，请重新选中 T 到 W 列的数据行或导出 XLSX。',
    'clipboard_invalid_span': '复制内容含无法识别的合并单元格，请缩小选区或导出 XLSX。',
    'clipboard_invalid_attachment': '复制内容的内嵌附件格式异常，请重新复制选区或导出 XLSX。',
    'clipboard_not_supported': '当前系统无法读取 Windows 表格剪贴板，请导出 XLSX。',
}

REVIEW_HINTS = {
    'invoice_filename_only': '表格只提供了发票 PDF 文件名。请从原表下载对应的 PDF，再补齐。',
    'invoice_not_in_folder': '所选文件夹里缺少同名 PDF。请确认下载完成并保留原文件名。',
    'invoice_filename_ambiguous': '发现重名或一条记录对应多个文件名，请人工核对发票归属。',
    'invoice_local_invalid': '部分同名文件无法读取，或并非有效的 PDF 文件内容。',
    'invoice_folder_unavailable': '无法读取指定的发票文件夹，请选择 PDF 所在的本地目录。',
    'resource_unavailable': '部分附件链接无法获取或文件内容无法识别，请查看清单。',
}


def report_summary(root, report, human=False):
    records = report['records']
    saved = Counter(file['category'] for record in records for file in record['files'])
    missing = Counter(label for record in records for label in record['missing'])
    reasons = Counter(reason for record in records for reason in record['missing_reasons'].values())
    gaps = sum(bool(record['missing']) for record in records)
    if not human:
        print(json.dumps({
            'output':str(root), 'saved':report['saved'], 'errors':len(report['errors']), 'missing_records':gaps,
            'records':len(records), 'saved_by_category':saved, 'missing_by_category':missing, 'missing_by_reason':reasons,
        }, ensure_ascii=False))
        return
    if not records:
        print('未识别到报账记录。请检查选区是否从 T 列开始并包含数据行。')
    else:
        print(f"已保存 {report['saved']} 个附件，{gaps}/{len(records)} 条记录有缺项。")
    if saved:
        print('已保存：' + '，'.join(f'{label} {count}' for label, count in saved.items()))
    if missing:
        print('缺少：' + '，'.join(f'{label} {count}' for label, count in missing.items()))
    for reason, hint in REVIEW_HINTS.items():
        if reason in reasons:
            print(hint)
    if report['errors'] or report['warnings']:
        print(f"另有 {len(report['errors'])} 项处理问题、{len(report['warnings'])} 项格式提示，详见清单。")
    print(f'结果目录：{root}')


def snapshot(sheets, *, invoice_config=None, include_filenames=False):
    result = []
    for sheet in sheets:
        eligible = set() if invoice_config is None else invoice_link_cells(
            sheet, invoice_config, include_filenames=include_filenames,
        )
        cells = []
        for (row, col), value in sheet.cells.items():
            assets = [
                {'source': asset.source,
                 'base64': base64.b64encode(asset.data).decode() if asset.data is not None else None}
                for asset in value.assets
            ]
            cell = {'row': row, 'col': col, 'text': value.text, 'assets': assets}
            if (row, col) in eligible:
                cell['invoice_link'] = True
            cells.append(cell)
        result.append({'name': sheet.name, 'merges': sheet.merges, 'warnings': sheet.warnings, 'cells': cells})
    return result


def from_snapshot(data):
    sheets = []
    for entry in data:
        s = Sheet(entry['name'],merges=entry.get('merges',[]),warnings=entry.get('warnings',[]))
        for c in entry['cells']:
            s.cells[c['row'],c['col']] = Cell(c['text'],[Asset(a['source'],base64.b64decode(a['base64'],validate=True) if a.get('base64') is not None else None) for a in c['assets']])
        sheets.append(s)
    return sheets


def main():
    p = argparse.ArgumentParser(description='报账附件提取；不操作送审')
    p.add_argument('mode', choices=['xlsx','html','clipboard','clipboard-sequence','snapshot'])
    p.add_argument('input', nargs='?')
    p.add_argument('--config')
    p.add_argument('--output', default='outputs')
    p.add_argument('--start-row', type=int, default=2)
    p.add_argument('--start-col', default='T')
    p.add_argument('--after-sequence', type=int)
    p.add_argument('--expected-end-row', type=int)
    p.add_argument('--expected-end-col')
    p.add_argument('--trim-trailing-empty', action='store_true')
    p.add_argument('--invoice-dir', help='已下载发票 PDF 所在文件夹，按表格中的唯一同名文件归类')
    p.add_argument('--human', action='store_true', help='显示便于阅读的结果和缺项说明')
    p.add_argument('--prepare', help='仅输出本地中间模型，供已登录浏览器 Fetch 使用')
    p.add_argument('--invoice-links', action='store_true', help='标记配置的发票列中可供在线处理的来源')
    args = p.parse_args()
    automatic_fields = (args.after_sequence, args.expected_end_row, args.expected_end_col)
    expected_end = None
    if args.trim_trailing_empty or any(value is not None for value in automatic_fields):
        if (args.mode != 'clipboard' or not args.trim_trailing_empty
                or any(value is None for value in automatic_fields)):
            p.error('自动复制校验参数必须在 clipboard 模式同时使用')
        if not 1 <= args.after_sequence <= 0xffffffff:
            p.error('剪贴板更新编号无效')
        try:
            expected_end = (args.expected_end_row, column(args.expected_end_col))
        except ValueError:
            p.error('自动选区的结束列无效')
    if args.invoice_links and (args.mode not in ('xlsx', 'clipboard') or not args.prepare):
        p.error('--invoice-links 仅用于带 --prepare 的 xlsx 或 clipboard')
    if args.mode == 'clipboard-sequence':
        print(clipboard_sequence())
        return 0
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.config else {}
    if args.mode == 'xlsx': sheets = read_xlsx(args.input, config.get('sheet'))
    elif args.mode == 'snapshot': sheets = from_snapshot(json.loads(Path(args.input).read_text(encoding='utf-8')))
    else:
        if args.mode == 'clipboard':
            raw = read_windows_html() if args.after_sequence is None else read_windows_html(args.after_sequence)
        else:
            raw = Path(args.input).read_bytes()
        sheets = read_html(raw, args.start_row, column(args.start_col),
                           expected_end=expected_end, trim_trailing_empty=args.trim_trailing_empty)
    if args.prepare:
        dest = Path(args.prepare)
        dest.parent.mkdir(parents=True,exist_ok=True)
        invoice_config = config if args.invoice_links else None
        cells = snapshot(sheets, invoice_config=invoice_config, include_filenames=args.mode == 'clipboard')
        dest.write_text(json.dumps(cells, ensure_ascii=False), encoding='utf-8')
        if args.human:
            print('已读取导出的 XLSX。' if args.mode == 'xlsx' else '已读取表格选区。')
        else:
            print('Prepared local snapshot')
    else:
        root, report = classify(sheets, args.output, config, invoice_dir=args.invoice_dir)
        report_summary(root, report, args.human)
        return 2 if report['errors'] or report['warnings'] or not report['records'] or any(r['missing'] for r in report['records']) else 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except ClipboardInputError as exc:
        print(CLIPBOARD_MESSAGES.get(exc.code, '无法读取表格选区，请重新复制或导出 XLSX。'))
        raise SystemExit(2)
    except Exception as exc:
        # 原始异常可能含有 URL / 文档内容，终端只给出类型。
        print('提取失败：' + type(exc).__name__ + '；请检查输入格式、配置及文件权限。')
        raise SystemExit(2)
