# 【开发中】Casmart报账系统自动送审脚本
正在尝试借助AI开发Casmart报账系统自动送审脚本，减少报账送审时的重复性操作。

## 登录与本机保存账号密码

配置本机入口后，脚本直接打开南方医科大学统一认证。输入学校账号密码并勾选“记住账号密码”可在下次运行时自动填写。验证码、微信或其他验证由本人在浏览器中完成，认证回跳后进入线下单据列表。

首次建议只验证登录：`python songshen.py --login-only`（不会送审）。正常运行：`python songshen.py`。

- [登录步骤、保存位置与验证范围](docs/LOGIN.md)

## 腾讯文档附件下载与分类（独立 MVP）

保留原自动送审功能。新增附件模块默认从导出的 XLSX 离线提取，另提供 Windows HTML 剪贴板 + 独立 Chromium 授权 Fetch 回退，以及 Open API 只读探测入口。

- [使用说明、配置与边界](docs/ATTACHMENTS.md)
- [实验结果与方案比较](docs/EXPERIMENTS.md)
- 快速运行：`python -m attachments xlsx "data/报账表.xlsx" --config config.example.json`
- 测试：`python -m unittest discover -s tests -v`

真实腾讯文档导出、附件质量及 OAuth 权限尚待样例验证；合成测试结果不代表线上成功。请勿将报账数据或浏览器登录状态提交到此公开仓库。

