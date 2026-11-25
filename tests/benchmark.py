"""本地合成性能记录；不代表腾讯网络速度。"""
import json
import statistics
import tempfile
import time
from pathlib import Path
from test_attachments import fixture, PNG
from attachments.xlsx import read_xlsx
from attachments.clipboard import read_html
from attachments.core import classify

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp)/'fixture.xlsx';fixture(path)
    times = []
    for _ in range(30):
        t = time.perf_counter(); sheets = read_xlsx(path);classify(sheets,Path(tmp)/'out');times.append((time.perf_counter()-t)*1000)
    print(json.dumps({'fixture':'synthetic-3-rows-2-records','runs':30,'xlsx_parse_classify_median_ms':round(statistics.median(times),3),'note':'No Tencent network; original PNG byte hashes covered by unit tests'}))
