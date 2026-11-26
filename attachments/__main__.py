import argparse
import base64
from collections import Counter
import json
from pathlib import Path
from .core import Asset, Cell, Sheet, classify, column
from .xlsx import read_xlsx
from .clipboard import ClipboardInputError, read_html, read_windows_html


CLIPBOARD_MESSAGES = {
    'clipboard_busy': '剪贴板暂时被占用，请回到表格重新复制后再试。',
    'clipboard_has_no_html': '剪贴板没有表格格式。请在腾讯表格中选中单元格并按 Ctrl+C，再回到任务窗口。',
    'clipboard_lock_failed': '无法读取剪贴板，请重新复制选区。',
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


def snapshot(sheets):
    return [{'name':s.name,'merges':s.merges,'warnings':s.warnings,'cells':[
        {'row':r,'col':c,'text':v.text,'assets':[{'source':a.source,'base64':base64.b64encode(a.data).decode() if a.data is not None else None} for a in v.assets]}
        for (r,c),v in s.cells.items()]} for s in sheets]


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
    p.add_argument('mode', choices=['xlsx','html','clipboard','snapshot'])
    p.add_argument('input', nargs='?')
    p.add_argument('--config')
    p.add_argument('--output', default='outputs')
    p.add_argument('--start-row', type=int, default=2)
    p.add_argument('--start-col', default='T')
    p.add_argument('--invoice-dir', help='已下载发票 PDF 所在文件夹，按表格中的唯一同名文件归类')
    p.add_argument('--human', action='store_true', help='显示便于阅读的结果和缺项说明')
    p.add_argument('--prepare', help='仅输出本地中间模型，供已登录浏览器 Fetch 使用')
    args = p.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.config else {}
    if args.mode == 'xlsx': sheets = read_xlsx(args.input, config.get('sheet'))
    elif args.mode == 'snapshot': sheets = from_snapshot(json.loads(Path(args.input).read_text(encoding='utf-8')))
    else:
        raw = read_windows_html() if args.mode == 'clipboard' else Path(args.input).read_bytes()
        sheets = read_html(raw, args.start_row, column(args.start_col))
    if args.prepare:
        dest = Path(args.prepare)
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_text(json.dumps(snapshot(sheets),ensure_ascii=False),encoding='utf-8')
        print('已读取表格选区。' if args.human else 'Prepared local snapshot')
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
