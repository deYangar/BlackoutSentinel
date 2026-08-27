# -*- coding: utf-8 -*-
"""断电哨兵 Blackout Sentinel 冒烟测试：配置持久化 + 宽限期 + 多目标仲裁 + 关机失败重试 +
热加载容错 + 命令同步/超时 + 密码脱敏 + 单实例 + 自启状态。

所有读写都隔离在临时目录（monkeypatch core._wd_cache），不污染真实配置。
"""
import os
import sys
import io
import time
import json
import shutil
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sentinel_core as core
import sentinel_autostart as autostart

# ---- 临时目录隔离：配置/日志/备份全部落到临时目录，绝不碰真实配置 ----
TMP = tempfile.mkdtemp(prefix="nw_smoke_")
core._wd_cache = TMP

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail, flush=True)


def marker_path(name):
    return os.path.join(TMP, name)


def wait_marker(name, timeout=12):
    p = marker_path(name)
    end = time.time() + timeout
    while time.time() < end and not os.path.exists(p):
        time.sleep(0.2)
    return os.path.exists(p)


# ================= 1. 配置读写/容错 =================
cfg = core.load_config()
check("默认配置加载", cfg["ip"] == "192.168.1.1" and cfg["interval"] == 2
      and cfg["threshold"] == 60 and cfg["grace_period"] == 60
      and cfg["targets"] == [])

cfg["ip"] = "10.0.0.5"
cfg["threshold"] = 45
cfg["targets"] = ["10.0.0.6"]
cfg["actions"] = [{"action": "shutdown", "target": "", "wait_after": 0}]
check("配置保存", core.save_config(cfg))
cfg2 = core.load_config()
check("配置回读一致(含备用目标)", cfg2["ip"] == "10.0.0.5" and cfg2["threshold"] == 45
      and cfg2["targets"] == ["10.0.0.6"]
      and cfg2["actions"][0]["action"] == "shutdown")

# 多动作链 + wait_after
cfg2["actions"] = [
    {"action": "command", "target": "echo first", "wait_after": 2},
    {"action": "command", "target": "echo second", "wait_after": 0},
]
core.save_config(cfg2)
cfg3 = core.load_config()
check("多动作链读写", len(cfg3["actions"]) == 2 and cfg3["actions"][0]["wait_after"] == 2
      and cfg3["actions"][0]["action"] == "command")

# .bak 备份在保存时生成
check("保存时生成 .bak 备份", os.path.exists(core.config_bak_path()))

# 损坏配置：主文件损坏时从 .bak 恢复（不回退默认 192.168.1.1）
bak_ip = None
bak_data = core._read_json_file(core.config_bak_path())
if bak_data:
    bak_ip = bak_data.get("ip")
with open(core.config_path(), "w", encoding="utf-8") as f:
    f.write("{ broken json !!!")
cfg_broken = core.load_config()
check("损坏配置从 .bak 恢复而非默认",
      cfg_broken["ip"] != core.DEFAULT_CONFIG["ip"] or bak_ip is not None,
      f"bak_ip={bak_ip} got={cfg_broken['ip']}")
core.save_config(core.DEFAULT_CONFIG)

# ================= 2. 数值钳制 / IP 校验 / 目标解析 =================
check("is_valid_ip 合法", core.is_valid_ip("192.168.1.1") and core.is_valid_ip("10.0.0.255"))
check("is_valid_ip 非法", not core.is_valid_ip("999.1.1.1")
      and not core.is_valid_ip("abc") and not core.is_valid_ip("1.2.3"))

with open(core.config_path(), "w", encoding="utf-8") as f:
    json.dump({"ip": "10.0.0.9", "interval": 999, "threshold": 2,
               "grace_period": 99999,
               "targets": ["bad", "10.0.0.10", "10.0.0.10", "10.0.0.11", "10.0.0.12"]}, f)
cc = core.load_config()
check("数值钳制(interval/threshold/grace)",
      cc["interval"] == 60 and cc["threshold"] == 5
      and cc["grace_period"] == 3600)
check("备用目标过滤非法/去重/限上限2个",
      cc["targets"] == ["10.0.0.10", "10.0.0.11"], str(cc["targets"]))
core.save_config(core.DEFAULT_CONFIG)

# effective_targets：去重、过滤非法、主 IP 非法时兜底默认
et = core.effective_targets({"ip": "10.0.0.1", "targets": ["10.0.0.2", "10.0.0.1", "bad"]})
check("effective_targets 去重过滤", et == ["10.0.0.1", "10.0.0.2"], str(et))
et2 = core.effective_targets({"ip": "bad", "targets": []})
check("effective_targets 非法主IP兜底默认", et2 == [core.DEFAULT_CONFIG["ip"]], str(et2))

# ================= 3. 密码脱敏 =================
check("密码脱敏 plink -pw",
      core._redact('echo y | plink root@1.2.3.4 -pw s3cr3t "poweroff"')
      .find("s3cr3t") == -1 and "****" in core._redact('plink -pw s3cr3t'))
check("脱敏不误伤普通命令", core._redact("shutdown /s /f") == "shutdown /s /f")

# ================= 3b. 文件日志开关（默认关，不写本地文件）=================
core.set_file_log_enabled(False)
flog = os.path.join(TMP, "fswitch.log")
if os.path.exists(flog):
    os.remove(flog)
core.append_file_log(flog, "should_not_write")
check("开关关时不写文件日志", not os.path.exists(flog))
core.set_file_log_enabled(True)
core.append_file_log(flog, "should_write_line")
check("开关开后写文件日志", os.path.exists(flog)
      and "should_write_line" in io.open(flog, encoding="utf-8").read())
# 引擎日志同样受开关控制
elog = os.path.join(TMP, "eng.log")
if os.path.exists(elog):
    os.remove(elog)
core.set_file_log_enabled(False)
eng_f = core.MonitorEngine(log_func=lambda m: None, file_log_path=elog, run_in_service=True)
eng_f._log("eng_not_written")
check("引擎日志开关关时不落盘", not os.path.exists(elog))
core.set_file_log_enabled(True)
eng_f._log("eng_written")
check("引擎日志开关开时落盘", os.path.exists(elog))
core.set_file_log_enabled(False)
# 配置持久化 + refresh 读取
cfg_f = core.load_config()
cfg_f["file_log"] = True
core.save_config(cfg_f)
check("file_log 配置回读为 True", core.load_config()["file_log"] is True)
check("refresh 从配置读到开关 True", core.refresh_file_log_switch() is True)
core.set_file_log_enabled(False)
core.save_config(core.DEFAULT_CONFIG)
check("默认配置 file_log 为 False", core.DEFAULT_CONFIG["file_log"] is False)

# ================= 4. ping 判据 / 多目标仲裁 =================
check("ping 127.0.0.1 通", core.ping_once("127.0.0.1") is True)
check("ping 192.0.2.1 不通", core.ping_once("192.0.2.1") is False)
r = core.ping_all(["192.0.2.1", "127.0.0.1", "192.0.2.2"],
                  ping_fn=lambda ip, t=1000: ip == "127.0.0.1")
check("ping_all 多目标结果同序", r == [False, True, False], str(r))
r2 = core.ping_all(["192.0.2.1", "192.0.2.2"], ping_fn=lambda ip, t=1000: False)
check("ping_all 全不通", r2 == [False, False])

# ================= 5. 动作链：命令同步 + 关机失败返回 =================
m_a = marker_path("chain_a.txt")
m_b = marker_path("chain_b.txt")
for _m in (m_a, m_b):
    if os.path.exists(_m):
        os.remove(_m)
t0 = time.time()
# 两动作链：第 1 个动作 wait_after=1（最后一个动作不等待是设计行为）
res_ok = core.run_action_chain(
    [{"action": core.ACTION_COMMAND, "target": f'cmd /c echo a > "{m_a}"', "wait_after": 1},
     {"action": core.ACTION_COMMAND, "target": f'cmd /c echo b > "{m_b}"', "wait_after": 0}],
    log_func=lambda m: None)
check("命令动作同步执行成功", wait_marker("chain_a.txt", 5) and wait_marker("chain_b.txt", 5)
      and res_ok["ok"] is True)
check("wait_after 生效(动作间等待)", time.time() - t0 >= 1)

# 关机动作失败 → power_ok=False（monkeypatch do_action 模拟关机彻底失败）
orig_do = core.do_action
core.do_action = lambda a, t="", log_func=print: (a not in core.POWER_ACTIONS)
res_pw = core.run_action_chain(
    [{"action": core.ACTION_COMMAND, "target": "echo x", "wait_after": 0},
     {"action": core.ACTION_SHUTDOWN, "target": "", "wait_after": 0}],
    log_func=lambda m: None)
check("关机失败时 power_ok=False 且命令动作仍 ok",
      res_pw["power_ok"] is False and res_pw["ok"] is False)
core.do_action = orig_do

# ================= 6. 引擎：宽限期内不触发，宽限期后触发 =================
m_grace = marker_path("grace_marker.txt")
if os.path.exists(m_grace):
    os.remove(m_grace)
logs_g = []
eng_g = core.MonitorEngine(log_func=lambda m: logs_g.append(m),
                           file_log_path=None, run_in_service=True)
eng_g.ping_fn = lambda ip, t=1000: False
eng_g.update_config(ip="198.51.100.1", interval=1, threshold=1, grace_period=3,
                    actions=[{"action": core.ACTION_COMMAND,
                              "target": f'cmd /c echo g > "{m_grace}"', "wait_after": 0}])
th_g = threading.Thread(target=eng_g.run, daemon=True)
th_g.start()
time.sleep(2.0)
check("宽限期内达阈值不触发", not os.path.exists(m_grace))
check("宽限期内有提示日志", any("宽限期" in m for m in logs_g), str([m for m in logs_g if "宽限" in m]))
check("宽限期后触发动作", wait_marker("grace_marker.txt", 8))
eng_g.stop()
th_g.join(timeout=5)

# ================= 7. 引擎：多目标仲裁，任一通则不触发 =================
m_arb = marker_path("arb_marker.txt")
if os.path.exists(m_arb):
    os.remove(m_arb)
eng_a = core.MonitorEngine(log_func=lambda m: None, file_log_path=None, run_in_service=True)
# 主目标不通，备用目标通 → 网络视为正常
eng_a.ping_fn = lambda ip, t=1000: (ip == "10.1.1.2")
eng_a.update_config(ip="10.1.1.1", interval=1, threshold=2, grace_period=0,
                    targets=["10.1.1.2"],
                    actions=[{"action": core.ACTION_COMMAND,
                              "target": f'cmd /c echo x > "{m_arb}"', "wait_after": 0}])
th_a = threading.Thread(target=eng_a.run, daemon=True)
th_a.start()
time.sleep(4.0)
check("多目标任一通则不触发", not os.path.exists(m_arb))
# 备用目标也断开 → 全不通 → 触发
eng_a.ping_fn = lambda ip, t=1000: False
check("多目标全不通才触发", wait_marker("arb_marker.txt", 8))
eng_a.stop()
th_a.join(timeout=5)

# ================= 8. 引擎：关机失败后自动重试 =================
logs_r = []
eng_r = core.MonitorEngine(log_func=lambda m: logs_r.append(m),
                           file_log_path=None, run_in_service=True)
eng_r.ping_fn = lambda ip, t=1000: False
# 前两次关机失败，第三次成功
state = {"calls": 0}
def fake_do(a, t="", log_func=print):
    if a in core.POWER_ACTIONS:
        state["calls"] += 1
        return state["calls"] >= 3   # 第 3 次才成功
    return True
core.do_action = fake_do
_orig_retry = core.POWEROFF_RETRY_INTERVAL
core.POWEROFF_RETRY_INTERVAL = 2   # 测试缩短重试间隔
eng_r.update_config(ip="198.51.100.9", interval=1, threshold=1, grace_period=0,
                    actions=[{"action": core.ACTION_SHUTDOWN, "target": "", "wait_after": 0}])
th_r = threading.Thread(target=eng_r.run, daemon=True)
th_r.start()
time.sleep(2 * 2 + 8)
eng_r.stop()
th_r.join(timeout=5)
core.do_action = orig_do
core.POWEROFF_RETRY_INTERVAL = _orig_retry
retry_logs = [m for m in logs_r if "次执行动作链" in m or "重试" in m]
check("关机失败后自动重试至成功", state["calls"] >= 3 and len(retry_logs) >= 1,
      f"calls={state['calls']} retry_logs={len(retry_logs)}")

# ================= 9. 热加载：非法配置被忽略 =================
eng_h = core.MonitorEngine(log_func=lambda m: None, file_log_path=None, run_in_service=True)
eng_h.update_config(ip="198.51.100.50", interval=1, threshold=999,
                    actions=[{"action": core.ACTION_COMMAND, "target": "echo x", "wait_after": 0}])
time.sleep(0.2)
with open(core.config_path(), "w", encoding="utf-8") as f:
    json.dump({"ip": "not_an_ip", "interval": 2, "threshold": 60,
               "actions": [{"action": "shutdown", "target": "", "wait_after": 0}]}, f)
new_ip, changed = eng_h._reload_if_changed()
check("非法配置热加载被忽略(保持旧IP)", eng_h.get_config()["ip"] == "198.51.100.50",
      eng_h.get_config()["ip"])
eng_h.stop()
core.save_config(core.DEFAULT_CONFIG)

# ================= 10. 单实例互斥 =================
m1 = core.acquire_single_instance()
check("第一个实例获得互斥锁", m1 is not None)
m2 = core.acquire_single_instance()
check("第二个实例被互斥(None)", m2 is None)

# ================= 11. 自启方式状态枚举（只读，不安装） =================
for name, label, admin in autostart.METHODS:
    st = autostart.method_status(name)
    print(f"   自启方式 {name}: 已安装={st} 需管理员={admin}")
check("自启方式共5种", len(autostart.METHODS) == 5)

# boot-task XML 生成包含关键策略
xml = autostart._boot_task_xml()
check("boot-task XML 含失败重启", "RestartOnFailure" in xml and "<Count>3</Count>" in xml)
check("boot-task XML 电池供电运行", "DisallowStartIfOnBatteries>false" in xml
      and "StopIfGoingOnBatteries>false" in xml)
check("boot-task XML 无执行时长限制", "ExecutionTimeLimit>PT0S" in xml)

# ================= 12. pywin32 服务组件可导入 =================
try:
    import win32serviceutil
    import servicemanager
    import win32event
    import win32security
    check("pywin32 服务/安全组件可导入", True)
except Exception as e:
    check("pywin32 服务/安全组件可导入", False, str(e))

# ================= 汇总 =================
print("\n==== 结果 ====", flush=True)
fails = [r for r in results if not r[1]]
for n, ok, d in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {n} {d}", flush=True)
print(f"\n共 {len(results)} 项，失败 {len(fails)} 项", flush=True)

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
