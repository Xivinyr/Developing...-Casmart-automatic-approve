# 腾讯文档附件模块使用说明

2026-11-25：可运行 MVP。原 `songshen.py` 保留，模块不导入或调用送审代码。旧飞书两个脚本原地保留，未复制私有仓库或浏览器配置。业务规则依据原合并单元格版本的 T/U/V/W、商品识别、合并与哈希去重逻辑独立实现。

## 快速运行

需要 Python 3.10+；离线入口无需安装第三方 Python 包。当前 Work 环境的 Python 命令别名不可用，验证使用 Codex 随附 Python 3.12.14。

```powershell
python -m attachments xlsx "data/报账表.xlsx" --config config.example.json
python -m unittest discover -s tests -v
```

从腾讯文档普通在线表格通过正常菜单导出 XLSX，放入已忽略的 `data/`。输出位于 `outputs/run-日期-随机标识/`，分为工作表、记录起始行、商品、附件类型；`manifest.json` 保存哈希、字节数、分类、缺失项，`errors.jsonl` 保存脱敏错误。退出码 0 表示无缺失、错误或格式警告；2 表示需复核或输入失败。必须人工检查清单与图片。

T=订单截图、U=支付记录、V=发票、W=开箱实物图。V 的纯文件名只识别商品，不算已取得发票。PDF 只有实际取得 `%PDF-` 开头的字节才保存；当前是文件签名检查，不是完整 PDF 有效性校验。

配置支持 `sheet`、`header_row`、`start_row`、`end_row`、`product_column`、`record_column`、`columns`、`headers`、`required`。`headers` 非空时替代固定列映射，格式是 `{"订单截图":"实际表头"}`，要求表头唯一；可指定部分附件列。`required` 单独指定必需类型。默认开始第 2 行，结束于最后有内容行。

建议用明确商品列识别名称。未指定时复用旧脚本的「发票文件名去扩展名及末尾金额」规则，含末尾数字的商品可能误识别，须配置商品列。不同起始行同名商品始终分开。只有记录列（优先）、商品列或发票列的显式纵向合并才归入同一记录；不按相同文字自动合并，未合并的跨行同订单需先人工整理。跨类型横向合并不自动猜分类，记录警告及缺失。

去重范围为同记录同类型；跨类型、跨记录保留相同字节，避免丢失业务归属。每次运行独立目录，文件以排他模式创建，无自动覆盖。

## Chromium 与剪贴板回退

需 Node.js 20+，使用 Playwright 独立 Chromium，不依赖 Edge。首次安装：

```powershell
npm install
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.local/pw-browsers'
npx playwright install chromium
node browser/authorized.mjs "https://docs.qq.com/sheet/你的表格" "python" "config.example.json" "T"
```

第二参数可填写 Python 可执行文件绝对路径。第四参数是选区左上列，复制行号由配置 `start_row` 提供。请选中配置对应的实际区域，不包含表头。手动登录/验证码后，正常菜单导出文件将保存至 `.local/download-随机标识.xlsx`；再运行离线入口。UI 菜单差异尚未针对真实表格自动定位。

选区 Ctrl+C 后在终端输入 `c`，读取 Windows `HTML Format`，解析 rowspan/colspan、img src、a href，使用当前表格页面正常 Fetch 获取资源，再交给相同分类模块。Fetch 遵循 CORS 与服务端权限；失败如实记录，不绕过验证，不扫描私有网络接口。可多次复制，最后输入 `q`。原站若只复制 canvas/私有格式且无 HTML，会明确失败。

本地登录状态只在 `.local/chromium-profile/`；未操作日常浏览器配置。快照可能包含原始链接或签名，仅在 `.local` 保存，不应分享或提交。Blob URL 有效期受页面限制，跨域资源可能被 CORS 阻止。若图片只是缩略图，当前 Fetch 保存的也只是该资源，不能宣称原图。

单独离线验证 HTML：`python -m attachments html .local/selection.html --start-row 2 --start-col T`；Windows 剪贴板入口：`python -m attachments clipboard`。脱离浏览器时外链不会自动联网，缺失资源记录 `online_fetch_required`。

XLSX 中的显式超链接也可通过 `--prepare .local/prepared.json` 导出候选模型；当前浏览器交互入口只串联剪贴板模型，尚未提供任意网络链接批量下载界面。不要误认为 XLSX 内所有 PDF 链接已经下载。

## Open API 只读探测

官方范围接口：`GET /openapi/spreadsheet/v3/files/{fileId}/{sheetId}/{range}`；需要 `scope.sheet.readonly` 或 `scope.sheet`。请求头为 Access-Token、Open-Id、Client-Id。OAuth 授权与应用权限需实际配置，网页登录 Cookie 不可替代。

通过本机环境变量设置 `TENCENT_ACCESS_TOKEN`、`TENCENT_OPEN_ID`、`TENCENT_CLIENT_ID`（不要在代码、命令历史或聊天中粘贴真实令牌），然后：

```powershell
python -m attachments.openapi FILE_ID SHEET_ID T2:W100
```

响应仅写 `.local/openapi-response.json`，待人工核对业务返回码、范围、图片/链接字段。该命令不是附件提取成功标志。范围限制：行≤1000、列≤200、总单元格≤10000；禁止认证头重定向转发。未取得授权，本轮不请求私人文档接口，也未实现未经证实的附件 API。

## 目前边界

支持 drawing oneCellAnchor/twoCellAnchor 的起始格映射、共享字符串、inlineStr、合并格、相对/绝对关系路径、同格多图、cellimages.xml + DISPIMG 名称映射。图片原字节保存，不重编码。

Excel 现代 `vm` rich-value 单元格图片仅检测并警告，尚未实现 metadata/richData 关系链；absoluteAnchor 无单元格坐标时也警告，不猜位置。OLE 附件、公式 HYPERLINK、非标准私有图片索引暂不支持。合成测试不代表完整 Excel 文件兼容性，也不代表腾讯导出真实性；低层夹具专为 XML 方言回归构造。

源码仓库公开。真实表格、附件、manifest、快照、浏览器配置、凭证与下载均须留在已忽略目录。未触碰旧 `.feishu-edge-profile`，未清理 Git 历史，未推送。

验证证据与方案比较见 [EXPERIMENTS.md](EXPERIMENTS.md)。
