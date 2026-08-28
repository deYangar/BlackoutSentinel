# -*- coding: utf-8 -*-
"""
断电哨兵 Blackout Sentinel 统一入口

用法：
  BlackoutSentinel.exe                    启动 GUI（若服务/后台已在监控，进入配置面板模式）
  BlackoutSentinel.exe --background       后台监控（无窗口，热加载配置文件）
  BlackoutSentinel.exe --service-run      Windows 服务宿主入口（由 SCM 调用）
  BlackoutSentinel.exe --install all      安装全部自启（service|server|desktop|all|单个方法名）
  BlackoutSentinel.exe --uninstall all    卸载全部（或单个方法名）
  BlackoutSentinel.exe --status           打印各自启方式状态
"""

import os
import sys


def main():
    args = sys.argv[1:]

    # Windows 服务宿主入口
    if "--service-run" in args:
        import sentinel_autostart as autostart
        autostart.run_service()
        return

    # 后台监控模式
    if "--background" in args:
        import sentinel_core as core
        eng = core.run_headless(run_in_service=True)
        # run_headless 内部已 run，正常不会返回（服务停止进程结束）
        return

    # 自启安装/卸载
    if any(a.startswith("--install") for a in args):
        _install(args)
        return
    if any(a.startswith("--uninstall") for a in args):
        _uninstall(args)
        return
    if "--status" in args:
        _status()
        return

    # 默认：启动 GUI
    import blackout_sentinel as gui
    gui.launch_gui()


def _target_method(args):
    """从 --install/--uninstall 取参数：all / server / desktop / 具体方法名。"""
    for i, a in enumerate(args):
        if a.startswith("--install") or a.startswith("--uninstall"):
            if "=" in a:
                return a.split("=", 1)[1].strip()
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return args[i + 1].strip()
    return "all"


def _autostart_result_path() -> str:
    """提权子进程与 GUI 之间的结果文件（临时目录，双方同用户同 TEMP）。

    windowed exe 无 console、print 不可见；文件日志默认关闭——
    安装/卸载结果若不落盘，UAC 提权子进程的成败对用户完全静默。
    因此无论日志开关如何，结果都覆盖写入此文件，GUI 轮询读取显示。"""
    import tempfile
    return os.path.join(tempfile.gettempdir(), "blackoutsentinel_autostart_result.txt")


def _write_autostart_result(text: str):
    """结果文件原子写入：先写 .tmp 再 os.replace，避免 GUI 轮询读到截断中的空文件。"""
    try:
        p = _autostart_result_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except Exception:
        pass


def _safe_print(text: str):
    """windowed exe（console=False）下 sys.stdout 为 None，print 会抛异常导致后续
    代码（如结果文件写入）不执行——安装/卸载结果必须落盘，print 只做尽力而为。"""
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass


def _install(args):
    import sentinel_autostart as autostart
    import sentinel_core as core
    core.refresh_file_log_switch()
    target = _target_method(args)

    if target in ("server",):
        methods = ["service"]
    elif target in ("desktop",):
        methods = ["logon-task", "reg-run", "startup-lnk"]
    elif target in ("all",):
        methods = ["service", "boot-task", "logon-task", "reg-run", "startup-lnk"]
    else:
        methods = [target]

    results = []
    for m in methods:
        needs_admin = dict((name, adm) for name, _, adm in autostart.METHODS).get(m, False)
        if needs_admin and not autostart.is_admin():
            results.append(f"[跳过] {m} 需要管理员权限（请用 --install {m} 从管理员命令行运行，或 GUI 授权）")
            continue
        ok, msg = autostart.install_method(m)
        results.append(f"[{'OK' if ok else '失败'}] {m}: {msg}")
    text = "\n".join(results)
    # 结果文件必须先写：windowed exe 下 print 可能抛异常，写在后面会被跳过
    _write_autostart_result(text)
    _safe_print(text)
    core.append_file_log(core.service_log_path(),
                         "自启安装: " + " | ".join(results))
    if "--bg-nopause" not in args:
        try:
            input("\n按回车退出...")
        except Exception:
            pass


def _uninstall(args):
    import sentinel_autostart as autostart
    import sentinel_core as core
    core.refresh_file_log_switch()
    target = _target_method(args)
    if target in ("all",):
        methods = [m for m, _, _ in autostart.METHODS]
    else:
        methods = [target]
    results = []
    for m in methods:
        needs_admin = dict((name, adm) for name, _, adm in autostart.METHODS).get(m, False)
        if needs_admin and not autostart.is_admin():
            results.append(f"[跳过] {m} 需要管理员权限")
            continue
        ok, msg = autostart.uninstall_method(m)
        results.append(f"[{'OK' if ok else '失败'}] {m}: {msg}")
    text = "\n".join(results)
    _write_autostart_result(text)
    _safe_print(text)
    core.append_file_log(core.service_log_path(),
                         "自启卸载: " + " | ".join(results))
    if "--bg-nopause" not in args:
        try:
            input("\n按回车退出...")
        except Exception:
            pass


def _status():
    import sentinel_autostart as autostart
    for name, label, admin in autostart.METHODS:
        state = "已安装" if autostart.method_status(name) else "未安装"
        extra = ""
        if name == "service":
            st = autostart.service_state()
            if st == 4:
                extra = "（运行中）"
            elif st == 1:
                extra = "（已停止）"
        _safe_print(f"  [{state}]{extra} {label}  <{name}>")
    _safe_print(f"管理员权限: {'是' if autostart.is_admin() else '否'}")


if __name__ == "__main__":
    main()
