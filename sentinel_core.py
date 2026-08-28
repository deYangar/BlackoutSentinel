# -*- coding: utf-8 -*-
"""
断电哨兵 Blackout Sentinel 核心模块（无 GUI 依赖）
- 配置持久化：blackout_sentinel_config.json（服务 / 后台 / GUI 共享同一份）
- MonitorEngine：监控引擎，支持配置热加载（服务模式轮询文件，GUI 模式内存+落盘）
- ping / 多目标仲裁 / 启动宽限期 / 关机失败重试 / 动作执行 / 单实例互斥 / 文件日志

GUI、Windows 服务、后台命令行三种形态共用本模块。
"""

import os
import sys
import re
import json
import time
import ipaddress
import threading
import subprocess
import datetime
from concurrent.futures import ThreadPoolExecutor

# 应用版本号（GUI 标题栏、发布用）
APP_VERSION = "1.1.1"

# 监控逻辑统一通过 _now() 取时间，便于测试 mock（不影响 time.sleep）
_now = time.time
_sleep = time.sleep

# ======================== 默认配置 ========================
DEFAULT_CONFIG = {
    "ip": "192.168.1.1",
    "interval": 2,            # ping 间隔（秒），1~60
    "threshold": 60,          # 连续不通阈值（秒），5~3600
    # 启动宽限期（秒）：服务/引擎启动后的保护期。开机时 DHCP/交换机 STP/网卡
    # 驱动可能 1~2 分钟才就绪，期内即使连续不通也不触发动作（只记录）。
    # 宽限期过后仍连续不通达阈值则照关。0 = 关闭宽限期。
    "grace_period": 60,
    # 是否把运行日志写入本地文件 blackout_sentinel_service.log。默认关闭：
    # 不生成/不写本地日志文件（GUI 日志框仍正常显示，只是不落盘）。
    # 需要排查 7×24 运行问题时在 GUI 勾选开启，服务/后台热加载后即生效。
    "file_log": False,
    # 备用监控目标（IPv4 列表，默认空）：配置后所有目标【全部不通】才算断网，
    # 任一通则视为正常。避免单台设备（如 NAS/路由器）重启导致本机误关机。
    "targets": [],
    # 动作链：触发后按顺序执行。每项 {action, target, wait_after}
    # action: software / sleep / shutdown / command
    # wait_after: 该动作执行后等待秒数（给关机/休眠类动作留时间），再执行下一个
    "actions": [
        {"action": "software", "target": "notepad.exe", "wait_after": 0}
    ],
}
MAX_ACTIONS = 10
MAX_TARGETS = 2            # 主 IP 之外的备用目标上限（总目标数最多 3）
PING_TIMEOUT_MS = 1000
POWEROFF_RETRY_INTERVAL = 60   # 关机/休眠动作失败后的重试间隔（秒）
GRACE_WARN_INTERVAL = 15       # 宽限期内达阈值时的日志提示节流（秒）
CONFIG_FILENAME = "blackout_sentinel_config.json"
CONFIG_BAK_SUFFIX = ".bak"
SERVICE_LOG_FILENAME = "blackout_sentinel_service.log"
MAX_LOG_FILE_BYTES = 2 * 1024 * 1024

ACTION_SOFTWARE = "software"
ACTION_SLEEP = "sleep"
ACTION_SHUTDOWN = "shutdown"
ACTION_COMMAND = "command"

ACTION_LABELS = {
    ACTION_SOFTWARE: "打开软件",
    ACTION_SLEEP: "系统休眠",
    ACTION_SHUTDOWN: "系统关机",
    ACTION_COMMAND: "执行命令",
}
POWER_ACTIONS = (ACTION_SHUTDOWN, ACTION_SLEEP)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 把程序目录加入 PATH：便于「执行命令」动作直接调用同目录的小工具（如 plink.exe），
# 用户无需写完整路径，换机器部署时也不用改命令。
# 打包后 exe 同目录与 PyInstaller 解压目录（sys._MEIPASS）都加入，
# 保证 plink.exe 无论是随包打进 exe 还是单独放在 exe 旁边都能被找到。
try:
    _dirs = []
    if getattr(sys, "frozen", False):
        _dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        _meipass = getattr(sys, "_MEIPASS", None)
        if _meipass:
            _dirs.append(_meipass)
    else:
        _dirs.append(os.path.dirname(os.path.abspath(__file__)))
    _parts = os.environ.get("PATH", "").split(os.pathsep)
    for _d in _dirs:
        if _d and _d not in _parts:
            os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
            _parts.insert(0, _d)
except Exception:
    pass

SINGLE_INSTANCE_MUTEX = "Global\\BlackoutSentinel_SingleInstance_v1"

# 命令日志脱敏：plink -pw 密码 → -pw ****（密码仍明文存配置文件，仅日志/界面隐藏）
_PW_RE = re.compile(r'(-pw\s+)([^\s|]+)', re.IGNORECASE)


def _redact(text) -> str:
    """隐藏命令中的明文密码（plink -pw xxx）。"""
    try:
        return _PW_RE.sub(r'\1****', str(text))
    except Exception:
        return str(text)


def is_valid_ip(s) -> bool:
    """是否为合法 IPv4 地址。"""
    try:
        ipaddress.IPv4Address(str(s).strip())
        return True
    except Exception:
        return False


# ======================== 路径 ========================
def app_dir() -> str:
    """exe 打包后返回 exe 所在目录，源码运行返回脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


_wd_cache = None


def _writable_dir() -> str:
    """返回一个可写目录：优先 exe 同目录，不可写则用 ProgramData。结果缓存。"""
    global _wd_cache
    if _wd_cache:
        return _wd_cache
    d = app_dir()
    try:
        test = os.path.join(d, ".nw_write_test")
        with open(test, "w") as f:
            f.write("x")
        os.remove(test)
    except Exception:
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        d = os.path.join(pd, "BlackoutSentinel")
        os.makedirs(d, exist_ok=True)
    _wd_cache = d
    return d


def config_path() -> str:
    return os.path.join(_writable_dir(), CONFIG_FILENAME)


def config_bak_path() -> str:
    return config_path() + CONFIG_BAK_SUFFIX


def service_log_path() -> str:
    return os.path.join(_writable_dir(), SERVICE_LOG_FILENAME)


# ======================== 配置读写 ========================
def _read_json_file(p: str):
    """读取 JSON 文件，返回 dict；缺失/损坏返回 None。"""
    try:
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _parse_config(data) -> dict:
    """把原始 dict 规范化为完整配置；data 为 None 时返回默认配置。"""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        for k in DEFAULT_CONFIG:
            if k in data:
                cfg[k] = data[k]
    # 数值钳制
    try:
        cfg["interval"] = max(1, min(int(cfg.get("interval", 2)), 60))
    except Exception:
        cfg["interval"] = DEFAULT_CONFIG["interval"]
    try:
        cfg["threshold"] = max(5, min(int(cfg.get("threshold", 60)), 3600))
    except Exception:
        cfg["threshold"] = DEFAULT_CONFIG["threshold"]
    try:
        cfg["grace_period"] = max(0, min(int(cfg.get("grace_period", 60)), 3600))
    except Exception:
        cfg["grace_period"] = DEFAULT_CONFIG["grace_period"]
    # file_log 容错：任意真值都转 bool（非 bool 类型如 1/"true" 也能识别）
    cfg["file_log"] = bool(cfg.get("file_log", False)) if cfg.get("file_log", False) else False
    cfg["ip"] = str(cfg.get("ip", "")).strip() or DEFAULT_CONFIG["ip"]

    # 备用目标：合法 IPv4、去重、限上限
    targets = []
    raw_targets = data.get("targets") if isinstance(data, dict) else None
    if isinstance(raw_targets, list):
        for t in raw_targets:
            t = str(t).strip()
            if is_valid_ip(t) and t not in targets:
                targets.append(t)
    cfg["targets"] = targets[:MAX_TARGETS]

    # 动作链：按 actions 列表解析；非法动作过滤、wait 钳制、空列表回退默认
    raw_actions = data.get("actions") if isinstance(data, dict) else None
    actions = []
    if isinstance(raw_actions, list):
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            a = item.get("action", "")
            if a not in ACTION_LABELS:
                continue
            try:
                wait = int(item.get("wait_after", 0))
            except (TypeError, ValueError):
                wait = 0
            actions.append({
                "action": a,
                "target": str(item.get("target", "")),
                "wait_after": max(0, min(wait, 600)),
            })
    if not actions:
        actions = [dict(DEFAULT_CONFIG["actions"][0])]
    cfg["actions"] = actions[:MAX_ACTIONS]
    return cfg


def load_config() -> dict:
    """读取配置文件。主文件缺失/损坏 → 尝试 .bak 备份 → 默认配置。"""
    data = _read_json_file(config_path())
    if data is None and os.path.exists(config_bak_path()):
        bak = _read_json_file(config_bak_path())
        if bak is not None:
            try:
                append_file_log(service_log_path(),
                                f"[{datetime.datetime.now()}] ⚠️ 主配置文件损坏/缺失，"
                                f"已从 .bak 备份恢复配置")
            except Exception:
                pass
            data = bak
    return _parse_config(data)


def _config_valid(cfg: dict) -> bool:
    """热加载前的完整合法性检查：非法配置绝不采纳（防止损坏文件冲掉运行中配置）。"""
    try:
        if not is_valid_ip(str(cfg.get("ip", "")).strip()):
            return False
        if not (1 <= int(cfg.get("interval", 0)) <= 60):
            return False
        if not (5 <= int(cfg.get("threshold", 0)) <= 3600):
            return False
        if not (0 <= int(cfg.get("grace_period", -1)) <= 3600):
            return False
        acts = cfg.get("actions")
        if not isinstance(acts, list) or not acts:
            return False
        for a in acts:
            if not isinstance(a, dict) or a.get("action") not in ACTION_LABELS:
                return False
        for t in (cfg.get("targets") or []):
            if not is_valid_ip(str(t).strip()):
                return False
        return True
    except Exception:
        return False


def save_config(cfg: dict) -> bool:
    """原子写配置文件（写临时文件再替换，避免服务读到半截 JSON）。

    写入前把当前有效配置备份到 .bak：主文件日后损坏时可回退，
    避免热加载把运行中的看门狗冲回默认配置。"""
    try:
        p = config_path()
        bak = config_bak_path()
        # 备份上一份有效配置（只有合法 JSON 才配当备份）
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    old = f.read()
                json.loads(old)
                with open(bak, "w", encoding="utf-8") as f:
                    f.write(old)
        except Exception:
            pass
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def effective_targets(cfg: dict) -> list:
    """实际监控的 IP 列表：主 IP + 备用目标，去重、过滤非法。主 IP 非法时兜底默认。"""
    ips = []
    ip = str(cfg.get("ip", "")).strip()
    if is_valid_ip(ip):
        ips.append(ip)
    for t in (cfg.get("targets") or []):
        t = str(t).strip()
        if is_valid_ip(t) and t not in ips:
            ips.append(t)
    if not ips:
        ips.append(DEFAULT_CONFIG["ip"])
    return ips


# ======================== 文件日志 ========================
# 文件日志总闸：默认关闭，不写本地日志文件。由配置 file_log 控制，
# 引擎启动/热加载/GUI 改动时通过 set/refresh 同步。
_file_log_enabled = False


def file_log_enabled() -> bool:
    return _file_log_enabled


def set_file_log_enabled(v) -> bool:
    global _file_log_enabled
    _file_log_enabled = bool(v)
    return _file_log_enabled


def refresh_file_log_switch() -> bool:
    """从配置文件读取 file_log 开关并应用。直接读原始 JSON，
    不走 load_config，避免与配置损坏告警路径递归。"""
    try:
        data = _read_json_file(config_path())
        v = bool(data.get("file_log", False)) if isinstance(data, dict) else False
    except Exception:
        v = False
    set_file_log_enabled(v)
    return v


def append_file_log(path: str, msg: str):
    if not _file_log_enabled:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        if os.path.getsize(path) > MAX_LOG_FILE_BYTES:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-2000:])
    except Exception:
        pass


# ======================== ping ========================
def ping_once(ip: str, timeout_ms: int = PING_TIMEOUT_MS) -> bool:
    """Windows 下 ping 一次，真正收到回声应答（含 TTL=）才返回 True。

    同网段主机不存在时本机协议栈回复「无法访问目标主机」，此时退出码为 0、
    丢失 0%，但实际不通，故以输出中是否含 TTL= 为准。
    """
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), ip],
            capture_output=True,
            encoding="gbk",
            errors="replace",
            timeout=timeout_ms / 1000 + 5,
            creationflags=_NO_WINDOW,
        )
        out = ((result.stdout or "") + (result.stderr or "")).lower()
        return "ttl=" in out
    except Exception:
        return False


def ping_all(ips: list, timeout_ms: int = PING_TIMEOUT_MS, ping_fn=None) -> list:
    """并发 ping 多个目标，返回与 ips 同序的 bool 列表。全部不通才算断网。"""
    ping_fn = ping_fn or ping_once
    if len(ips) <= 1:
        return [ping_fn(ips[0], timeout_ms)]
    try:
        with ThreadPoolExecutor(max_workers=len(ips)) as ex:
            return list(ex.map(lambda ip: ping_fn(ip, timeout_ms), ips))
    except Exception:
        # 线程池异常时退化为串行，绝不因探测手段故障误判
        return [ping_fn(ip, timeout_ms) for ip in ips]


# ======================== 关机/休眠权限与 API ========================
def _enable_shutdown_privilege() -> bool:
    """在当前进程 token 中启用 SeShutdownPrivilege。

    服务账户/非提权进程 token 里该权限可能存在但未启用，
    不启用直接调 ExitWindowsEx 会报 ERROR_PRIVILEGE_NOT_HELD(1314)。
    """
    try:
        import ctypes
        from ctypes import wintypes

        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002
        ERROR_NOT_ALL_ASSIGNED = 1300

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD),
                        ("HighPart", wintypes.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID),
                        ("Attributes", wintypes.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD),
                        ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        # 64 位下必须声明 argtypes，否则 HANDLE 指针按 32 位截断，
        # OpenProcessToken 会返回 ERROR_INVALID_HANDLE(6)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.LookupPrivilegeValueW.argtypes = (
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID))
        advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
        advapi32.AdjustTokenPrivileges.argtypes = (
            wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
            wintypes.DWORD, ctypes.POINTER(TOKEN_PRIVILEGES),
            ctypes.POINTER(wintypes.DWORD))
        advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        h_token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(),
                TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                ctypes.byref(h_token)):
            return False
        try:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(
                    None, "SeShutdownPrivilege", ctypes.byref(luid)):
                return False
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            if not advapi32.AdjustTokenPrivileges(
                    h_token, False, ctypes.byref(tp),
                    ctypes.sizeof(tp), None, None):
                return False
            if kernel32.GetLastError() == ERROR_NOT_ALL_ASSIGNED:
                return False
            return True
        finally:
            kernel32.CloseHandle(h_token)
    except Exception:
        return False


def _run_cmd_capture(cmd: list, timeout: int = 30):
    """同步执行命令并等待结束，返回 (returncode, 合并输出文本)。"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, encoding="gbk", errors="replace",
            timeout=timeout, creationflags=_NO_WINDOW, shell=False,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode, out
    except Exception as e:
        return -1, f"执行异常: {type(e).__name__}: {e}"


def _shutdown_via_api() -> tuple:
    """直接调 ExitWindowsEx 强制关机（shutdown.exe 失败时的降级方案）。

    返回 (ok, 说明文本)。系统已在关机流程中（err 1115/1190）视为成功。
    """
    try:
        import ctypes
        EWX_SHUTDOWN = 0x00000001
        EWX_FORCE = 0x00000004
        EWX_FORCEIFHUNG = 0x00000010
        SHTDN_REASON_MAJOR_OTHER = 0x00000000
        SHTDN_REASON_FLAG_PLANNED = 0x80000000
        flags = EWX_SHUTDOWN | EWX_FORCE | EWX_FORCEIFHUNG
        reason = SHTDN_REASON_MAJOR_OTHER | SHTDN_REASON_FLAG_PLANNED
        if ctypes.windll.user32.ExitWindowsEx(flags, reason):
            return True, ""
        err = ctypes.windll.kernel32.GetLastError()
        if err in (1115, 1190):  # SHUTDOWN_IN_PROGRESS / 已计划关机
            return True, f"关机流程已在进行中(err={err})"
        return False, f"ExitWindowsEx 失败 err={err}"
    except Exception as e:
        return False, f"ExitWindowsEx 异常: {type(e).__name__}: {e}"


def _hibernate_via_api() -> tuple:
    """直接调 SetSuspendState 强制休眠（shutdown /h 失败时的降级方案）。"""
    try:
        import ctypes
        # SetSuspendState(bHibernate=1, bForce=1, bWakeupEventsDisabled=0)
        ctypes.windll.powrprof.SetSuspendState(1, 1, 0)
        return True, ""
    except Exception as e:
        return False, f"SetSuspendState 异常: {type(e).__name__}: {e}"


# ======================== 动作 ========================
def action_brief(actions: list) -> str:
    """动作链的简短描述，用于日志。"""
    parts = []
    for i, a in enumerate(actions, 1):
        label = ACTION_LABELS.get(a.get("action"), a.get("action"))
        parts.append(f"{i}.{label}")
    return " → ".join(parts) if parts else "(无动作)"


def do_action(action: str, target: str = "", log_func=print) -> bool:
    """执行触发动作。任何异常都捕获并记日志，绝不向上抛。

    返回 True/False 表示动作是否成功（关机/休眠/命令以真实结果判定），
    供动作链决定是否需要重试。
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_target = _redact(target)
    try:
        if action == ACTION_SOFTWARE:
            if not target:
                log_func(f"[{now}] ⚠️ 软件路径为空，未执行")
                return False
            try:
                os.startfile(target)
            except Exception:
                try:
                    subprocess.Popen(f'start "" "{target}"', shell=True,
                                     creationflags=_NO_WINDOW)
                except Exception as e:
                    log_func(f"[{now}] ❌ 打开软件失败: {safe_target}；{type(e).__name__}: {e}")
                    return False
            log_func(f"[{now}] ✅ 已打开软件: {safe_target}")
            return True
        elif action == ACTION_SLEEP:
            _enable_shutdown_privilege()
            rc, out = _run_cmd_capture(["shutdown", "/h", "/f"])
            if rc == 0:
                log_func(f"[{now}] ✅ 已触发系统休眠")
                return True
            ok, msg = _hibernate_via_api()
            if ok:
                log_func(f"[{now}] ✅ 休眠命令返回 {rc}（{out}），"
                         f"已通过系统 API 强制休眠")
                return True
            log_func(f"[{now}] ❌ 休眠失败: shutdown rc={rc} {out or '(无输出)'}；{msg}")
            return False
        elif action == ACTION_SHUTDOWN:
            priv = _enable_shutdown_privilege()
            rc, out = _run_cmd_capture(["shutdown", "/s", "/f", "/t", "0"])
            # rc==0：关机指令已受理（系统随即开始关机，本进程很快被结束）
            # rc==1190：系统已有计划中的关机，关机必定会发生，视为成功
            if rc == 0 or rc == 1190:
                log_func(f"[{now}] ✅ 已触发系统关机（shutdown.exe rc={rc}，"
                         f"关机权限: {'已启用' if priv else '未启用/无需'}）")
                return True
            ok, msg = _shutdown_via_api()
            if ok:
                log_func(f"[{now}] ✅ shutdown.exe 失败(rc={rc}, {out or '无输出'})，"
                         f"已通过系统 API 强制关机；{msg}".rstrip("；"))
                return True
            log_func(f"[{now}] ❌ 关机彻底失败: shutdown.exe rc={rc} "
                     f"{out or '(无输出)'}；{msg}。请检查账户关机权限/组策略/杀软拦截")
            return False
        elif action == ACTION_COMMAND:
            if not target:
                log_func(f"[{now}] ⚠️ 命令为空，未执行")
                return False
            # shell=True 走 cmd：支持管道（echo y | plink ...）与重定向；
            # app_dir 已加入 PATH，可直接调用同目录 plink 等。
            # 同步等待真实结果：plink 关 NAS 失败必须知道，不能 fire-and-forget。
            try:
                r = subprocess.run(
                    target, shell=True,
                    capture_output=True, encoding="gbk", errors="replace",
                    creationflags=_NO_WINDOW)
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                tail = out[-300:] if out else "(无输出)"
                if r.returncode == 0:
                    log_func(f"[{now}] ✅ 命令执行成功(rc=0): {safe_target}"
                             + (f"；输出: {tail}" if out else ""))
                    return True
                log_func(f"[{now}] ❌ 命令返回非零(rc={r.returncode}): {safe_target}；输出: {tail}")
                return False
            except Exception as e:
                log_func(f"[{now}] ❌ 命令执行异常: {safe_target}；{type(e).__name__}: {e}")
                return False
        else:
            log_func(f"[{now}] ⚠️ 未知动作类型: {action}")
            return False
    except Exception as e:
        log_func(f"[{now}] ❌ 动作执行失败: {type(e).__name__}: {e}")
        return False


def run_action_chain(actions: list, log_func=print,
                     sleep_fn=None, stop_check=None) -> dict:
    """按顺序执行动作链。

    - 单个动作失败不中断后续动作（例如 NAS 关不上也不能耽误本机关机）
    - 每个动作执行后按 wait_after 等待，等待期间可被 stop_check 打断
    - sleep_fn / stop_check 可注入（引擎传入可中断睡眠与停止事件）
    返回 {"ok": 所有动作是否都成功, "power_ok": 关机/休眠类动作是否都成功}；
    power_ok=False 时引擎会按 POWEROFF_RETRY_INTERVAL 重试整条链。
    """
    if sleep_fn is None:
        sleep_fn = _sleep
    if stop_check is None:
        stop_check = lambda: False

    result = {"ok": True, "power_ok": True}
    total = len(actions)
    for idx, item in enumerate(actions, 1):
        if stop_check():
            log_func(f"⏹ 动作链在第 {idx - 1} 个动作后被停止")
            return result
        a = item.get("action", "")
        target = item.get("target", "")
        wait = int(item.get("wait_after", 0) or 0)
        label = ACTION_LABELS.get(a, a)
        if total > 1:
            extra = f"（{_redact(target)}）" if target and a in (ACTION_SOFTWARE, ACTION_COMMAND) else ""
            log_func(f"⚡ 执行动作 {idx}/{total}: {label}{extra}")
        ok = do_action(a, target, log_func)
        if not ok:
            result["ok"] = False
            if a in POWER_ACTIONS:
                result["power_ok"] = False
        if wait > 0 and idx < total:
            log_func(f"⏳ 等待 {wait} 秒后执行下一个动作...")
            end = _now() + wait
            while _now() < end and not stop_check():
                sleep_fn(min(0.5, max(0.0, end - _now())))
    return result


# ======================== 单实例 ========================
def acquire_single_instance():
    """获取全局单实例互斥锁。返回句柄（持有）；已有实例返回 None；异常退化返回 -1。

    使用 Global\\ 前缀，跨会话（服务 session 0 与用户会话）互斥。
    互斥体设置 NULL DACL：普通用户会话默认无 SeCreateGlobalPrivilege，
    不放开权限时无法打开 SYSTEM 创建的 Global\\ 对象，互斥会静默失效导致双跑。
    句柄由进程持有，进程退出自动释放。
    """
    try:
        import win32event
        import win32api
        import win32security
        import winerror
        mutex = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            try:
                win32api.CloseHandle(mutex)
            except Exception:
                pass
            return None
        # NULL DACL = 任何用户都可打开该内核对象（跨 session 互斥才真正生效）
        try:
            sd = win32security.SECURITY_DESCRIPTOR()
            sd.SetSecurityDescriptorDacl(1, None, 0)
            win32security.SetSecurityInfo(
                mutex, win32security.SE_KERNEL_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
                None, None, None, None)
        except Exception:
            pass
        return mutex
    except Exception:
        # 非 Windows / pywin32 异常时不阻塞运行，退化为不互斥
        return -1


# ======================== 监控引擎 ========================
class MonitorEngine:
    """监控引擎。GUI / 服务 / 后台共用。

    - GUI：update_config() 实时改内存并落盘，立即生效
    - 服务/后台：每轮轮询配置文件 mtime，变化且合法才热加载（非法配置保留旧值）
    """

    def __init__(self, log_func=print, file_log_path: str = None,
                 run_in_service: bool = False):
        self.log = log_func
        self.file_log_path = file_log_path
        self.run_in_service = run_in_service
        self.stop_event = threading.Event()
        self._cfg_lock = threading.Lock()
        self._cfg = load_config()
        set_file_log_enabled(self._cfg.get("file_log", False))
        self._cfg_path = config_path()
        try:
            self._cfg_mtime = os.path.getmtime(self._cfg_path)
        except Exception:
            self._cfg_mtime = 0
        self.ping_fn = ping_once
        # 状态回调（GUI 用来更新状态标签）
        self.on_fail_seconds = None   # callback(int seconds)
        self.on_last_result = None   # callback(str)

    # ---------- 配置 ----------
    def get_config(self) -> dict:
        with self._cfg_lock:
            return dict(self._cfg)

    def update_config(self, **kw) -> dict:
        """GUI 实时更新：合并内存配置并落盘。返回更新后的配置。"""
        with self._cfg_lock:
            for k, v in kw.items():
                if k in DEFAULT_CONFIG:
                    self._cfg[k] = v
            cfg = dict(self._cfg)
        set_file_log_enabled(cfg.get("file_log", False))
        save_config(cfg)
        try:
            self._cfg_mtime = os.path.getmtime(self._cfg_path)
        except Exception:
            pass
        return cfg

    def _reload_if_changed(self):
        """服务/后台模式：轮询配置文件，变化且【合法】才热加载。

        返回 (new_ip_or_None, changed_bool)：changed 表示监控目标集合发生变化
        （主 IP 或备用目标），需重置失败计时。非法配置直接忽略并告警，
        绝不让损坏文件把运行中的看门狗冲回默认配置。"""
        try:
            m = os.path.getmtime(self._cfg_path)
        except Exception:
            return None, False
        if m <= self._cfg_mtime:
            return None, False
        self._cfg_mtime = m
        old = self.get_config()
        new = load_config()
        if not _config_valid(new):
            self.log("❌ 配置文件内容非法（IP/数值/动作链异常），热加载已忽略，"
                     "继续使用上一份有效配置，请检查配置文件")
            return None, False
        with self._cfg_lock:
            self._cfg = new
        set_file_log_enabled(new.get("file_log", False))
        old_ip = old.get("ip")
        new_ip = new.get("ip")
        targets_changed = old.get("targets") != new.get("targets")
        changes = []
        if old_ip != new_ip:
            changes.append(f"监控目标 {old_ip} → {new_ip}")
        if targets_changed:
            changes.append(f"备用目标 → {new.get('targets') or '无'}")
        if old.get("interval") != new.get("interval"):
            changes.append(f"间隔 → {new.get('interval')} 秒")
        if old.get("threshold") != new.get("threshold"):
            changes.append(f"阈值 → {new.get('threshold')} 秒")
        if old.get("grace_period") != new.get("grace_period"):
            changes.append(f"宽限期 → {new.get('grace_period')} 秒")
        if old.get("actions") != new.get("actions"):
            changes.append(f"动作链 → {action_brief(new.get('actions', []))}")
        if changes:
            self.log("🔄 配置文件已变更，热加载: " + "；".join(changes))
        return new_ip, (old_ip != new_ip or targets_changed)

    # ---------- 日志 ----------
    def _log(self, msg: str):
        try:
            self.log(msg)
        except Exception:
            pass
        if self.file_log_path:
            append_file_log(self.file_log_path, msg)

    # ---------- 控制 ----------
    def stop(self):
        self.stop_event.set()

    def _sleep(self, seconds: float):
        end = _now() + seconds
        while _now() < end and not self.stop_event.is_set():
            _sleep(0.2)

    # ---------- 主循环 ----------
    def run(self):
        fail_start = None
        triggered = False
        power_failed = False        # 上次动作链中关机/休眠是否失败
        chain_count = 0             # 动作链执行次数（首次触发 + 重试）
        last_chain_ts = 0
        last_grace_warn = 0
        last_ip = None
        last_targets = None
        start_ts = _now()
        t0 = _now()
        last_loop_ts = _now()

        self._log(f"🛡️ 监控引擎启动（{'服务/后台模式' if self.run_in_service else 'GUI 模式'}），"
                  f"配置文件: {self._cfg_path}")

        while not self.stop_event.is_set():
            try:
                # 服务/后台模式热加载配置文件；GUI 模式配置已在内存实时更新
                if self.run_in_service:
                    new_ip_file, targets_changed_file = self._reload_if_changed()
                else:
                    new_ip_file, targets_changed_file = None, False

                cfg = self.get_config()
                new_ip = str(cfg.get("ip", "")).strip()
                targets = effective_targets(cfg)
                interval = int(cfg.get("interval", 2))
                threshold = int(cfg.get("threshold", 60))
                grace = int(cfg.get("grace_period", 60))
                actions = cfg.get("actions") or [dict(DEFAULT_CONFIG["actions"][0])]

                # 目标集合切换检测（GUI 内存变更 或 服务文件变更）：重置失败计时
                targets_changed = (last_ip is not None and
                                   (new_ip != last_ip or targets != last_targets))
                if targets_changed or targets_changed_file:
                    if fail_start is not None or triggered or last_ip is not None:
                        self._log(f"🔀 监控目标切换为 {targets}，失败计时已重置")
                    fail_start = None
                    triggered = False
                    power_failed = False
                    chain_count = 0
                    if self.on_fail_seconds:
                        try: self.on_fail_seconds(0)
                        except Exception: pass
                last_ip = new_ip
                last_targets = targets

                t0 = _now()
                results = ping_all(targets, PING_TIMEOUT_MS, ping_fn=self.ping_fn)
                any_ok = any(results)
                ok_ip = next((ip for ip, ok in zip(targets, results) if ok), None)
                now = datetime.datetime.now().strftime("%H:%M:%S")

                # 休眠/挂起检测（放在 ping 之后）：上一轮结束到本轮 ping 返回
                # 的墙钟间隔 >30 秒，说明系统休眠/挂起，本轮结果作废、重置计时。
                gap_after = _now() - last_loop_ts
                if gap_after > 30 or gap_after < -30:
                    self._log(f"⏰ 检测到系统休眠/挂起（间隔 {int(gap_after)} 秒），"
                              f"本轮结果作废、失败计时已重置")
                    fail_start = None
                    triggered = False
                    power_failed = False
                    chain_count = 0
                    if self.on_fail_seconds:
                        try: self.on_fail_seconds(0)
                        except Exception: pass
                    last_loop_ts = _now()
                    continue

                if any_ok:
                    if fail_start is not None:
                        self._log(f"[{now}] ✅ 网络已恢复连通（{ok_ip} 可达）")
                    fail_start = None
                    triggered = False
                    power_failed = False
                    chain_count = 0
                    if self.on_fail_seconds:
                        try: self.on_fail_seconds(0)
                        except Exception: pass
                    if self.on_last_result:
                        try: self.on_last_result(f"最近: ✅ {now} 通（{ok_ip}）")
                        except Exception: pass
                else:
                    if fail_start is None:
                        fail_start = t0
                        self._log(f"[{now}] ⚠️ 所有监控目标均不通（{len(targets)} 个: "
                                  f"{', '.join(targets)}），开始累计...")
                    fail_secs = int(_now() - fail_start)
                    if fail_secs < 0:
                        fail_start = t0
                        fail_secs = 0
                    if self.on_fail_seconds:
                        try: self.on_fail_seconds(fail_secs)
                        except Exception: pass
                    if self.on_last_result:
                        try: self.on_last_result(f"最近: ❌ {now} 全部不通")
                        except Exception: pass

                    in_grace = (_now() - start_ts) < grace
                    if fail_secs >= threshold and not triggered:
                        if in_grace:
                            # 启动宽限期：开机网络未就绪保护，期内只记录不触发
                            if _now() - last_grace_warn >= GRACE_WARN_INTERVAL:
                                remain = int(grace - (_now() - start_ts))
                                self._log(f"[{now}] 🔒 连续不通已达 {fail_secs} 秒，"
                                          f"但处于启动宽限期（剩余 {remain} 秒），暂不触发动作链")
                                last_grace_warn = _now()
                        else:
                            tt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            self._log(f"[{tt}] 🚨 连续不通已达 {threshold} 秒，触发动作链 "
                                      f"（共 {len(actions)} 个）: {action_brief(actions)}")
                            res = run_action_chain(
                                actions, self._log,
                                sleep_fn=lambda s: self._sleep(s),
                                stop_check=lambda: self.stop_event.is_set())
                            triggered = True
                            chain_count = 1
                            last_chain_ts = _now()
                            power_failed = not res.get("power_ok", True)
                            if power_failed:
                                self._log(f"⚠️ 关机/休眠动作未成功，网络仍不通时将在 "
                                          f"{POWEROFF_RETRY_INTERVAL} 秒后重试动作链")
                    elif (triggered and power_failed
                          and _now() - last_chain_ts >= POWEROFF_RETRY_INTERVAL):
                        # 关机/休眠失败后的重试：看门狗的使命是确保机器关停，
                        # 失败一次绝不能放弃（关机命令幂等，重复执行安全）
                        chain_count += 1
                        self._log(f"🔁 第 {chain_count} 次执行动作链"
                                  f"（上次关机/休眠未成功，网络仍不通）...")
                        res = run_action_chain(
                            actions, self._log,
                            sleep_fn=lambda s: self._sleep(s),
                            stop_check=lambda: self.stop_event.is_set())
                        last_chain_ts = _now()
                        power_failed = not res.get("power_ok", True)
                        if not power_failed:
                            self._log("✅ 重试后关机/休眠动作已受理")

            except Exception as e:
                try:
                    self._log(f"⚠️ 监控循环异常（已自动恢复）: {type(e).__name__}: {e}")
                except Exception:
                    pass
                self._sleep(2)
                continue

            elapsed = _now() - t0
            self._sleep(max(0.0, interval - elapsed))
            last_loop_ts = _now()

        self._log("监控引擎已停止")


def run_headless(file_log: str = None, run_in_service: bool = True):
    """后台/服务模式入口：持有单实例锁，supervisor 循环跑引擎直到进程结束。

    引擎 run() 正常情况下不返回（stop_event 由服务停止触发）；
    万一引擎整体崩溃退出，等待后自动重启，进程不死、监控不停。"""
    log_path = file_log or service_log_path()

    def _log(m):
        append_file_log(log_path, m)

    refresh_file_log_switch()
    mutex = acquire_single_instance()
    if mutex is None:
        _log(f"[{datetime.datetime.now()}] 已有监控实例在运行，本实例退出")
        return None
    restarts = 0
    eng = None
    while True:
        try:
            # log_func 传 no-op：文件日志统一由 file_log_path 写，
            # 避免 log_func 与 file_log_path 各写一遍导致日志重复落盘
            eng = MonitorEngine(log_func=lambda m: None, file_log_path=log_path,
                                run_in_service=run_in_service)
            eng.run()
            return eng  # 正常停止（stop_event）
        except KeyboardInterrupt:
            try:
                if eng:
                    eng.stop()
            except Exception:
                pass
            _log(f"[{datetime.datetime.now()}] 收到中断信号，监控停止")
            return eng
        except Exception as e:
            restarts += 1
            _log(f"[{datetime.datetime.now()}] 💥 监控引擎整体崩溃"
                 f"（第 {restarts} 次）: {type(e).__name__}: {e}，5 秒后重启引擎")
            try:
                time.sleep(5)
            except Exception:
                pass
            if restarts >= 10:
                # 持续性故障：拉长重试间隔，避免疯狂空转，永不放弃
                _log(f"[{datetime.datetime.now()}] ⚠️ 引擎已连续崩溃 {restarts} 次，"
                     f"降级为 60 秒间隔重试")
                try:
                    time.sleep(55)
                except Exception:
                    pass
