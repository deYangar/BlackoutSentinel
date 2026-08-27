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
    print("\n".join(results))
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
    print("\n".join(results))
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
        print(f"  [{state}]{extra} {label}  <{name}>")
    print(f"管理员权限: {'是' if autostart.is_admin() else '否'}")


if __name__ == "__main__":
    main()
