# -*- coding: utf-8 -*-
"""
断电哨兵 Blackout Sentinel 开机自启管理（5 种方式）+ Windows 服务实现

方式：
  1. service      Windows 服务（auto，无人值守最强，无需登录）—— 需管理员
  2. boot-task    任务计划·开机时(SYSTEM 后台，失败重启×3)      —— 需管理员
  3. logon-task   任务计划·登录时(当前用户 GUI)
  4. reg-run      注册表 HKCU 下 CurrentVersion/Run(当前用户 GUI)
  5. startup-lnk  启动文件夹快捷方式(GUI)

服务/后台模式读取 blackout_sentinel_config.json；GUI 修改配置实时落盘，服务每轮热加载。
"""

import os
import sys
import time
import threading
import subprocess

import sentinel_core as core

SERVICE_NAME = "BlackoutSentinel"
SERVICE_DISPLAY = "断电哨兵 Blackout Sentinel"
SERVICE_DESC = ("通过不接 UPS 的局域网探针检测市电断电，在 UPS 电池耗尽前自动优雅关机，"
                "并可向同网络中的其他 NAS/设备发送关机指令等动作。")

TASK_BOOT = "BlackoutSentinel_Boot"
TASK_LOGON = "BlackoutSentinel_Logon"
REG_RUN_NAME = "BlackoutSentinel"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ======================== 命令构造 ========================
def _script():
    return os.path.join(core.app_dir(), "blackout_sentinel.py")


def gui_command() -> str:
    """登录后启动 GUI 的命令行。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{_script()}"'


def background_command() -> str:
    """后台监控命令行（无窗口，跑 --background）。登录自启方式一律用它，
    保证自启后自动进入监控而非打开界面等人手动点。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    # 源码运行用 pythonw.exe（无控制台窗口）
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{_script()}" --background'


def service_binpath() -> str:
    """Windows 服务 binPath（带 --service-run 以便与双击 GUI 区分）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --service-run'
    return f'"{sys.executable}" "{_script()}" --service-run'


# ======================== 管理员 / 提权 ========================
def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(args: str) -> bool:
    """以管理员身份重新启动自身并传入参数（弹 UAC）。返回是否成功发起。"""
    try:
        import ctypes
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = args
        else:
            exe = sys.executable
            params = f'"{_script()}" {args}'
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
        return rc > 32
    except Exception:
        return False


def _run(cmd_args, timeout=30):
    try:
        r = subprocess.run(cmd_args, capture_output=True, text=True,
                           encoding="gbk", errors="replace", timeout=timeout,
                           creationflags=_NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


# ======================== Windows 服务 ========================
def service_state():
    """返回服务状态码（4=运行中,1=已停止），未安装返回 None。"""
    try:
        import win32service
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        try:
            svc = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
            try:
                status = win32service.QueryServiceStatus(svc)
                return status[1]
            finally:
                win32service.CloseServiceHandle(svc)
        finally:
            win32service.CloseServiceHandle(scm)
    except Exception:
        return None


def service_install():
    """创建服务（auto）+ 失败自动重启 + 启动。需管理员。返回 (ok, msg)。

    失败策略：reset= 86400（24 小时无失败则重置计数），重启 6 次、每次间隔 60 秒。

    创建用 win32service.CreateService（结构化传参，binPath 原样写入 ImagePath），
    彻底绕开 sc.exe 命令行解析坑——subprocess 引号包裹导致 binPath 值带前导空格/引号
    错位，ImagePath 损坏后 StartService 报 87 参数错误。"""
    if service_state() is not None:
        return True, "服务已存在"
    import win32service
    binpath = service_binpath()
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        try:
            svc = win32service.CreateService(
                scm,
                SERVICE_NAME,
                SERVICE_DISPLAY,
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                binpath,
                None, 0, None, None, None, None)
            win32service.CloseServiceHandle(svc)
        finally:
            win32service.CloseServiceHandle(scm)
    except Exception as e:
        return False, f"创建服务失败: {e}"
    # 描述
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        try:
            svc = win32service.OpenService(scm, SERVICE_NAME,
                                           win32service.SERVICE_CHANGE_CONFIG)
            try:
                win32service.ChangeServiceConfig2(svc, win32service.SERVICE_CONFIG_DESCRIPTION,
                                                  SERVICE_DESC)
            finally:
                win32service.CloseServiceHandle(svc)
        finally:
            win32service.CloseServiceHandle(scm)
    except Exception:
        pass
    # 失败重启：24h 无失败重置计数；连续 6 次、每次 60 秒后重启
    # （sc failure 参数已拆 token，值不含空格不会被引号包裹）
    _run(["sc", "failure", SERVICE_NAME, "reset=", "86400",
          "actions=", "restart/60000/restart/60000/restart/60000/restart/60000/restart/60000/restart/60000"])
    # 启动
    rc2, out2 = _run(["sc", "start", SERVICE_NAME], timeout=30)
    if rc2 != 0 and "1056" not in out2 and "已启动" not in out2:
        return False, (f"服务已创建但启动失败: {out2.strip()}（请检查 exe 路径是否含空格/"
                       f"移动位置后需重装，或手动 sc start {SERVICE_NAME} 排查）")
    return True, "服务已安装并启动（崩溃自动重启×6）"


def service_uninstall():
    """停止并删除服务。需管理员。"""
    if service_state() is None:
        return True, "服务不存在"
    _run(["sc", "stop", SERVICE_NAME], timeout=30)
    time.sleep(1.5)
    rc, out = _run(["sc", "delete", SERVICE_NAME])
    if rc != 0:
        return False, f"删除服务失败: {out.strip()}"
    return True, "服务已删除"


# ======================== 任务计划 ========================
def task_exists(name: str) -> bool:
    rc, _ = _run(["schtasks", "/query", "/tn", name])
    return rc == 0


def _xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _background_command_parts():
    """返回后台监控的 (Command, Arguments, WorkingDirectory)，供任务 XML 使用。"""
    if getattr(sys, "frozen", False):
        return sys.executable, "--background", core.app_dir()
    return sys.executable, f'"{_script()}" --background', core.app_dir()


def _boot_task_xml() -> str:
    """开机任务 XML：SYSTEM 身份、失败重启(1min×3)、错过补跑、电池供电也运行、
    执行时长无限制（看门狗需 7×24 常驻）、多实例忽略（与单实例 mutex 双保险）。"""
    cmd, args, wd = _background_command_parts()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>断电哨兵 Blackout Sentinel：开机后台监控（SYSTEM），通过不接 UPS 的局域网探针检测市电断电，电池耗尽前自动优雅关机</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(cmd)}</Command>
      <Arguments>{_xml_escape(args)}</Arguments>
      <WorkingDirectory>{_xml_escape(wd)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def boot_task_install():
    """开机时 SYSTEM 后台任务（XML 导入：失败重启×3 + 错过补跑 + 电池供电也运行 +
    执行时长无限制）。需管理员。

    注意：任务计划的 RestartOnFailure 只在任务【非零退出】时触发；
    --background 内部有 supervisor 循环常驻，正常永不退出，
    进程真崩了才由任务计划拉起（双保险）。"""
    import tempfile
    xml_path = os.path.join(tempfile.gettempdir(), "blackoutsentinel_boot_task.xml")
    try:
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(_boot_task_xml())
        rc, out = _run(["schtasks", "/create", "/tn", TASK_BOOT,
                        "/xml", xml_path, "/f"])
    finally:
        try:
            os.remove(xml_path)
        except Exception:
            pass
    if rc != 0:
        return False, f"创建开机任务失败: {out.strip()}"
    return True, "开机(SYSTEM)任务已安装（失败自动重启×3、电池供电运行、错过补跑）"


def boot_task_uninstall():
    rc, out = _run(["schtasks", "/delete", "/tn", TASK_BOOT, "/f"])
    return rc == 0 or not task_exists(TASK_BOOT), out.strip()


def logon_task_install():
    """登录时自动后台监控任务（--background，登录即监控，无人值守）。"""
    rc, out = _run(["schtasks", "/create", "/tn", TASK_LOGON,
                    "/tr", background_command(),
                    "/sc", "onlogon", "/rl", "HIGHEST", "/f"])
    if rc != 0:
        return False, f"创建登录任务失败: {out.strip()}"
    return True, "登录任务已安装（登录后自动后台监控）"


def logon_task_uninstall():
    rc, out = _run(["schtasks", "/delete", "/tn", TASK_LOGON, "/f"])
    return rc == 0 or not task_exists(TASK_LOGON), out.strip()


# ======================== 注册表 Run ========================
def reg_run_installed() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, REG_RUN_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def reg_run_install():
    """注册表 Run：登录时自动后台监控（--background）。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE | winreg.KEY_READ)
        winreg.SetValueEx(key, REG_RUN_NAME, 0, winreg.REG_SZ, background_command())
        winreg.CloseKey(key)
        return True, "注册表开机启动已添加（登录后自动后台监控）"
    except Exception as e:
        return False, f"添加注册表失败: {e}"


def reg_run_uninstall():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, REG_RUN_NAME)
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
        return True, "注册表开机启动已移除"
    except Exception as e:
        return False, f"移除注册表失败: {e}"


# ======================== 启动文件夹快捷方式 ========================
def _startup_lnk_path():
    try:
        from win32com.client import Dispatch
        shell = Dispatch("WScript.Shell")
        startup = shell.SpecialFolders("Startup")
        return os.path.join(startup, f"{REG_RUN_NAME}.lnk")
    except Exception:
        appdata = os.environ.get("APPDATA", "")
        return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup",
                            f"{REG_RUN_NAME}.lnk")


def startup_lnk_installed() -> bool:
    return os.path.exists(_startup_lnk_path())


def startup_lnk_install():
    try:
        from win32com.client import Dispatch
        lnk = _startup_lnk_path()
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
        shell = Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(lnk)
        if getattr(sys, "frozen", False):
            sc.TargetPath = sys.executable
            sc.Arguments = "--background"
        else:
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            sc.TargetPath = pyw if os.path.exists(pyw) else sys.executable
            sc.Arguments = f'"{_script()}" --background'
        sc.WorkingDirectory = core.app_dir()
        sc.Description = "断电哨兵 Blackout Sentinel"
        sc.Save()
        return True, "启动文件夹快捷方式已创建（登录后自动后台监控）"
    except Exception as e:
        return False, f"创建快捷方式失败: {e}"


def startup_lnk_uninstall():
    try:
        lnk = _startup_lnk_path()
        if os.path.exists(lnk):
            os.remove(lnk)
        return True, "启动文件夹快捷方式已移除"
    except Exception as e:
        return False, f"移除快捷方式失败: {e}"


# ======================== 统一状态枚举 ========================
METHODS = [
    ("service",    "Windows 服务（开机·无需登录·后台）", True),
    ("boot-task",  "任务计划·开机时（SYSTEM 后台·失败重启）", True),
    ("logon-task", "任务计划·登录时（自动后台监控）",      False),
    ("reg-run",    "注册表开机启动（登录后自动后台监控）", False),
    ("startup-lnk","启动文件夹快捷方式（登录后自动后台监控）", False),
]


def method_status(method: str) -> bool:
    if method == "service":
        return service_state() is not None
    if method == "boot-task":
        return task_exists(TASK_BOOT)
    if method == "logon-task":
        return task_exists(TASK_LOGON)
    if method == "reg-run":
        return reg_run_installed()
    if method == "startup-lnk":
        return startup_lnk_installed()
    return False


def install_method(method: str):
    if method == "service":
        return service_install()
    if method == "boot-task":
        return boot_task_install()
    if method == "logon-task":
        return logon_task_install()
    if method == "reg-run":
        return reg_run_install()
    if method == "startup-lnk":
        return startup_lnk_install()
    return False, "未知方式"


def uninstall_method(method: str):
    if method == "service":
        return service_uninstall()
    if method == "boot-task":
        return boot_task_uninstall()
    if method == "logon-task":
        return logon_task_uninstall()
    if method == "reg-run":
        return reg_run_uninstall()
    if method == "startup-lnk":
        return startup_lnk_uninstall()
    return False, "未知方式"


def uninstall_all():
    msgs = []
    for m, _, _ in METHODS:
        if method_status(m):
            ok, msg = uninstall_method(m)
            msgs.append(f"{m}: {'OK' if ok else msg}")
    return msgs


# ======================== Windows 服务类 ========================
def run_service():
    """服务进程入口（--service-run）：托管 BlackoutSentinelService。"""
    import servicemanager
    import win32event
    import win32serviceutil
    import win32service

    class BlackoutSentinelService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESC

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            self.engine = None
            self.thread = None
            self._mutex = None
            self._engine_restarts = 0
            self._MAX_ENGINE_RESTARTS = 3

        def _evt_log(self, msg):
            # 只写 Windows 事件日志；文件日志由引擎 file_log_path 统一写，
            # 避免同一条消息在文件里落盘两次
            try:
                servicemanager.LogInfoMsg(str(msg))
            except Exception:
                pass

        def _svc_log(self, msg):
            # 服务自身消息（非引擎日志）：事件日志 + 文件各写一次
            self._evt_log(msg)
            core.append_file_log(core.service_log_path(), str(msg))

        def _start_engine(self):
            """创建并启动引擎线程。"""
            self.engine = core.MonitorEngine(
                log_func=self._evt_log,
                file_log_path=core.service_log_path(),
                run_in_service=True,
            )
            self.thread = threading.Thread(target=self._engine_thread_main,
                                           daemon=True)
            self.thread.start()

        def _engine_thread_main(self):
            """引擎线程主体：引擎自身 run() 已有循环内异常保护，
            这里再包一层兜底——万一 run() 整体抛出，记录后线程退出，
            由 SvcDoRun 的看护逻辑重启线程。"""
            try:
                self.engine.run()
            except Exception as e:
                self._svc_log(f"💥 监控引擎线程整体崩溃: {type(e).__name__}: {e}")

        def SvcStop(self):
            # 停止最多等 15 秒（引擎线程每 0.2s 检查一次停止事件，能快速响应）
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=15000)
            if self.engine:
                self.engine.stop()
            win32event.SetEvent(self.hWaitStop)

        def SvcShutdown(self):
            # 系统关机时 SCM 发来的控制：立即停止引擎、不要阻拦系统关机
            self._svc_log("系统正在关机，哨兵服务停止")
            try:
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=10000)
            except Exception:
                pass
            if self.engine:
                self.engine.stop()
            win32event.SetEvent(self.hWaitStop)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""))
            # 单实例仲裁：已有监控实例（别的后台任务）在跑则退出
            self._mutex = core.acquire_single_instance()
            if self._mutex is None:
                self._svc_log("已有监控实例在运行，服务实例退出")
                return

            self._start_engine()

            # 看护循环：等待停止事件，每 10 秒检查引擎线程是否存活。
            # 线程意外死亡 → 重启引擎；连续超限 → 进程退出，由 SCM 的
            # sc failure 策略把整个服务进程拉起（彻底复位）。
            while True:
                rc = win32event.WaitForSingleObject(self.hWaitStop, 10000)
                if rc == win32event.WAIT_OBJECT_0:
                    break  # 收到停止/关机信号
                if self.thread is not None and not self.thread.is_alive():
                    self._engine_restarts += 1
                    if self._engine_restarts <= self._MAX_ENGINE_RESTARTS:
                        self._svc_log(f"⚠️ 监控引擎线程意外退出，"
                                      f"第 {self._engine_restarts} 次重启引擎线程")
                        self._start_engine()
                    else:
                        self._svc_log("❌ 引擎线程连续崩溃超过 "
                                      f"{self._MAX_ENGINE_RESTARTS} 次，服务进程退出，"
                                      f"由 SCM 失败策略在 60 秒后重启整个服务")
                        try:
                            self.ReportServiceStatus(
                                win32service.SERVICE_STOPPED,
                                win32ExitCode=1)
                        except Exception:
                            pass
                        return

            if self.engine:
                self.engine.stop()
            if self.thread:
                self.thread.join(timeout=10)

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(BlackoutSentinelService)
    servicemanager.StartServiceCtrlDispatcher()
