# 腾讯文档附件方案实验记录

日期：2026-11-25（Asia/Shanghai）。源码仓库：Xivinyr/Developing...-Casmart-automatic-approve；功能分支：`codex/tencent-attachments`。本轮无真实腾讯链接、导出样例或 OAuth 凭证；所有成功结果的范围均为本机合成实验。

## 实验环境及复现

Windows；Python 3.12.14（Codex 随附运行时），Node 使用随附运行时，Playwright 1.62.1，独立 Chromium 151.0.7922.34。默认 Python WindowsApps 别名不可执行，改用随附 Python 绝对路径；Playwright 使用随附 Node 包（NODE_PATH），浏览器安装至被忽略的 `.local/pw-browsers`。

```powershell
python -m unittest discover -s tests -v
$env:PYTHONPATH = (Get-Location).Path
python tests/benchmark.py
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.local/pw-browsers'
node browser/smoke.cjs
node --check browser/authorized.mjs
```

实际执行时 `python` 替换为 `load_workspace_dependencies` 返回的 Python 绝对路径。浏览器首次运行因缺少可执行文件失败；普通安装被本机网络权限拒绝，经允许扩大执行权限后下载官方 Chromium，再运行 localhost smoke 通过。没有修改浏览器依赖目录。

## 本轮输出

单元测试：**9/9 通过**，最近一次 `Ran 9 tests in 0.056s / OK`。

| 验证点 | 结果与证据范围 |
| --- | --- |
| XLSX rels / drawing | 非顺序 sheetA→drawing7；相对 media 关系；oneCellAnchor/twoCellAnchor 起始坐标均通过 |
| 单元格内嵌图 | 合成 cellimages.xml + DISPIMG 名称→关系→图片通过；现代 vm/richData 仅警告 |
| 合并与同名隔离 | V2:V3 为一条记录；同名 V4 独立，共两条；跨类型横向合并不猜测 |
| 保存与去重 | 授权模拟 Fetch 返回 PDF 时保存 4 个文件，记录内重复图片 1 个；跨记录保留相同图片 |
| 图片质量 | 3 个输出 PNG 与 ZIP 内 PNG 的 SHA-256 逐个相同；只证明不重编码，未验证腾讯导出前后原图质量 |
| 发票与错误 | 文件名文字仍标缺失；登录 HTML 拒绝保存；外链未下载有明确错误；日志无签名 URL |
| 重跑 | 独立 run 目录，原文件保留；排他写入 |
| 剪贴板解析 | 中文 CF_HTML 字节偏移、rowspan/colspan、img/br、Data URL 通过；未从真实腾讯网页读取 Windows 剪贴板 |
| 配置 | 唯一表头映射、商品列、缺失表头拒绝、工作表选择通过 |
| API 请求构建 | 固定官方 V3 GET URL、环境认证头及范围限制通过；未做真实 API 请求 |

30 轮小型合成文件（3 行、2 记录），XLSX 解析+分类落盘中位耗时 **3.848 ms**。命令：`python tests/benchmark.py`。未联网获取 PDF；磁盘缓存、夹具大小影响数值，不能外推真实报账表性能。暂未测真实网络速度，其他路线不作速度排名。

命令行端到端验证：`python -m attachments xlsx .local/cli-fixture.xlsx --config config.example.json`，实际保存 3 个图片、1 个外链待获取错误、2 条记录存在缺失，退出码 2（预期）。证据位于本机已忽略的 `outputs/run-20261125-204704-77856e71/manifest.json`；该测试没有模拟 PDF 获取。

Chromium localhost smoke 实际输出：

```json
{"browser":"151.0.7922.34","cookieFetch":"PASS","download":"PASS","unauthorized401":"PASS","scope":"localhost synthetic only"}
```

通过本机 HTTP 服务验证：仅带合成授权 Cookie 的 Fetch 成功、正常链接触发 download 事件并逐字节核对、本机未授权请求返回 401。没有接入腾讯生产网页；未验证真实 UI 菜单、登录态持久化后的再次登录、CORS、缩略图/原图、Blob 有效期或页面选区复制。

## 方案比较及当前决策

| 方案 | 覆盖情况 | 可靠性与速度证据 | 复杂度与局限 |
| --- | --- | --- | --- |
| XLSX ZIP/XML（推荐默认） | drawing 和 DISPIMG 合成验证；合并与多图可映射 | 本地可复现、无登录依赖；小夹具中位 3.848 ms | 低到中；只能取导出保留资源，vm/richData、OLE 尚未支持；PDF 链接不等于内嵌文件 |
| 官方 Open API（保留候选） | 核实普通表格 V3 范围读取；只读探测入口 | 官方接口、OAuth 明确；真实可用性与速度未测 | 中到高；需应用权限与用户授权，普通 CellValue 文档未列图片/文件字段，不能保证完整附件 |
| Playwright 下载（回退第一步） | Chromium 本机 download + Cookie Fetch 通过 | 仅证明正常授权机制及落盘可行 | 中；真实腾讯 UI 待适配，当前手动导出；页面/跨域规则可能变化 |
| Ctrl+C / CF_HTML + 浏览器 Fetch（回退第二步） | HTML 合成合并、图片及链接解析；Windows读取入口完成 | 本地解析通过；线上复制载荷及图片质量未知 | 中到高；依赖复制格式、手动选区、当前登录页面、CORS 和资源有效期 |

推荐：先正常导出 XLSX，离线提取并复核缺失清单；缺失时尝试浏览器正常下载或剪贴板 Fetch。API 暂不作为默认。各方案尚无真实在线附件总数作为分母，因此**不报告线上覆盖率百分比**。

## 待用户样例与下一轮验收

- 脱敏普通在线表格链接/导出 XLSX，至少含 T/U/V/W、多图、纵向合并、同名不同订单、纯文字发票及实际 PDF 链接。
- 用户在独立 Chromium 手动登录/验证；无需提供密码或旧浏览器配置。
- 对照真实附件清单逐项核对数量、坐标、订单归属、缺失与 PDF；比较网页原文件及导出图片的字节/尺寸，必要时目视检查清晰度。
- 如选择 API，用户在平台完成应用权限及 OAuth 授权；核验真实 fileId、sheetId、范围响应与图片/附件字段。不要在聊天发送令牌。
- 获得真实结构后，再决定实现 richData、OLE、自动导出菜单或其他格式；当前线上验收待完成。

## 来源

- 本地只读来源：课题主项目 AGENTS.md；Obsidian Projects_Manager/AGENTS.md 与 System/维护约定；原飞书 `demo_feishu_download_selenium_merged_cells_backup.py`（商品识别、合并、哈希规则）。未读取 .feishu-edge-profile。
- [腾讯官方 V3 范围接口](https://docs.qq.com/open/document/app/openapi/v3/sheet/get/get_range.html)：GET 路径、只读 scope、认证头、范围限制。
- [腾讯官方普通表格资源模型](https://docs.qq.com/open/document/app/openapi/v3/sheet/model/spreadsheet.html)：区分普通表格与智能表；CellValue 未列图片或文件类型，附件能力仍待核验。
- [腾讯官方应用权限](https://docs.qq.com/open/document/app/scope.html)：应用权限、审核及权限变更后重新授权。
- [腾讯官方 OAuth 与接口介绍](https://docs.qq.com/open/document/app)：授权前提；未将智能表或收集表附件能力套用于普通表格。
- [Playwright 官方认证说明](https://playwright.dev/python/docs/auth)：独立上下文与本地认证状态，禁止提交登录状态。
- [susu5/tencent-sheet-images 技术参考](https://github.com/susu5/tencent-sheet-images/blob/main/references/xlsx_image_format.md)：仅参考腾讯导出布局及 ZIP/XML 路线；未复制第三方实现，也未将其说明视为本项目实测。
