import argparse
import base64
import json
from pathlib import Path
from .core import Asset, Cell, Sheet, classify, column
from .xlsx import read_xlsx
from .clipboard import read_html, read_windows_html


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
        print('Prepared local snapshot')
    else:
        root, report = classify(sheets,args.output,config)
        print(json.dumps({'output':str(root),'saved':report['saved'],'errors':len(report['errors']),'missing_records':sum(bool(r['missing']) for r in report['records'])},ensure_ascii=False))
        return 2 if report['errors'] or report['warnings'] or not report['records'] or any(r['missing'] for r in report['records']) else 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        # 原始异常可能含有 URL / 文档内容，终端只给出类型。
        print('提取失败：' + type(exc).__name__ + '；请检查输入格式、配置及文件权限。')
        raise SystemExit(2)
