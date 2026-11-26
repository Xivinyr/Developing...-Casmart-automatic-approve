# Casmart 报账自动化系统
在项目目录运行主程序：

```powershell
conda activate reimbursement_auto
python main.py
```

在窗口中选择自动送审、仅验证登录、本地 XLSX 附件分类或腾讯表格下载与分类。分类结果可以从窗口中的“打开输出目录”查看。

## 登录与本机保存账号密码

配置本机入口后，脚本直接打开南方医科大学统一认证。输入学校账号密码并勾选“记住账号密码”，下次运行时会在登录表单可输入后自动填写。验证码、微信或其他验证由本人在浏览器中完成，认证回跳后进入线下单据列表。

原命令行入口仍可用，`python songshen.py --login-only` 可单独验证登录。

- [登录步骤、保存位置与验证范围](docs/LOGIN.md)

## 腾讯文档附件下载与分类

可选择本地 XLSX 直接提取。在线任务打开指定腾讯表格，在网页按正常菜单导出 XLSX 后自动接收并分类。Windows 剪贴板可作为外链附件的回退方法。在线任务使用 Node.js、Playwright 与独立配置的 Chromium 或 Edge。

- [使用说明、配置与边界](docs/ATTACHMENTS.md)
- [实验结果与方案比较](docs/EXPERIMENTS.md)
- 快速运行：`python -m attachments xlsx "data/报账表.xlsx" --config config.example.json`
- 测试：`python -m unittest discover -s tests -v`

真实腾讯文档导出、附件质量及 OAuth 权限尚待样例验证；合成测试结果不代表线上成功。请勿将报账数据或浏览器登录状态提交到此公开仓库。

