"""官方 V3 只读范围探测；响应留在 .local，不宣称附件支持。"""
import json
import os
import re
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import quote


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def request_spec(file_id, sheet_id, cell_range, environ=None):
    env = os.environ if environ is None else environ
    keys = {'Access-Token':'TENCENT_ACCESS_TOKEN','Open-Id':'TENCENT_OPEN_ID','Client-Id':'TENCENT_CLIENT_ID'}
    if any(not env.get(v) for v in keys.values()): raise ValueError('missing_oauth_environment')
    if not re.fullmatch(r'[A-Za-z]+\d+:[A-Za-z]+\d+',cell_range): raise ValueError('Invalid A1 range')
    from .core import coordinate
    a,b = map(coordinate, cell_range.split(':'))
    rows, cols = b[0]-a[0]+1,b[1]-a[1]+1
    if rows < 1 or cols < 1 or rows > 1000 or cols > 200 or rows*cols > 10000: raise ValueError('range_limit')
    url = 'https://docs.qq.com/openapi/spreadsheet/v3/files/' + '/'.join(quote(x,safe='') for x in (file_id,sheet_id,cell_range))
    return url, {k:env[v] for k,v in keys.items()}


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='只读 Open API 探测；凭证仅从环境变量读取')
    p.add_argument('file_id'); p.add_argument('sheet_id'); p.add_argument('range')
    a = p.parse_args()
    try:
        url, headers = request_spec(a.file_id,a.sheet_id,a.range)
        with build_opener(NoRedirect).open(Request(url,headers=headers),timeout=30) as response:
            data = json.load(response)
        dest = Path('.local/openapi-response.json')
        dest.parent.mkdir(exist_ok=True)
        dest.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        print('响应已保存至 .local；请核对业务码、gridData、图片/附件字段，不代表提取成功。')
    except Exception as exc:
        print('Open API 探测未完成：' + type(exc).__name__)
        raise SystemExit(2)
