# Casmart 报账自动化系统
在项目目录运行主程序：

```powershell
conda activate reimbursement_auto
python main.py
```

在窗口中选择自动送审、仅验证登录、腾讯网页选区分类或本地 XLSX 附件分类。分类结果可以从窗口中的“打开输出目录”查看。

## 登录与本机保存账号密码

配置本机入口后，脚本直接打开南方医科大学统一认证。输入学校账号密码并勾选“记住账号密码”，下次运行时会在登录表单可输入后自动填写。验证码、微信或其他验证由本人在浏览器中完成，认证回跳后进入线下单据列表。

原命令行入口仍可用，`python songshen.py --login-only` 可单独验证登录。

- [登录步骤、保存位置与验证范围](docs/LOGIN.md)

## 腾讯文档附件下载与分类

在线任务直接处理从腾讯网页复制的选区。可填月份或批次名，将结果分开放置。若网页能明确识别选区发票的下载入口，程序会尝试补取原始 PDF。手头已有 XLSX 时，也可使用独立的本地分类入口。

- [使用说明、配置与边界](docs/ATTACHMENTS.md)
- [实验结果与方案比较](docs/EXPERIMENTS.md)
- 快速运行：`python -m attachments xlsx "data/报账表.xlsx" --config config.example.json`
- 测试：`python -m unittest discover -s tests -v`

真实腾讯文档导出、附件质量及 OAuth 权限尚待样例验证；合成测试结果不代表线上成功。请勿将报账数据或浏览器登录状态提交到此公开仓库。

