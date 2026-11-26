# Casmart 与学校统一认证登录

## 运行

在项目目录中激活本机已有的独立环境：

```powershell
conda activate reimbursement_auto
python songshen.py --login-only
```

`--login-only` 只进行登录并进入线下单据列表，不查找或点击送审按钮。确认实际学校认证流程可用后，正常使用 `python songshen.py`，继续原有送审与翻页流程。

1. 在弹窗中填写学校统一认证账号密码。已有保存内容会预填，也可同时留空后在浏览器手动输入。
2. 脚本直接打开学校统一认证入口，查找“校园网账号”及对应密码框。写入后会检查两项内容是否留在输入框中。
3. 在学校页面完成验证码、微信或其他验证，并按页面提示登录。
4. 脚本确认已回到 Casmart 会员中心后，沿用本次回跳的新会话参数进入线下单据列表。

直登使用项目目录下被 Git 忽略的 `.local/smu-login-url.txt`。在其他设备上，把 Casmart 跳转到学校时浏览器地址栏中的完整 URL 单独粘贴到该文件的一行里，保留原有回跳和账号参数。若学校页面要求重新从商城发起认证，运行 `python songshen.py --login-only --via-casmart`，弹窗会显示两处账号。

默认最多等待 600 秒，可修改 `songshen.py` 的 `LOGIN_WAIT_SECONDS`。若学校页面重绘后未保留输入，脚本最多重新填写一次。账号密码错误时可以直接在浏览器修正。进入业务流程以认证回跳和线下单据列表检查为准。

## 保存与清除

勾选“记住账号密码”后，登录信息加密保存到 `%LOCALAPPDATA%\CasmartAuto\credentials.bin`，下次运行会预填弹窗。直登模式取消勾选后继续会取消记住学校账号，单独保存过的商城账号仍可在商城模式使用。使用“清除保存”可删除两处已保存信息。关闭或取消弹窗时保留已有内容。

使用 Windows 当前用户的 DPAPI 加密整个凭证内容，写入文件与临时文件的都是密文，不增加密码库依赖。通常只能在同一台设备、同一 Windows 用户下解密，换设备需重新填写；同一 Windows 用户运行的程序仍可调用系统解密。依据：[Microsoft CryptProtectData 文档](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)。

源码不包含个人账号、学校用户标识或密码，不读取旧飞书浏览器配置，也不保存新的浏览器登录态。自动填写仅用于明确列出的 Casmart HTTPS 域名及学校认证 HTTPS 域名；遇到其他验证站点时由本人操作。

## 验证范围（2026-11-26）

- 合成验证：27 项自动测试通过，覆盖默认直登、旧入口、回跳限制、认证后的列表检查、仅登录模式及 Windows 加密保存。另用 Edge 和模拟表单验证键盘填入、读取确认、检测页面清空、验证码及其他密码框保留。
- 真实腾讯导出验证：本次未执行，与登录适配分开记录。
- 线上授权验证：核对了学校公开页面的账号、密码及验证码标签。完整入口的真实登录、回跳及列表访问待验证，可用 `--login-only` 验收。公开页面见[南方医科大学统一身份认证平台](https://uis.smu.edu.cn/login.jsp)。

测试命令：`python -B -m unittest discover -s tests -v`。仅新增标准库模块，Python 运行依赖仍为 Selenium。
