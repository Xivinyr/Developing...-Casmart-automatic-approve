"""报账自动化的桌面任务入口。各任务使用原有独立程序。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import urlsplit


PROJECT = Path(__file__).resolve().parent
CONFIG = PROJECT / "config.example.json"
OUTPUTS = PROJECT / "outputs"
ONLINE_TASK = "腾讯网页选区分类"


def is_tencent_sheet_url(url):
    """交给浏览器的入口限于腾讯文档的 HTTPS 地址。"""
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "docs.qq.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and "\\" not in url
            and not any(char.isspace() for char in url)
        )
    except ValueError:
        return False


class SheetSelectionDialog(simpledialog.Dialog):
    def body(self, parent):
        ttk.Label(parent, text="腾讯表格链接").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        self.url = ttk.Entry(parent, width=48)
        self.url.grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(parent, text="批次名（可选）").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        self.batch = ttk.Entry(parent, width=48)
        self.batch.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(parent, text="例如 2026-11，用于区分输出目录。").grid(row=2, column=1, sticky="w")
        return self.url

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=(0, 4, 0, 8))
        ttk.Button(buttons, text="打开网页", command=self.ok).pack(side="left", padx=6)
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="left", padx=6)
        buttons.pack()
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def validate(self):
        url, batch = self.url.get().strip(), self.batch.get().strip()
        if not is_tencent_sheet_url(url):
            messagebox.showwarning("链接格式不匹配", "请粘贴 https://docs.qq.com 开头的表格链接。", parent=self)
            return False
        if len(batch) > 40:
            messagebox.showwarning("批次名过长", "请使用不超过 40 个字的批次名。", parent=self)
            self.initial_focus = self.batch
            return False
        self.result = url, batch
        return True


class TaskMenu:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.buttons = []
        root.title("报账自动化系统")
        root.minsize(610, 450)
        root.columnconfigure(0, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        style.configure("TaskTitle.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("TaskHint.TLabel", foreground="#465769")
        style.configure("Task.TButton", padding=(10, 7), anchor="w")

        body = ttk.Frame(root, padding=(26, 20))
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="报账自动化系统", style="TaskTitle.TLabel").grid(sticky="w")
        ttk.Label(body, text="选择要运行的任务。", style="TaskHint.TLabel").grid(sticky="w", pady=(4, 14))

        approval = ttk.LabelFrame(body, text="Casmart 采购单据", padding=(12, 6))
        approval.grid(sticky="ew")
        self.add_task(approval, "自动送审", "完成学校认证后，按原流程送审线下采购单据。", self.submit)
        self.add_task(approval, "采购单据签收", "打开签收页，按供应商汇总商品图片和发票。", self.receive_order)
        self.add_task(approval, "自动删除", "预览筛选范围，逐条处理有可用删除按钮的单据。", self.delete_orders)
        self.add_task(approval, "仅验证登录", "登录并进入单据列表，适合检查学校认证。", self.login_only)

        attachments = ttk.LabelFrame(body, text="腾讯文档附件", padding=(12, 6))
        attachments.grid(sticky="ew", pady=(10, 0))
        self.add_task(attachments, ONLINE_TASK, "打开当月工作表，在任务窗口输入 a 自动选区、复制和分类。", self.online_sheet)
        self.add_task(attachments, "本地 XLSX 附件分类", "选择已导出的 XLSX，提取并分类其中的附件。", self.local_xlsx)

        footer = ttk.Frame(body)
        footer.grid(sticky="ew", pady=(16, 0))
        footer.columnconfigure(0, weight=1)
        self.status = tk.StringVar(master=root, value="等待选择任务")
        ttk.Label(footer, textvariable=self.status, style="TaskHint.TLabel", wraplength=420).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(footer, text="打开输出目录", command=self.open_outputs).grid(row=0, column=1, sticky="e", padx=(12, 0))

    def add_task(self, parent, label, description, action):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        row.columnconfigure(1, weight=1)
        button = ttk.Button(row, text=label, width=23, style="Task.TButton", command=action)
        button.grid(row=0, column=0, sticky="w")
        self.buttons.append(button)
        ttk.Label(row, text=description, style="TaskHint.TLabel", wraplength=355).grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )

    def submit(self):
        self.start([sys.executable, str(PROJECT / "songshen.py"), "--wait-on-exit"], "自动送审", console=True)

    def login_only(self):
        self.start([sys.executable, str(PROJECT / "songshen.py"), "--login-only"], "仅验证登录", console=True)

    def receive_order(self):
        self.start([sys.executable, str(PROJECT / "order_receipt.py"), "--execute", "--wait-on-exit"],
                   "采购单据签收", console=True)

    def delete_orders(self):
        # 删除是独立任务，不能由送审或附件分类流程自动触发。
        self.start([sys.executable, str(PROJECT / "order_delete.py"), "--execute", "--wait-on-exit"],
                   "自动删除", console=True)

    def local_xlsx(self):
        filename = filedialog.askopenfilename(parent=self.root, title="选择导出的报账表格", filetypes=[("Excel 表格", "*.xlsx")])
        if not filename:
            return
        source = Path(filename).resolve()
        if not source.is_file() or source.suffix.lower() != ".xlsx":
            messagebox.showwarning("请选择 XLSX 表格", "请选择腾讯文档导出的 .xlsx 文件。", parent=self.root)
            return
        self.start(
            [sys.executable, "-m", "attachments", "xlsx", str(source), "--config", str(CONFIG),
             "--output", str(OUTPUTS)],
            "本地 XLSX 附件分类", attachment=True,
        )

    def online_sheet(self):
        # 先检查当前环境，避免收集链接后才发现运行时未安装。
        bundled_node = Path(sys.executable).with_name("node.exe")
        node = str(bundled_node) if bundled_node.is_file() else shutil.which("node")
        if not node:
            messagebox.showwarning(
                "缺少 Node.js", "请在安装 Node.js 20 或以上版本后，从报账 Conda 环境重新打开主程序。",
                parent=self.root,
            )
            return
        env = os.environ.copy()
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PROJECT / ".local" / "pw-browsers"))
        try:
            probe = subprocess.run(
                [node, "-e", "if(+process.versions.node.split('.')[0]<20)process.exit(1);require('playwright');"],
                cwd=PROJECT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            probe = None
        if probe is None or probe.returncode:
            messagebox.showwarning(
                "在线模式依赖尚未就绪",
                "请按 docs/ATTACHMENTS.md 安装 Node.js 20+ 和 Playwright，再试一次。",
                parent=self.root,
            )
            return

        selection = SheetSelectionDialog(self.root, ONLINE_TASK).result
        if selection is None:
            return
        url, batch = selection
        args = [node, str(PROJECT / "browser" / "authorized.mjs"), url, sys.executable, str(CONFIG), "T"]
        if batch:
            args.append(batch)
        args.append("--selection-only")
        self.start(
            args, ONLINE_TASK, console=True, attachment=True, env=env,
        )

    def start(self, args, label, *, console=False, attachment=False, env=None):
        if self.process is not None:
            return
        # 联网模式需要保留可输入 a/c/p/q 的终端；离线分类在后台执行。
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE" if console else "CREATE_NO_WINDOW", 0)
        streams = {} if console else {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        try:
            self.process = subprocess.Popen(args, cwd=PROJECT, env=env, creationflags=flags, **streams)
        except OSError:
            messagebox.showerror("任务启动失败", "请检查运行环境和项目目录后重试。", parent=self.root)
            return

        for button in self.buttons:
            button.state(["disabled"])
        self.status.set(f"正在运行{label}。" + ("请查看任务窗口。" if console else ""))
        self.root.after(250, self.check_result, label, attachment)

    def check_result(self, label, attachment):
        result = self.process.poll()
        if result is None:
            self.root.after(250, self.check_result, label, attachment)
            return

        self.process = None
        for button in self.buttons:
            button.state(["!disabled"])
        if attachment and result == 2:
            self.status.set(f"{label}有缺项或错误。若已生成结果，请查看输出目录里的清单。")
        elif result != 0:
            self.status.set(f"{label}异常结束（退出码 {result}）。")
        elif attachment:
            self.status.set(f"{label}已结束，已生成的结果可在输出目录查看。")
        else:
            self.status.set(f"{label}已结束，请在任务窗口或浏览器中查看结果。")

    def open_outputs(self):
        try:
            OUTPUTS.mkdir(exist_ok=True)
            os.startfile(str(OUTPUTS))
        except (AttributeError, OSError):
            messagebox.showinfo("输出目录", str(OUTPUTS), parent=self.root)

    def close(self):
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("任务正在运行", "请先在任务窗口完成或退出当前任务。", parent=self.root)
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    TaskMenu(root)
    root.mainloop()


if __name__ == "__main__":
    main()
