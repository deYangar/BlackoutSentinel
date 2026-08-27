# -*- coding: utf-8 -*-
"""
断电哨兵 GUI - Blackout Sentinel
基于 sentinel_core.MonitorEngine，配置实时落盘（服务/后台热加载同一份配置）。

用法：直接启动 GUI。若后台服务/后台任务已在监控，GUI 进入「配置面板模式」——
不重复跑监控，仅用于查看状态、修改配置（自动同步给服务）、管理开机自启。
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox


def setup_dpi_awareness(root):
    """感知 Windows 显示缩放：进程声明 DPI-aware（否则系统位图拉伸会发虚），
    并按当前 DPI 设置 tk scaling（Tk 字体以 point 计，自动随缩放放大）。
    返回缩放比例（1.0 = 100%）。"""
    scale = 1.0
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # SYSTEM_AWARE
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        root.update_idletasks()
        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(root.winfo_id())
        except Exception:
            dpi = 96
        if not dpi or dpi < 72:
            dpi = 96
        scale = dpi / 96.0
        # tk scaling = 每 point 对应像素数（96 DPI 下 96/72=1.333）
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        scale = 1.0
    return scale
import threading
import subprocess
import time
import datetime
import os
import sys

import sentinel_core as core

MAX_LOG_LINES = 5000


class WatchdogApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.dpi_scale = setup_dpi_awareness(root)
        self.root.title("🛡️ 断电哨兵 Blackout Sentinel")
        self.root.geometry(f"{int(820 * self.dpi_scale)}x{int(700 * self.dpi_scale)}")
        self.root.minsize(int(720 * self.dpi_scale), int(560 * self.dpi_scale))

        self._alive = True
        self.engine = None
        self.engine_thread = None
        self.monitoring = False
        self.panel_mode = False
        self._mutex = None

        cfg = core.load_config()
        # 本进程文件日志开关与配置对齐（默认关，不写本地日志）
        core.set_file_log_enabled(bool(cfg.get("file_log", False)))
        self.ip_var = tk.StringVar(value=cfg.get("ip", core.DEFAULT_CONFIG["ip"]))
        self.interval_var = tk.IntVar(value=cfg.get("interval", 2))
        self.threshold_var = tk.IntVar(value=cfg.get("threshold", 60))
        self.grace_var = tk.IntVar(value=cfg.get("grace_period", 60))
        # 文件日志开关（默认关：不写 blackout_sentinel_service.log）
        self.file_log_var = tk.BooleanVar(value=bool(cfg.get("file_log", False)))
        # 备用监控目标（逗号分隔，最多 core.MAX_TARGETS 个）；默认空
        _tg = cfg.get("targets") or []
        self.target_var = tk.StringVar(value=", ".join(_tg))
        # 动作链：每行一个动作（类型 + 目标 + 执行后等待秒数），可增删
        self.action_rows = []   # [{frame, action_var, target_var, wait_var, target_entry, combo}]

        self._cfg_after_id = None
        self._suppress_trace = False

        self._build_ui()
        # 动作链初始行（在 _build_ui 创建好动作区之后）
        for item in cfg.get("actions", core.DEFAULT_CONFIG["actions"]):
            self._add_action_row(
                action=item.get("action", core.ACTION_SOFTWARE),
                target=item.get("target", ""),
                wait_after=item.get("wait_after", 0))
        self._build_trace()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log("程序已启动。配置文件: " + core.config_path())

        # 单实例检测：若已有监控在跑，进入配置面板模式
        self._mutex = core.acquire_single_instance()
        if self._mutex is None:
            self.panel_mode = True
            self._enter_panel_mode()
        elif self._mutex == -1:
            # 互斥锁创建异常（权限/非 Windows）：用服务状态兜底判断
            try:
                import sentinel_autostart as autostart
                if autostart.service_state() == 4:
                    self.panel_mode = True
                    self._enter_panel_mode()
                    self._log("⚠️ 互斥锁不可用，但检测到服务正在运行，已进入配置面板模式", "warn")
                else:
                    self._log("⚠️ 单实例互斥锁创建失败，请避免同时运行多个监控实例", "warn")
            except Exception:
                self._log("⚠️ 单实例互斥锁不可用，请避免重复启动监控", "warn")
        else:
            self._log("✅ 无其他监控实例，本 GUI 可直接开始监控")

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        cfg_frame = ttk.LabelFrame(self.root, text="监控配置")
        cfg_frame.pack(fill="x", **pad)

        ttk.Label(cfg_frame, text="目标 IP:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        ttk.Entry(cfg_frame, textvariable=self.ip_var, width=20).grid(row=0, column=1, sticky="w")

        ttk.Label(cfg_frame, text="Ping 间隔(秒):").grid(row=0, column=2, sticky="e", padx=5)
        ttk.Spinbox(cfg_frame, from_=1, to=60, textvariable=self.interval_var, width=6).grid(row=0, column=3, sticky="w")

        ttk.Label(cfg_frame, text="不通阈值(秒):").grid(row=0, column=4, sticky="e", padx=5)
        ttk.Spinbox(cfg_frame, from_=5, to=3600, textvariable=self.threshold_var, width=7).grid(row=0, column=5, sticky="w")
        ttk.Label(cfg_frame, text="开机启动宽限(秒):").grid(row=0, column=6, sticky="e", padx=5)
        ttk.Spinbox(cfg_frame, from_=0, to=3600, textvariable=self.grace_var, width=6).grid(row=0, column=7, sticky="w")

        # 第二行：备用目标（多目标仲裁，全部不通才算断网）
        ttk.Label(cfg_frame, text="备用目标(逗号分隔):").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        ttk.Entry(cfg_frame, textvariable=self.target_var, width=20).grid(row=1, column=1, sticky="w")

        # ---- 动作链（可增删，按顺序执行）----
        act_outer = ttk.LabelFrame(self.root, text="触发动作（按顺序执行，可添加多个）")
        act_outer.pack(fill="x", **pad)

        self.act_frame = ttk.Frame(act_outer)
        self.act_frame.pack(fill="x", padx=8, pady=(4, 2))
        self.act_frame.columnconfigure(4, weight=1)

        add_bar = ttk.Frame(act_outer)
        add_bar.pack(fill="x", padx=8, pady=(0, 6))
        self.add_btn = ttk.Button(add_bar, text="➕ 添加动作", command=self._add_action_row)
        self.add_btn.pack(side="left")
        ttk.Label(add_bar,
                  text='提示：SSH 关 NAS 示例  echo y | plink root@192.168.x.x -pw 密码 "poweroff"'
                       '（plink 已随程序自带，无需配免密）',
                  foreground="#888").pack(side="left", padx=10)

        status = ttk.LabelFrame(self.root, text="状态")
        status.pack(fill="x", **pad)
        self.status_label = ttk.Label(status, text="⏹ 已停止", font=("Microsoft YaHei", 11, "bold"))
        self.status_label.pack(side="left", padx=10, pady=6)
        self.fail_label = ttk.Label(status, text="连续不通: 0 秒", foreground="#444")
        self.fail_label.pack(side="left", padx=20)
        self.last_label = ttk.Label(status, text="最近: —")
        self.last_label.pack(side="left", padx=10)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="▶ 开始监控", command=self.start)
        self.start_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(btns, text="⏹ 停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        ttk.Button(btns, text="🧪 测试 Ping", command=self.test_ping).pack(side="left", padx=5)
        ttk.Button(btns, text="⚙️ 开机自启", command=self.open_autostart_window).pack(side="left", padx=5)
        ttk.Button(btns, text="🧹 清空日志", command=self.clear_log).pack(side="left", padx=5)
        ttk.Checkbutton(btns, text="📝 写日志到文件", variable=self.file_log_var).pack(side="left", padx=8)

        self.mode_tip = ttk.Label(self.root, text="", foreground="#1a7f37")
        self.mode_tip.pack(fill="x", padx=10)

        log_frame = ttk.LabelFrame(self.root, text="日志")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap="word", font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4")
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)
        for tag, color in (("ok", "#6A9955"), ("fail", "#F44747"), ("warn", "#DCDCAA"),
                           ("info", "#9CDCFE"), ("trigger", "#C586C0")):
            self.log_text.tag_config(tag, foreground=color)
        self.log_text.tag_config("trigger", foreground="#C586C0",
                                 font=("Consolas", 10, "bold"))

    # ---------------- 动作链行 ----------------
    def _add_action_row(self, action=None, target="", wait_after=0):
        """新增一行动作。超过 MAX_ACTIONS 时拒绝。"""
        if len(self.action_rows) >= core.MAX_ACTIONS:
            messagebox.showinfo("提示", f"最多 {core.MAX_ACTIONS} 个动作")
            return None
        idx = len(self.action_rows)
        row = ttk.Frame(self.act_frame)
        row.grid(row=idx, column=0, columnspan=6, sticky="we", pady=2)

        act_key = action if action in core.ACTION_LABELS else core.ACTION_SOFTWARE
        action_var = tk.StringVar(value=core.ACTION_LABELS[act_key])
        # target=None（新增行）→ 用该类型默认值；从配置加载时原样保留（含空字符串）
        default_tgt = self._DEFAULT_TARGET.get(act_key, "")
        target_var = tk.StringVar(value=default_tgt if target is None else (target or ""))
        try:
            wait_var = tk.IntVar(value=int(wait_after or 0))
        except (tk.TclError, ValueError, TypeError):
            wait_var = tk.IntVar(value=0)

        ttk.Label(row, text=f"{idx + 1}.", width=3).pack(side="left")
        combo = ttk.Combobox(row, textvariable=action_var, state="readonly",
                             width=10, values=list(core.ACTION_LABELS.values()))
        combo.pack(side="left")
        target_entry = ttk.Entry(row, textvariable=target_var, width=34)
        target_entry.pack(side="left", padx=(6, 6), fill="x", expand=True)
        ttk.Label(row, text="执行后等待").pack(side="left")
        ttk.Spinbox(row, from_=0, to=600, textvariable=wait_var, width=5).pack(side="left", padx=(2, 2))
        ttk.Label(row, text="秒").pack(side="left")
        del_btn = ttk.Button(row, text="🗑", width=3,
                             command=lambda r=row: self._del_action_row(r))
        del_btn.pack(side="left", padx=(8, 0))

        rec = {"frame": row, "action_var": action_var, "target_var": target_var,
               "wait_var": wait_var, "target_entry": target_entry, "combo": combo,
               "del_btn": del_btn}
        self.action_rows.append(rec)

        combo.bind("<<ComboboxSelected>>",
                   lambda e=None, r=rec: self._on_row_action_change(r, reset_target=True))
        target_var.trace_add("write", lambda *a: self._on_cfg_change())
        wait_var.trace_add("write", lambda *a: self._on_cfg_change())

        # 初始化：只同步输入框可用状态，不重置已从配置加载的目标值
        self._on_row_action_change(rec, fire=False, reset_target=False)
        self._refresh_add_btn()
        self._refresh_del_buttons()
        return rec

    def _del_action_row(self, frame):
        if len(self.action_rows) <= 1:
            messagebox.showinfo("提示", "至少保留一个动作")
            return
        rec = next((r for r in self.action_rows if r["frame"] is frame), None)
        if rec:
            self.action_rows.remove(rec)
        frame.destroy()
        for i, r in enumerate(self.action_rows, 1):
            kids = r["frame"].winfo_children()
            if kids:
                kids[0].config(text=f"{i}.")
        self._refresh_add_btn()
        self._refresh_del_buttons()
        self._on_cfg_change()

    def _refresh_add_btn(self):
        n = len(self.action_rows)
        self.add_btn.config(state=("disabled" if n >= core.MAX_ACTIONS else "normal"))

    def _refresh_del_buttons(self):
        """只剩一个动作时隐藏删除按钮（该行不允许删除）。"""
        multi = len(self.action_rows) > 1
        for r in self.action_rows:
            btn = r["del_btn"]
            if multi:
                if not btn.winfo_ismapped():
                    btn.pack(side="left", padx=(8, 0))
            else:
                btn.pack_forget()

    # 各动作类型新建/切换时的目标默认值
    _DEFAULT_TARGET = {
        core.ACTION_SOFTWARE: "notepad.exe",
        core.ACTION_COMMAND: 'echo y | plink root@192.168.x.x -pw 密码 "poweroff"',
        core.ACTION_SLEEP: "",
        core.ACTION_SHUTDOWN: "",
    }

    def _on_row_action_change(self, rec, fire=True, reset_target=False):
        """动作类型变化：同步目标输入框可用状态；reset_target=True 时
        把目标框重置为新类型的默认值（用户主动切换类型时使用，
        类型都变了旧目标值已无意义）。"""
        key = self._action_key_of(rec["action_var"].get())
        if key in (core.ACTION_SLEEP, core.ACTION_SHUTDOWN):
            rec["target_entry"].config(state="disabled")
            if reset_target:
                rec["target_var"].set("")
        else:
            rec["target_entry"].config(state="normal")
            if reset_target:
                rec["target_var"].set(self._DEFAULT_TARGET.get(key, ""))
        if fire:
            self._on_cfg_change()

    def _action_key_of(self, label):
        return next((k for k, v in core.ACTION_LABELS.items() if v == label),
                    core.ACTION_SOFTWARE)

    def _build_trace(self):
        for var in (self.ip_var, self.interval_var, self.threshold_var,
                    self.grace_var, self.target_var,
                    self.file_log_var):
            var.trace_add("write", lambda *a: self._on_cfg_change())

    def _enter_panel_mode(self):
        """后台服务/任务已在监控：GUI 作为配置面板，不重复跑监控。"""
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text="🛡 后台监控运行中", foreground="#0a66c2")
        self.mode_tip.config(
            text="ℹ️ 检测到服务/后台任务已在监控。此处修改的配置会自动保存并由后台实时热加载，"
                 "无需停止监控。请通过「⚙️ 开机自启」管理后台实例。")
        self._log("🛡 检测到后台监控实例（服务/后台任务）正在运行。GUI 进入配置面板模式。", "info")
        self._log("   你在此修改的任何配置会实时落盘，后台 2 秒内热加载生效。", "info")

    # ---------------- 配置（UI → 落盘） ----------------
    def _read_actions_ui(self):
        """从动作行读取动作链，返回 list[dict]；某行解析失败返回 None。"""
        actions = []
        for rec in self.action_rows:
            key = self._action_key_of(rec["action_var"].get())
            target = rec["target_var"].get().strip()
            try:
                wait = int(rec["wait_var"].get())
            except (ValueError, tk.TclError):
                wait = 0
            actions.append({
                "action": key,
                "target": target,
                "wait_after": max(0, min(wait, 600)),
            })
        return actions or None

    def _on_cfg_change(self):
        if self._suppress_trace:
            return
        if self._cfg_after_id is not None:
            try:
                self.root.after_cancel(self._cfg_after_id)
            except Exception:
                pass
        self._cfg_after_id = self._after(600, self._apply_cfg)

    def _parse_targets(self):
        """解析备用目标输入框：逗号分隔（支持中文逗号），合法 IPv4、去重、排除主 IP、限上限。"""
        ip = self.ip_var.get().strip()
        targets = []
        for part in self.target_var.get().replace("，", ",").split(","):
            part = part.strip()
            if part and core.is_valid_ip(part) and part != ip and part not in targets:
                targets.append(part)
        return targets[:core.MAX_TARGETS]

    def _read_ui(self):
        try:
            ip = self.ip_var.get().strip()
            if not core.is_valid_ip(ip):
                return None
            try:
                interval = int(self.interval_var.get())
                threshold = int(self.threshold_var.get())
                grace = int(self.grace_var.get())
            except (ValueError, tk.TclError):
                return None
            actions = self._read_actions_ui()
            if not actions:
                return None
            return {
                "ip": ip,
                "interval": max(1, min(interval, 60)),
                "threshold": max(5, min(threshold, 3600)),
                "grace_period": max(0, min(grace, 3600)),
                "file_log": bool(self.file_log_var.get()),
                "targets": self._parse_targets(),
                "actions": actions,
            }
        except Exception:
            return None

    def _apply_cfg(self):
        self._cfg_after_id = None
        if not self._alive:
            return
        cfg = self._read_ui()
        if not cfg or not cfg["ip"]:
            return
        if self.engine is not None:
            # GUI 监控模式：更新引擎内存（立即生效）+ 落盘（供服务/其他实例读）
            old = self.engine.get_config()
            self.engine.update_config(**cfg)
            changes = []
            if old.get("ip") != cfg["ip"]:
                changes.append(f"监控目标 {old.get('ip')} → {cfg['ip']}（计时已重置）")
            if old.get("interval") != cfg["interval"]:
                changes.append(f"间隔 → {cfg['interval']} 秒")
            if old.get("threshold") != cfg["threshold"]:
                changes.append(f"阈值 → {cfg['threshold']} 秒")
            if old.get("actions") != cfg["actions"]:
                changes.append(f"动作链 → {core.action_brief(cfg['actions'])}")
            if changes:
                self._log("🔄 配置已实时更新: " + "；".join(changes))
        else:
            # 面板模式（服务在跑）：落盘即可，服务热加载
            core.save_config(cfg)
        # 同步本进程文件日志开关（GUI 自己的日志落盘也受它控制）
        core.set_file_log_enabled(cfg.get("file_log", False))

    # ---------------- 日志 ----------------
    def _after(self, ms, func):
        if not self._alive:
            return None
        try:
            return self.root.after(ms, func)
        except Exception:
            return None

    def _log(self, msg, tag="info"):
        core.append_file_log(core.service_log_path(), msg)

        def _append():
            if not self._alive:
                return
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n", tag)
                lines = int(self.log_text.index("end-1c").split(".")[0])
                if lines > MAX_LOG_LINES:
                    self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except Exception:
                pass
        self._after(0, _append)

    def clear_log(self):
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    # ---------------- 控制 ----------------
    def test_ping(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showwarning("提示", "请填写目标 IP")
            return
        if not core.is_valid_ip(ip):
            messagebox.showwarning("提示", f"目标 IP 格式不正确：{ip}")
            return
        # 主目标 + 备用目标一起测（多目标仲裁：任一通则网络正常）
        ips = [ip] + self._parse_targets()
        self._log(f"🧪 测试 ping {len(ips)} 个目标: {', '.join(ips)} ...")

        def _run():
            results = core.ping_all(ips)
            now = datetime.datetime.now().strftime("%H:%M:%S")
            ok_ips = [x for x, ok in zip(ips, results) if ok]
            for x, ok in zip(ips, results):
                self._log(f"[{now}] {'✅' if ok else '❌'} ping {x} {'通' if ok else '不通'}",
                          "ok" if ok else "fail")
            if ok_ips:
                self._after(0, lambda: self.last_label.config(
                    text=f"最近: ✅ {now} {len(ok_ips)}/{len(ips)} 可达"))
            else:
                self._after(0, lambda: self.last_label.config(
                    text=f"最近: ❌ {now} 全部不通"))
        threading.Thread(target=_run, daemon=True).start()

    def start(self):
        if self.monitoring or self.panel_mode:
            return
        # 先校验 IP 格式（拼错 IP 会导致 ping 永久失败 → 误关机）
        ip_raw = self.ip_var.get().strip()
        if not ip_raw:
            messagebox.showwarning("提示", "请填写目标 IP")
            return
        if not core.is_valid_ip(ip_raw):
            messagebox.showwarning("提示", f"目标 IP 格式不正确：{ip_raw}\n请填写合法的 IPv4 地址，例如 192.168.1.1")
            return
        for part in self.target_var.get().replace("，", ",").split(","):
            part = part.strip()
            if part and not core.is_valid_ip(part):
                messagebox.showwarning("提示", f"备用目标 IP 格式不正确：{part}")
                return
        cfg = self._read_ui()
        if not cfg:
            messagebox.showwarning("提示", "配置有误，请检查间隔/阈值/宽限期等数值")
            return
        actions = cfg["actions"]
        # 校验：需要目标的动作必须填写
        for i, a in enumerate(actions, 1):
            if a["action"] in (core.ACTION_SOFTWARE, core.ACTION_COMMAND) and not a["target"]:
                messagebox.showwarning("提示", f"第 {i} 个动作（{core.ACTION_LABELS[a['action']]}）需要填写目标路径/命令")
                return
        # 关机/休眠不在动作链末尾时警告：该动作执行后机器关停，后面的动作不会执行
        power_pos = [i for i, a in enumerate(actions)
                     if a["action"] in (core.ACTION_SHUTDOWN, core.ACTION_SLEEP)]
        if power_pos and max(power_pos) < len(actions) - 1:
            if not messagebox.askyesno("注意",
                    "关机/休眠动作不在动作链末尾。\n"
                    "该动作执行后机器会关停，排在它后面的动作将不会执行。\n\n"
                    "建议把关机/休眠移到最后。仍要按当前配置开始监控吗？"):
                return
        has_power = any(a["action"] in (core.ACTION_SHUTDOWN, core.ACTION_SLEEP) for a in actions)
        if has_power:
            names = "、".join(core.ACTION_LABELS[a["action"]] for a in actions
                              if a["action"] in (core.ACTION_SHUTDOWN, core.ACTION_SLEEP))
            if not messagebox.askyesno("确认",
                    f"连续不通 {cfg['threshold']} 秒后将依次执行 {len(actions)} 个动作"
                    f"（含【{names}】），开机启动宽限 {cfg['grace_period']} 秒，确定开始监控？"):
                return

        core.save_config(cfg)
        self.monitoring = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_label.config(text="🔄 监控中", foreground="#1a7f37")
        tg_desc = ("，备用目标: " + ", ".join(cfg["targets"])) if cfg.get("targets") else ""
        self._log(f"▶ 开始监控: 目标={cfg['ip']}{tg_desc}, interval={cfg['interval']}s, "
                  f"threshold={cfg['threshold']}s, 宽限期={cfg['grace_period']}s, "
                  f"动作链: {core.action_brief(actions)}")

        self.engine = core.MonitorEngine(
            log_func=lambda m: self._log_engine(m),
            file_log_path=None,
            run_in_service=False,
        )
        self.engine.update_config(**cfg)
        self.engine.on_fail_seconds = lambda s: self._after(
            0, lambda: self.fail_label.config(text=f"连续不通: {s} 秒"))
        self.engine.on_last_result = lambda t: self._after(
            0, lambda: self.last_label.config(text=t))
        self.engine_thread = threading.Thread(target=self.engine.run, daemon=True)
        self.engine_thread.start()

    def _log_engine(self, msg):
        """引擎日志按关键字着色。"""
        tag = "info"
        if "✅" in msg or "恢复" in msg:
            tag = "ok"
        elif "❌" in msg:
            tag = "fail"
        elif "⚠️" in msg or "⏰" in msg or "🔀" in msg:
            tag = "warn"
        elif "🚨" in msg:
            tag = "trigger"
        self._log(msg, tag)

    def stop(self):
        if not self.monitoring:
            return
        if self.engine:
            self.engine.stop()
        self.monitoring = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text="⏹ 已停止", foreground="#444")
        self.fail_label.config(text="连续不通: 0 秒")
        self._log("⏹ 监控已停止", "warn")

    def _on_close(self):
        try:
            if self.engine:
                self.engine.stop()
            if self.engine_thread and self.engine_thread.is_alive():
                self.engine_thread.join(timeout=5)
        finally:
            self._alive = False
            try:
                self.root.destroy()
            except Exception:
                pass

    # ---------------- 开机自启窗口 ----------------
    def open_autostart_window(self):
        AutostartWindow(self)


class AutostartWindow:
    def __init__(self, app: WatchdogApp):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("⚙️ 开机自启管理")
        # 先隐藏：Toplevel 创建时会先按默认位置（屏幕左上角）映射显示，
        # 若之后再改坐标会肉眼可见地闪一下。withdraw → 算坐标 → deiconify，
        # 用户看到第一眼就是最终位置。
        self.win.withdraw()
        s = getattr(app, "dpi_scale", 1.0)
        W, H = int(640 * s), int(460 * s)
        x = y = None
        try:
            app.root.update_idletasks()
            px, py = app.root.winfo_x(), app.root.winfo_y()
            pw, ph = app.root.winfo_width(), app.root.winfo_height()
            x = px + max(0, (pw - W) // 2)
            y = py + max(0, (ph - H) // 2)
            # 钳制到屏幕可视区域内，避免主窗口贴边时子窗口超出屏幕
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            x = max(0, min(x, sw - W - 20))
            y = max(0, min(y, sh - H - 40))
        except Exception:
            x = y = None
        if x is not None:
            self.win.geometry(f"{W}x{H}+{x}+{y}")
        else:
            self.win.geometry(f"{W}x{H}")
        self.win.transient(app.root)
        self.win.deiconify()  # 位置就绪后再显示
        try:
            self.win.lift(app.root)
        except Exception:
            pass
        self.rows = {}

        ttk.Label(self.win, text="勾选/取消即可安装或卸载对应自启方式。多种方式冗余可提高启动可靠性。",
                  foreground="#555").pack(anchor="w", padx=12, pady=(10, 4))

        import sentinel_autostart as autostart
        self.autostart = autostart

        frame = ttk.Frame(self.win)
        frame.pack(fill="both", expand=True, padx=12, pady=6)

        header = ttk.Frame(frame)
        header.pack(fill="x")
        ttk.Label(header, text="状态", width=8).pack(side="left")
        ttk.Label(header, text="自启方式").pack(side="left")

        self.vars = {}
        for name, label, admin in autostart.METHODS:
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=3)
            state_lbl = ttk.Label(row, text="", width=8, foreground="#888")
            state_lbl.pack(side="left")
            var = tk.BooleanVar()
            cb = ttk.Checkbutton(row, text=label + ("  🔒需管理员" if admin else ""),
                                 variable=var, command=lambda n=name: self._toggle(n))
            cb.pack(side="left")
            self.rows[name] = (state_lbl, var)
            self.vars[name] = var

        ttk.Separator(self.win).pack(fill="x", padx=12, pady=6)
        quick = ttk.Frame(self.win)
        quick.pack(fill="x", padx=12)
        ttk.Button(quick, text="一键部署·服务器(服务)",
                   command=lambda: self._quick("server")).pack(side="left", padx=4)
        ttk.Button(quick, text="一键部署·桌面(登录GUI)",
                   command=lambda: self._quick("desktop")).pack(side="left", padx=4)
        ttk.Button(quick, text="全部卸载",
                   command=lambda: self._quick("uninstall")).pack(side="left", padx=4)
        ttk.Button(quick, text="🔄 刷新状态",
                   command=self.refresh).pack(side="right", padx=4)

        self.note = ttk.Label(self.win, text="", foreground="#c0392b", wraplength=600)
        self.note.pack(fill="x", padx=12, pady=6)
        self.refresh()

    def refresh(self):
        for name, label, admin in self.autostart.METHODS:
            state_lbl, var = self.rows[name]
            installed = self.autostart.method_status(name)
            var.set(installed)
            if name == "service":
                st = self.autostart.service_state()
                if installed and st == 4:
                    state_lbl.config(text="🟢运行中", foreground="#1a7f37")
                elif installed:
                    state_lbl.config(text="🟡已安装", foreground="#b8860b")
                else:
                    state_lbl.config(text="⚪未安装", foreground="#888")
            else:
                state_lbl.config(text="🟢已安装" if installed else "⚪未安装",
                                 foreground="#1a7f37" if installed else "#888")
        self.win.after(4000, lambda: self._alive_refresh())

    def _alive_refresh(self):
        try:
            if self.win.winfo_exists():
                self.refresh()
        except Exception:
            pass

    def _toggle(self, name):
        installed = self.autostart.method_status(name)
        if installed:
            self._do(name, uninstall=True)
        else:
            self._do(name, uninstall=False)
        self.refresh()

    def _do(self, name, uninstall):
        admin = dict((n, a) for n, _, a in self.autostart.METHODS).get(name, False)
        if admin and not self.autostart.is_admin():
            # 需要管理员：弹 UAC 以提权子进程执行
            action = "uninstall" if uninstall else "install"
            ok = self.autostart.relaunch_as_admin(f"--{action} {name} --bg-nopause")
            self.note.config(
                text=("已请求管理员授权（UAC），请在弹窗中确认；完成后点「刷新状态」查看。"
                      if ok else "提权失败，请右键以管理员身份运行程序后再操作此项。"))
            return
        if uninstall:
            ok, msg = self.autostart.uninstall_method(name)
        else:
            ok, msg = self.autostart.install_method(name)
        self.note.config(text=f"{msg}", foreground="#1a7f37" if ok else "#c0392b")
        self.app._log(f"自启 [{name}] {'卸载' if uninstall else '安装'}: {msg}")

    def _quick(self, mode):
        if mode == "server":
            if not self.autostart.is_admin():
                self.autostart.relaunch_as_admin("--install server --bg-nopause")
                self.note.config(text="已请求管理员授权安装 Windows 服务，请在 UAC 弹窗确认，然后刷新。")
                return
            ok, msg = self.autostart.install_method("service")
            self.note.config(text=msg, foreground="#1a7f37" if ok else "#c0392b")
        elif mode == "desktop":
            msgs = []
            for m in ("logon-task", "reg-run", "startup-lnk"):
                ok, msg = self.autostart.install_method(m)
                msgs.append(msg)
            self.note.config(text="；".join(msgs), foreground="#1a7f37")
        elif mode == "uninstall":
            if not self.autostart.is_admin():
                self.autostart.relaunch_as_admin("--uninstall all --bg-nopause")
                self.note.config(text="已请求管理员授权卸载，请在 UAC 弹窗确认，然后刷新。")
                return
            msgs = self.autostart.uninstall_all()
            self.note.config(text="；".join(msgs) if msgs else "无已安装项", foreground="#1a7f37")
        self.refresh()


def launch_gui():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    WatchdogApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
