# -*- coding: utf-8 -*-
"""
联网_service.py — Dr.COM 校园网自动登录（Web UI 配置版 v1.3.2）

架构
    主线程：阻塞在 ThreadingHTTPServer 上，提供 Web UI 与 REST API。
    守护线程 1：启动触发 — service start 后立即执行一次 run_once('startup')。
    守护线程 2：周期自检 — auto_check_enabled=True 时按间隔（含退避）循环执行 run_once('periodic')。
    信号处理：SIGTERM / SIGBREAK / SIGINT 触发优雅退出（设置 STOP_EVENT）。

线程模型
    STATE / BACKOFF 由独立 Lock 保护；run_once 全程持 RUN_LOCK，保证周期与手动触发串行。

依赖
    仅 Python 3 标准库（http.server / json / threading / signal / urllib / re）。
    无第三方依赖。

文件布局（脚本所在目录）
    config.json         — 运行配置（自动生成，含默认值）
    password.txt        — 校园网密码（UTF-8，第一行；缺失则跳过登录）
    logs/campus_login.log  — 业务日志
    logs/service_stdout.log — NSSM stdout（由 install.bat 配置）
    logs/service_stderr.log — NSSM stderr（由 install.bat 配置）
"""

import hashlib
import io
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ============================================================
# 常量
# ============================================================
VERSION = "1.3.2"
BACKOFF_LEVELS = [5, 10, 20, 40, 60]  # 分钟，索引 = 连续失败次数，封顶 60

DEFAULT_CONFIG = {
    "host": "172.16.80.3",
    "port": 80,
    "account": "",
    "suffix": "",
    "auto_check_enabled": True,
    "auto_check_interval_min": 30,
    "network_wait_timeout_sec": 60,
    "ui_port": 8848,
    # —— 自动升级字段（v1.3 新增）——
    "auto_update_enabled": True,
    "update_check_interval_hours": 6,
    "update_min_free_disk_mb": 200,
}

ALLOWED_SUFFIXES = ("", "@yd", "@dx", "@lt")
ALLOWED_INTERVALS = (5, 15, 30, 60, 120)
ALLOWED_UPDATE_INTERVALS = (6, 12, 24)

# —— 升级常量 ——
GITHUB_REPO = "TSS-Small-sunshine/DrcomAutoLogin-Windows"
GITHUB_RELEASES_API = "https://api.github.com/repos/{}/releases/latest".format(GITHUB_REPO)
GITHUB_API_VERSION = "2022-11-28"
GITHUB_UA = "DrcomAutoLogin-Windows/{}".format(VERSION)
HTTP_TIMEOUT_SEC = 8
DOWNLOAD_CHUNK_BYTES = 64 * 1024  # 64KB
DOWNLOAD_TIMEOUT_SEC = 300  # 5 分钟
INSTALLER_FILENAME_PATTERN = "DrcomAutoLogin-Setup-v{ver}.exe"
NSSM_REGISTRY_PATH = r"HKLM\SYSTEM\CurrentControlSet\Services\DrcomAutoLogin"
NSSM_PARAMETERS_PATH = NSSM_REGISTRY_PATH + r"\Parameters"
SERVICE_NAME = "DrcomAutoLogin"
UPGRADE_HISTORY_MAX_LINES = 50
UPGRADE_SUCCESS_TTL_SEC = 5 * 60  # 成功后绿 banner 仅保留 5 分钟
BACKUP_RETENTION_DAYS = 7

# —— 配置导入/导出（zip）——
CONFIG_EXPORT_SCHEMA_VERSION = 1
CONFIG_EXPORT_TOOL = "DrcomAutoLogin-Windows"
CONFIG_IMPORT_MAX_BYTES = 4 * 1024 * 1024  # 4MB 安全上限


# ============================================================
# 路径解析
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PASSWORD_FILE = os.path.join(BASE_DIR, "password.txt")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "campus_login.log")
# —— 升级相关路径 ——
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
NSSM_PATH = os.path.join(TOOLS_DIR, "nssm.exe")
UPGRADE_LOG_FILE = os.path.join(LOG_DIR, "upgrade.log")

# 启动时确保日志目录存在（幂等）
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError as exc:
    sys.stderr.write("无法创建日志目录 {}: {}\n".format(LOG_DIR, exc))


# ============================================================
# 共享状态（所有访问须持对应 Lock）
# ============================================================
STATE = {
    "service_started_at": None,
    "network_reachable": None,
    "online": None,
    "last_login_at": None,
    "last_login_success": None,
    "last_error": None,
    "last_check_at": None,
    "next_check_at": None,
    "next_check_in_sec": None,
    "current_account": "",
    "login_in_progress": False,
    "service_uptime_sec": 0,
    # —— 自动升级字段（v1.3 新增）——
    "update_state": None,           # None / checking / downloading / upgrading / success / error
    "update_progress": 0,           # 0-100
    "update_progress_message": "",  # 描述当前阶段
    "update_lock": False,           # 是否正在升级（防并发）
    "update_available": False,      # 是否发现新版
    "update_latest_version": None,  # 远程最新版本号（不含 v 前缀）
    "update_latest_url": None,      # 远程安装包直链
    "update_last_check_at": None,   # 上次检查时间
    "update_last_error": None,      # 上次错误信息
    "update_target_version": None,  # 正在升级到的版本号
    "update_success_at": None,      # 升级成功时间戳（用于自动清理绿 banner）
}

BACKOFF = {
    "consecutive_failures": 0,
    "current_minutes": BACKOFF_LEVELS[0],
    "until": None,
}

STATE_LOCK = threading.Lock()
BACKOFF_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()  # 串行化 run_once 多次调用
PWD_LOCK = threading.Lock()
UPDATE_LOCK = threading.Lock()  # 保护 update_lock 字段的并发读写


# ============================================================
# 密码（仅内存 + 文件，不入日志/响应）
# ============================================================
_PWD_VALUE = None  # None 表示未设置


def _load_password_from_disk():
    """从 password.txt 读入 _PWD_VALUE。文件不存在或为空 → None。"""
    global _PWD_VALUE
    with PWD_LOCK:
        if not os.path.isfile(PASSWORD_FILE):
            _PWD_VALUE = None
            return None
        try:
            with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
                line = f.readline()
        except OSError as exc:
            logger.error("读取 password.txt 失败: %s", exc)
            _PWD_VALUE = None
            return None
        pwd = line.strip()
        _PWD_VALUE = pwd if pwd else None
        return _PWD_VALUE


def _get_password():
    with PWD_LOCK:
        return _PWD_VALUE


def _save_password_to_disk(password):
    """写入 password.txt（原子写：先 tmp 再 replace）。"""
    global _PWD_VALUE
    if not isinstance(password, str) or len(password) < 1:
        raise ValueError("password 必须是非空字符串")
    tmp = PASSWORD_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(password.rstrip("\r\n") + "\n")
    os.replace(tmp, PASSWORD_FILE)
    with PWD_LOCK:
        _PWD_VALUE = password


# ============================================================
# 日志（统一走 logger，禁用 print）
# ============================================================
logger = logging.getLogger("campus_network")
logger.setLevel(logging.INFO)
logger.propagate = False  # 避免根 logger 重复输出

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_file_handler)


# ============================================================
# 配置：载入 / 保存 / 校验 / 默认
# ============================================================
def _default_config():
    return dict(DEFAULT_CONFIG)


def _validate_config(cfg):
    """返回错误信息列表。空列表 = 通过。"""
    errors = []
    if not isinstance(cfg, dict):
        return ["config 必须是对象"]

    host = cfg.get("host")
    if not isinstance(host, str) or not host.strip():
        errors.append("host 必须是非空字符串")

    port = cfg.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        errors.append("port 必须是 1-65535 之间的整数")

    account = cfg.get("account")
    if not isinstance(account, str) or not account.isdigit():
        errors.append("account 必须是数字字符串")

    suffix = cfg.get("suffix")
    if suffix not in ALLOWED_SUFFIXES:
        errors.append("suffix 必须是 空 / @yd / @dx / @lt 之一")

    enabled = cfg.get("auto_check_enabled")
    if not isinstance(enabled, bool):
        errors.append("auto_check_enabled 必须是布尔值")

    interval = cfg.get("auto_check_interval_min")
    if interval not in ALLOWED_INTERVALS:
        errors.append("auto_check_interval_min 必须是 5 / 15 / 30 / 60 / 120 之一")

    timeout = cfg.get("network_wait_timeout_sec")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not (10 <= timeout <= 300):
        errors.append("network_wait_timeout_sec 必须是 10-300 之间的整数")

    ui_port = cfg.get("ui_port")
    if not isinstance(ui_port, int) or isinstance(ui_port, bool) or not (1024 <= ui_port <= 65535):
        errors.append("ui_port 必须是 1024-65535 之间的整数")

    # —— 自动升级字段（v1.3 新增）——
    auto_upd = cfg.get("auto_update_enabled")
    if not isinstance(auto_upd, bool):
        errors.append("auto_update_enabled 必须是布尔值")

    upd_intv = cfg.get("update_check_interval_hours")
    if upd_intv not in ALLOWED_UPDATE_INTERVALS:
        errors.append("update_check_interval_hours 必须是 6 / 12 / 24 之一")

    upd_disk = cfg.get("update_min_free_disk_mb")
    if not isinstance(upd_disk, int) or isinstance(upd_disk, bool) or not (50 <= upd_disk <= 10240):
        errors.append("update_min_free_disk_mb 必须是 50-10240 之间的整数")

    return errors


def _load_config():
    """读 config.json；缺失/损坏 → 写默认值并返回。"""
    if not os.path.isfile(CONFIG_FILE):
        cfg = _default_config()
        try:
            _save_config_raw(cfg)
            logger.warning("config.json 不存在，已生成默认值")
        except OSError as exc:
            logger.error("写入默认 config.json 失败: %s", exc)
        return cfg
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config.json 根节点不是对象")
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.error("config.json 损坏或读取失败 (%s)，已回退默认值", exc)
        cfg = _default_config()
        try:
            _save_config_raw(cfg)
        except OSError:
            pass
        return cfg

    # 补齐缺失键（保留用户自定义键但保证默认值存在）
    merged = _default_config()
    for k, v in cfg.items():
        merged[k] = v
    return merged


def _save_config_raw(cfg):
    """原子写：tmp → replace。"""
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, CONFIG_FILE)


def _save_config(cfg):
    """校验 + 写入。失败抛 ValueError。
    兜底：先按 DEFAULT_CONFIG 把缺失字段补全，再校验、再写盘。
    这样前端 collectConfig 漏字段或老 config.json 缺失 v1.3 字段时，
    仍能按默认值落盘而不是直接报错（前后端字段没收口的修复）。
    """
    merged = _default_config()
    for k, v in cfg.items():
        merged[k] = v
    cfg = merged
    errors = _validate_config(cfg)
    if errors:
        raise ValueError("; ".join(errors))
    _save_config_raw(cfg)
    # 更新 STATE.current_account
    with STATE_LOCK:
        STATE["current_account"] = "{}{}".format(cfg.get("account", ""), cfg.get("suffix", ""))


# ============================================================
# 状态/退避助手
# ============================================================
def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _set_state(**kwargs):
    with STATE_LOCK:
        STATE.update(kwargs)


def _snapshot_state():
    with STATE_LOCK:
        s = dict(STATE)
        # 实时计算 uptime
        started = s.get("service_started_at")
        if started:
            try:
                dt = datetime.fromisoformat(started)
                s["service_uptime_sec"] = int((datetime.now() - dt).total_seconds())
            except ValueError:
                s["service_uptime_sec"] = 0
        # next_check_in_sec 实时计算
        nxt = s.get("next_check_at")
        if nxt:
            try:
                dt = datetime.fromisoformat(nxt)
                s["next_check_in_sec"] = max(0, int((dt - datetime.now()).total_seconds()))
            except ValueError:
                s["next_check_in_sec"] = None
        else:
            s["next_check_in_sec"] = None
        return s


def _set_backoff():
    """失败时：递增退避级别，记录 until。"""
    with BACKOFF_LOCK:
        cur_failures = BACKOFF["consecutive_failures"] + 1
        idx = min(cur_failures - 1, len(BACKOFF_LEVELS) - 1)
        minutes = BACKOFF_LEVELS[idx]
        BACKOFF["consecutive_failures"] = cur_failures
        BACKOFF["current_minutes"] = minutes
        BACKOFF["until"] = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        until = BACKOFF["until"]
    logger.warning("登录失败，进入退避：连续 %s 次，下次重试 %s 分钟后（%s）",
                   cur_failures, minutes, until)


def _reset_backoff():
    with BACKOFF_LOCK:
        BACKOFF["consecutive_failures"] = 0
        BACKOFF["current_minutes"] = BACKOFF_LEVELS[0]
        BACKOFF["until"] = None


def _backoff_until():
    with BACKOFF_LOCK:
        return BACKOFF.get("until")


# ============================================================
# 网络操作（与上版一致 + 接受 cfg 参数）
# ============================================================
def wait_network(host, port, timeout):
    """每 2s 探测 host:port，最多 timeout 秒。返回 bool。"""
    logger.info("等待网络 %s:%s 可用（最长 %ss）...", host, port, timeout)
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            with socket.create_connection((host, port), timeout=3):
                logger.info("网络已可达（第 %s 次尝试）", attempt)
                return True
        except OSError as exc:
            logger.info("等待中 (%s): %s", attempt, exc)
            # 此处 sleep 用在 retry 循环，不是长循环
            time.sleep(2)
    logger.error("等待 %ss 后 %s:%s 仍不可达", timeout, host, port)
    return False


def discover_network(host):
    """获取本机在校园网段的 IP 和 MAC，用于登录表单。

    策略：
      1. 优先从 chkstatus 响应里拿 v4ip / olmac（最准，跟登录账号绑定）；
      2. fallback 用 UDP socket connect 拿本机 IP；
      3. MAC 拿不到就空字符串（Dr.COM 网关允许 MAC 占位）。

    返回 (ip, mac) 元组；任意拿不到就空字符串。
    """
    ip = ""
    mac = ""
    try:
        url = "http://{}/drcom/chkstatus?callback=cb&jsVersion=4.X".format(host)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'"v4ip"\s*:\s*"([^"]*)"', txt)
        if m:
            ip = m.group(1)
        m2 = re.search(r'"olmac"\s*:\s*"([^"]*)"', txt, re.I)
        if m2:
            mac = m2.group(1).upper().replace(":", "").replace("-", "")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        pass
    # fallback：UDP connect 拿本机 IP
    if not ip:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect((host, 80))
                ip = sock.getsockname()[0]
            finally:
                sock.close()
        except OSError:
            pass
    return ip, mac


def is_online(host):
    """通过 chkstatus JSONP 查询在线状态（端口 80）。

    请求：GET http://host/drcom/chkstatus?callback=cb&jsVersion=4.X
    响应：cb({"result":1,"uid":"...","AC":"...","oltime":N,...})

    返回：
        True   已在线（result == 1）
        False  未在线（result == 0）
        None   网络异常 / 解析失败
    """
    url = "http://{}/drcom/chkstatus?callback=cb&jsVersion=4.X".format(host)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.debug("在线检查网络异常: %s", exc)
        return None
    m = re.search(r"\((\{.*?\})\s*\)", txt, re.S)
    if not m:
        logger.debug("在线检查响应非 JSONP: %r", txt[:120])
        return None
    try:
        data = json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError):
        logger.debug("在线检查 JSON 解析失败: %r", txt[:120])
        return None
    result = data.get("result")
    if result == 1:
        return True
    if result == 0:
        return False
    return None


def login(host, account, suffix, password, wlan_user_ip, wlan_user_mac):
    """Dr.COM JSONP 登录（GET /eportal/portal/login，端口 801）。

    请求：GET http://host:801/eportal/portal/login?callback=dr1234&...
    响应：dr1234({"result":1,"msg":"...","ret_code":0})

    返回 (success, msg)：
        success  True=成功（result == 1）/ False=失败
        msg      服务端 msg 字段或本地诊断信息（用于日志）
    """
    callback = "dr{}".format(random.randint(1000, 9999))
    params = {
        "callback":       callback,
        "login_method":   "1",
        "user_account":   "{}{}".format(account, suffix),
        "user_password":  password,
        "wlan_user_ip":   wlan_user_ip or "",
        "wlan_user_ipv6": "",
        "wlan_user_mac":  wlan_user_mac or "",
        "wlan_ac_ip":     "",
        "wlan_ac_name":   "",
        "terminal_type":  "1",
        "jsVersion":      "4.1.3",
        "lang":           "zh-cn",
        "v":              str(random.randint(1000, 9999)),
    }
    url = "http://{}:801/eportal/portal/login?{}".format(host, urllib.parse.urlencode(params))
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.error("登录请求异常: %s", exc)
        return False, "请求失败: {}".format(exc)
    # 解析 JSONP：dr1234({...});
    m = re.search(r"\((\{.*?\})\s*\)\s*;?\s*$", txt, re.S)
    if not m:
        return False, "login 接口返回非 JSONP: {}".format(txt[:80])
    try:
        data = json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError):
        return False, "login JSON 解析失败: {}".format(txt[:80])
    success = data.get("result") == 1
    msg = data.get("msg", "") or ""
    return success, msg


# ============================================================
# run_once — 一次完整检查
# ============================================================
def run_once(reason):
    """串行执行一次：等网络 → 查在线 → 登录。reason: startup / periodic / manual。"""
    with RUN_LOCK:
        with STATE_LOCK:
            if STATE["login_in_progress"]:
                logger.info("已有检查在进行中，跳过本次 (reason=%s)", reason)
                return
            STATE["login_in_progress"] = True

        cfg = None
        try:
            logger.info("=" * 40)
            logger.info("开始检查 (reason=%s)", reason)
            cfg = _load_config()
            host = cfg["host"]
            port = cfg["port"]
            account = cfg["account"]
            suffix = cfg["suffix"]
            interval_min = cfg["auto_check_interval_min"]

            # 更新当前账号显示
            _set_state(current_account="{}{}".format(account, suffix))

            # 1. 等网络
            if not wait_network(host, port, cfg["network_wait_timeout_sec"]):
                _set_state(network_reachable=False, online=None, last_error="校园网不可达")
                _set_backoff()
                return

            _set_state(network_reachable=True)

            # 2. 查在线
            if is_online(host):
                _set_state(online=True, last_error=None)
                _reset_backoff()
                logger.info("已在线，无需登录")
                return

            _set_state(online=False)

            # 3. 登录
            pwd = _get_password()
            if pwd is None:
                logger.warning("密码未设置，跳过登录。请通过 Web UI 设置密码。")
                _set_state(last_error="密码未设置")
                # 不计入退避（用户操作问题，不是网络问题）
                return

            wlan_ip, wlan_mac = discover_network(host)
            ok, msg = login(host, account, suffix, pwd, wlan_ip, wlan_mac)
            now_iso = _now_iso()
            if ok:
                logger.info("登录成功: %s", msg)
                _set_state(
                    online=True,
                    last_login_at=now_iso,
                    last_login_success=True,
                    last_error=None,
                )
                _reset_backoff()
            else:
                logger.error("登录失败: %s", msg)
                _set_state(
                    last_login_success=False,
                    last_error="登录失败: {}".format(msg) if msg else "登录失败（账号或密码错误或网络异常）",
                )
                _set_backoff()

        except Exception as exc:  # noqa: BLE001 — 兜底写日志
            logger.exception("run_once 未捕获异常: %s", exc)
            _set_state(last_error="内部异常: {}".format(exc))
            _set_backoff()
        finally:
            with STATE_LOCK:
                STATE["login_in_progress"] = False
                STATE["last_check_at"] = _now_iso()
            # 更新 next_check_at：退避优先，否则按 interval
            bu = _backoff_until()
            with STATE_LOCK:
                if bu:
                    STATE["next_check_at"] = bu
                else:
                    next_min = (cfg or {}).get("auto_check_interval_min", 30)
                    STATE["next_check_at"] = (
                        datetime.now() + timedelta(minutes=next_min)
                    ).isoformat(timespec="seconds")


# ============================================================
# 后台线程
# ============================================================
def _startup_trigger():
    """启动后稍等几秒再跑首次检查（让 Web server 先就绪 + 网络稳定）。"""
    if STOP_EVENT.wait(3):
        return
    run_once("startup")


def run_periodic():
    """周期自检：尊重 auto_check_enabled 与 BACKOFF.until。"""
    logger.info("周期自检线程启动")
    while not STOP_EVENT.is_set():
        cfg = _load_config()
        if not cfg.get("auto_check_enabled", True):
            logger.info("auto_check_enabled=False，30s 后重新检查开关")
            if STOP_EVENT.wait(30):
                return
            continue

        interval_sec = cfg["auto_check_interval_min"] * 60

        # 计算 wait_sec：取 interval 与剩余退避时间的较大者
        bu = _backoff_until()
        wait_sec = interval_sec
        if bu:
            try:
                bu_dt = datetime.fromisoformat(bu)
                delta = (bu_dt - datetime.now()).total_seconds()
                if delta > 0:
                    wait_sec = max(interval_sec, delta)
            except ValueError:
                pass

        # 写 next_check_at 用于 UI 显示
        with STATE_LOCK:
            STATE["next_check_at"] = (
                datetime.now() + timedelta(seconds=wait_sec)
            ).isoformat(timespec="seconds")

        # 中断等待
        if STOP_EVENT.wait(wait_sec):
            return

        run_once("periodic")


# ============================================================
# 自动升级（v1.3 新增）— 版本比较 / GitHub 探测 / 日志 / 状态
# ============================================================
def _parse_version(s):
    """将 "1.2" / "v1.3.1" / "1.3-fix" / "1.3.1-hotfix" 解析为可比较元组。解析失败返回 ()。

    规则:
    - 忽略前缀 'v' / 'V'
    - '.' 分隔数字段;每个数字段必须是纯数字,或数字后带非数字后缀( '-fix' / '_hotfix' / '-rc1' 等)
    - 遇到非数字后缀时,追加 sentinel 999 让补丁版本严格大于同主版本号 (1.3-fix > 1.3)
      但又人工高于任意后续小版本号( 1.3-fix > 1.3.1 );遇到后缀后立即停止解析,
      防止 "1.3-fix.5" 这类异常输入被错误地拆出更多段
    - 某段完全不包含数字 → 解析失败返回 ()
    """
    if not isinstance(s, str):
        return ()
    s = s.strip().lstrip("v").lstrip("V")
    if not s:
        return ()
    # 全无数字 → 拒绝
    if not any(c.isdigit() for c in s):
        return ()
    out = []
    for part in s.split("."):
        # 同一段内从头取连续数字;遇到第一个非数字即停
        digits = ""
        for c in part:
            if c.isdigit():
                digits += c
            else:
                break
        if not digits:
            return ()  # 该段没数字,解析失败
        out.append(int(digits))
        # 同一段里剩余字符(如 "-fix" / "_hotfix" / "-rc1" 等)→ 追加 sentinel
        if len(part) > len(digits):
            # 任何非数字后缀都让补丁版 > 同主版本(且人工高于任意后续小版本)
            out.append(999)
            # 后续段不再解析(防止 "1.3-fix.5" 被错误解析)
            break
    return tuple(out)


def _compare_versions(local, remote):
    """本地 vs 远程：返回 -1 / 0 / 1；不可比较返回 None。"""
    lv = _parse_version(local)
    rv = _parse_version(remote)
    if not lv or not rv:
        return None
    # 用 (差值列表) 比较
    n = max(len(lv), len(rv))
    lv = lv + (0,) * (n - len(lv))
    rv = rv + (0,) * (n - len(rv))
    if lv < rv:
        return -1
    if lv > rv:
        return 1
    return 0


def _ensure_upgrade_log():
    """确保升级日志文件存在并返回句柄。每次追加写。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if not os.path.isfile(UPGRADE_LOG_FILE):
            # 原子创建（utf-8 + LF）
            with open(UPGRADE_LOG_FILE, "a", encoding="utf-8") as f:
                pass
    except OSError as exc:
        logger.warning("无法准备 upgrade.log: %s", exc)


def _log_upgrade(level, msg):
    """同时写到 logs/upgrade.log 与主 logger。"""
    _ensure_upgrade_log()
    line = "[{}] [{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    try:
        with open(UPGRADE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        logger.warning("写 upgrade.log 失败: %s", exc)
    if level == "ERROR":
        logger.error("UPGRADE: %s", msg)
    elif level == "WARN":
        logger.warning("UPGRADE: %s", msg)
    else:
        logger.info("UPGRADE: %s", msg)


def _check_disk_free_mb():
    """检查 BASE_DIR 所在磁盘剩余空间（MB），失败返回 None。"""
    try:
        usage = shutil.disk_usage(BASE_DIR)
        return int(usage.free / (1024 * 1024))
    except (OSError, AttributeError):
        return None


def _check_github_latest():
    """调用 GitHub releases/latest API。
    返回 (version, asset_url, digest, size, published_at) 元组；失败返回 None。
    digest 形如 "sha256:abcd..."，已剥掉前缀。
    """
    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={
                "User-Agent": GITHUB_UA,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.warning("GitHub releases/latest 不可达: %s", exc)
        return None
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("GitHub API JSON 解析失败: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    version = tag.strip().lstrip("v").lstrip("V")
    assets = data.get("assets") or []
    asset_url = None
    digest = None
    size = None
    if isinstance(assets, list):
        for a in assets:
            if not isinstance(a, dict):
                continue
            name = a.get("name") or ""
            if isinstance(name, str) and name.lower().endswith(".exe"):
                asset_url = a.get("browser_download_url")
                d = a.get("digest") or ""
                if isinstance(d, str) and d.startswith("sha256:"):
                    digest = d.split(":", 1)[1]
                s = a.get("size")
                if isinstance(s, int):
                    size = s
                break
    if not asset_url:
        return None
    published = data.get("published_at")
    return version, asset_url, digest, size, published


def _set_update_state(**kwargs):
    """写 STATE 的 update_* 字段。线程安全。"""
    with STATE_LOCK:
        for k, v in kwargs.items():
            STATE[k] = v


def _get_update_field(key):
    with STATE_LOCK:
        return STATE.get(key)


def _acquire_update_lock():
    """原子检查并获取升级锁。返回 True=获得锁；False=已在升级。"""
    with UPDATE_LOCK:
        with STATE_LOCK:
            if STATE.get("update_lock"):
                return False
            STATE["update_lock"] = True
            STATE["update_state"] = "checking"
            STATE["update_progress"] = 0
            STATE["update_progress_message"] = "准备升级..."
            STATE["update_last_error"] = None
            return True


def _release_update_lock():
    with UPDATE_LOCK:
        with STATE_LOCK:
            STATE["update_lock"] = False


def _download_installer(url, dest_path, expected_size, progress_callback=None):
    """流式下载安装器到本地。返回写入字节数或抛异常。

    progress_callback(downloaded_bytes, total_bytes_or_None) 每 ~200ms 触发。
    """
    last_report = [0.0]
    last_bytes = [0]

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": GITHUB_UA, "Accept": "application/octet-stream"},
        )
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            total_header = resp.headers.get("Content-Length")
            total = int(total_header) if (total_header and total_header.isdigit()) else None
            tmp = dest_path + ".part"
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    f.write(chunk)
                    last_bytes[0] += len(chunk)
                    now = time.time()
                    if progress_callback and (now - last_report[0] >= 0.2 or last_bytes[0] == total):
                        last_report[0] = now
                        try:
                            progress_callback(last_bytes[0], total or expected_size)
                        except Exception:  # noqa: BLE001
                            pass
            # 原子改名
            os.replace(tmp, dest_path)
            return last_bytes[0]
    except (urllib.error.URLError, OSError, TimeoutError):
        # 清理半成品
        for p in (dest_path + ".part", dest_path):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        raise


def _verify_sha256(path, expected_hex):
    """计算文件 SHA256，对比十六进制串（小写）。返回 bool。"""
    if not expected_hex:
        return False
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK_BYTES), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest().lower() == expected_hex.lower()


def _backup_service_py():
    """复制联网_service.py 到 %TEMP%，返回 backup 路径。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(os.environ.get("TEMP", "."), "drcom_backup_{}.py".format(ts))
    try:
        shutil.copy2(os.path.join(BASE_DIR, "联网_service.py"), backup)
        return backup
    except OSError as exc:
        logger.error("备份联网_service.py 失败: %s", exc)
        return None


def _read_nssm_appexit():
    """从注册表读 AppExit 值；不存在 / 权限不足返回 None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NSSM_PARAMETERS_PATH) as k:
            value, _ = winreg.QueryValueEx(k, "AppExit")
            return value
    except (OSError, ImportError):
        return None


def _write_nssm_appexit(value):
    """直写 AppExit 到注册表。失败抛 OSError。"""
    import winreg
    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, NSSM_PARAMETERS_PATH) as k:
        winreg.SetValueEx(k, "AppExit", 0, winreg.REG_SZ, value)


def _set_nssm_appexit(value):
    """设 AppExit。先试注册表直写，再试 nssm.exe 调用。"""
    try:
        _write_nssm_appexit(value)
        return True
    except (OSError, ImportError) as reg_exc:
        # fallback：调 nssm.exe（路径含空格 → 用 list + 0x22 quote）
        if not os.path.isfile(NSSM_PATH):
            logger.warning("nssm.exe 不在 %s，无法 fallback", NSSM_PATH)
            return False
        try:
            subprocess.run(
                [NSSM_PATH, "set", SERVICE_NAME, "AppExit", value],
                timeout=15,
                check=False,
            )
            return True
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("nssm set AppExit 失败: %s", exc)
            return False


def _nssm_stop_service(timeout_sec=30):
    """通过 nssm.exe 停服务。timeout 后 fallback 不做（installer 会接管）。"""
    if not os.path.isfile(NSSM_PATH):
        _log_upgrade("WARN", "nssm.exe 不存在，无法显式 stop；依赖 installer / NSSM 接管")
        return False
    try:
        proc = subprocess.run(
            [NSSM_PATH, "stop", SERVICE_NAME],
            timeout=timeout_sec,
            check=False,
        )
        _log_upgrade("INFO", "nssm stop 返回码: {}".format(proc.returncode))
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        _log_upgrade("WARN", "nssm stop 超时（{}s），fallback 由 installer 接管".format(timeout_sec))
        return False
    except OSError as exc:
        _log_upgrade("WARN", "nssm stop 调用失败: {}".format(exc))
        return False


def _launch_installer(installer_path):
    """用 Inno Setup 静默参数启动 installer，返回 Popen 对象或抛异常。

    关键：用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB 让
    子进程完全脱离父 Python 服务的进程生命周期。这样 Python 服务被 NSSM 杀掉时，
    installer 不会被连累，能继续完成安装（停止旧服务 → 复制文件 → PostInstall 启动新服务）。
    """
    args = [
        installer_path,
        "/SP-",
        "/SILENT",
        "/CLOSEAPPLICATIONS",
        "/TASKS=startservice",
    ]
    # Windows 进程创建标志（详见 MSDN CreateProcess dwCreationFlags）
    DETACHED_PROCESS          = 0x00000008  # 子进程无控制台、不继承父 console
    CREATE_NEW_PROCESS_GROUP   = 0x00000200  # 子进程属于新 process group，不响应父 Ctrl+C/Ctrl+Break
    CREATE_BREAKAWAY_FROM_JOB  = 0x01000000  # 子进程脱离父进程的 Job Object（NSSM/服务宿主常用 Job）
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    return subprocess.Popen(args, close_fds=True, creationflags=flags)


# ============================================================
# 自动升级主流程（11 步）
# ============================================================
def _do_update_now():
    """完整执行一次升级检测 + 下载 + 升级。返回 dict（用于 HTTP 响应）。"""
    if not _acquire_update_lock():
        return {"ok": False, "error": "升级正在进行中"}
    try:
        # —— 1. 锁住：state=checking（acquire 时已设置）——
        _set_update_state(update_progress_message="检查 GitHub 最新版本...")
        cfg = _load_config()
        min_free_mb = int(cfg.get("update_min_free_disk_mb", 200))

        # —— 2. 校验前置条件 ——
        # 2a. 磁盘剩余
        free_mb = _check_disk_free_mb()
        if free_mb is not None and free_mb < min_free_mb:
            msg = "磁盘剩余空间不足：{} MB < {} MB".format(free_mb, min_free_mb)
            _set_update_state(update_state="error", update_progress=0, update_progress_message=msg)
            _log_upgrade("ERROR", msg)
            return {"ok": False, "error": msg}

        # 2b. nssm.exe 存在
        if not os.path.isfile(NSSM_PATH):
            msg = "找不到 nssm.exe：{}".format(NSSM_PATH)
            _set_update_state(update_state="error", update_progress=0, update_progress_message=msg)
            _log_upgrade("ERROR", msg)
            return {"ok": False, "error": msg}

        # 2c. GitHub API 可达
        latest = _check_github_latest()
        if latest is None:
            msg = "GitHub releases/latest 不可达或返回异常"
            _set_update_state(update_state="error", update_progress=0, update_progress_message=msg)
            _log_upgrade("ERROR", msg)
            return {"ok": False, "error": msg}
        remote_ver, asset_url, digest, asset_size, published = latest

        # —— 3. 比较版本 ——
        cmp = _compare_versions(VERSION, remote_ver)
        if cmp is None:
            msg = "无法比较版本：本机 {} vs 远程 {}".format(VERSION, remote_ver)
            _set_update_state(update_state="error", update_progress=0, update_progress_message=msg)
            _log_upgrade("ERROR", msg)
            return {"ok": False, "error": msg}
        if cmp >= 0:
            _set_update_state(update_state=None, update_progress=0, update_progress_message="")
            _log_upgrade("INFO", "已是最新版本：{}（远程 {}）".format(VERSION, remote_ver))
            return {"ok": False, "error": "已是最新版本 {}（远程 {}）".format(VERSION, remote_ver)}

        _log_upgrade("INFO", "检测到新版本 {}（当前 {}），开始下载".format(remote_ver, VERSION))
        _set_update_state(
            update_available=True,
            update_latest_version=remote_ver,
            update_latest_url=asset_url,
            update_target_version=remote_ver,
            update_last_check_at=_now_iso(),
        )

        # —— 4. 下载 ——
        _set_update_state(update_state="downloading", update_progress=0,
                          update_progress_message="下载安装器（{}）...".format(remote_ver))

        temp_dir = os.environ.get("TEMP", ".")
        installer_name = INSTALLER_FILENAME_PATTERN.format(ver=remote_ver)
        installer_path = os.path.join(temp_dir, installer_name)

        def _on_progress(done, total):
            pct = int(done * 100 / total) if total and total > 0 else 0
            _set_update_state(update_progress=pct, update_progress_message="下载中 {}%".format(pct))

        try:
            written = _download_installer(asset_url, installer_path, asset_size, _on_progress)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            msg = "下载失败：{}".format(exc)
            _set_update_state(update_state="error", update_progress=0, update_progress_message=msg)
            _log_upgrade("ERROR", msg)
            return {"ok": False, "error": msg}

        _log_upgrade("INFO", "下载完成：{} 字节".format(written))

        # —— 5. 校验 SHA256 ——
        if digest and not _verify_sha256(installer_path, digest):
            try:
                os.remove(installer_path)
            except OSError:
                pass
            msg = "SHA256 校验失败，已删除安装器"
            _set_update_state(update_state="error", update_progress=0, update_progress_message=msg)
            _log_upgrade("ERROR", "{}（期望 {}）".format(msg, digest[:16] + "..."))
            return {"ok": False, "error": msg}
        elif digest:
            _log_upgrade("INFO", "SHA256 校验通过")

        # —— 6. 备份当前脚本 ——
        backup = _backup_service_py()
        if backup is None:
            msg = "备份联网_service.py 失败"
            _set_update_state(update_state="error", update_progress=0, update_progress_message=msg)
            _log_upgrade("ERROR", msg)
            try:
                os.remove(installer_path)
            except OSError:
                pass
            return {"ok": False, "error": msg}
        _set_update_state(update_backup=backup)
        _log_upgrade("INFO", "备份到 {}".format(backup))

        # —— 7. 保存 AppExit 原值（升级透明，不修改）——
        # 之前尝试设 "Disabled" 但 NSSM 合法值是 Default/Exit/Success/Failure/Codes，
        # 写 "Disabled" 会让 nssm 服务无法启动（OpenService 0x424）。
        # 现在采用透明策略：原值存到 STATE，仅在 _post_upgrade_startup 做幂等恢复，
        # 升级期间不动注册表，避免污染用户 NSSM 配置。
        prev_appexit = _read_nssm_appexit()
        _set_update_state(update_prev_appexit=prev_appexit)
        _log_upgrade("INFO", "升级透明：AppExit 保持原值 {}（不修改注册表）".format(prev_appexit))

        # —— 8. 启动 installer（DETACHED_PROCESS 完全脱离父 Python；不 wait）——
        try:
            proc = _launch_installer(installer_path)
            _log_upgrade("INFO", "installer 已启动 PID={}（DETACHED_PROCESS）".format(proc.pid))
        except OSError as exc:
            # 启动失败：不让 Python 退出，保持服务运行 + Web UI 显示 error
            msg = "启动 installer 失败：{}".format(exc)
            _set_update_state(update_state="error", update_progress=0, update_progress_message=msg)
            _log_upgrade("ERROR", msg)
            return {"ok": False, "error": msg}

        # —— 9. 升级中状态 ——
        _set_update_state(
            update_state="upgrading",
            update_progress=100,
            update_progress_message="正在升级到 {}（约 60 秒）...".format(remote_ver),
        )

        # —— 10. 立即退出 Python 进程（installer 独立接管后续安装）——
        # 关键：installer 已用 DETACHED_PROCESS 脱离父进程 + Python 用 os._exit(0) 立即
        # 终止，不调 _nssm_stop_service（之前那种调用会触发 NSSM 把我们和 installer 连带杀掉）。
        # installer 自身在 ssInstall 阶段会调 nssm stop（idempotent）→ 复制文件 → 
        # PostInstall 启动新服务。
        _log_upgrade("INFO", "升级触发完成，Python 进程立即退出（installer 独立运行）")
        os._exit(0)

    finally:
        # 若走到这里说明流程在 installer 启动前失败 / 或 stop 没杀掉我们
        # 正常路径下 NSSM stop 会让我们在执行 finally 前被 SIGKILL
        _release_update_lock()


def _do_check_now():
    """只跑 GitHub 检查 + 状态更新，不升级。"""
    if not _acquire_update_lock():
        return {"ok": False, "error": "升级正在进行中"}
    try:
        _set_update_state(update_progress_message="检查 GitHub 最新版本...")
        latest = _check_github_latest()
        if latest is None:
            _set_update_state(update_state="error", update_progress=0,
                              update_progress_message="GitHub 不可达或返回异常",
                              update_last_error="GitHub releases/latest 不可达",
                              update_last_check_at=_now_iso())
            _log_upgrade("WARN", "GitHub 检查失败")
            return {"ok": False, "error": "GitHub 不可达或返回异常"}

        remote_ver, asset_url, digest, size, published = latest
        cmp = _compare_versions(VERSION, remote_ver)
        if cmp is None or cmp >= 0:
            _set_update_state(
                update_state=None,
                update_progress=0,
                update_progress_message="",
                update_available=False,
                update_latest_version=remote_ver,
                update_latest_url=asset_url,
                update_last_check_at=_now_iso(),
                update_last_error=None,
            )
            _log_upgrade("INFO", "检查完成：已是最新 {}".format(VERSION))
            return {"ok": True, "update_available": False, "latest_version": remote_ver}

        _set_update_state(
            update_state=None,
            update_progress=0,
            update_progress_message="有新版本可用：{}".format(remote_ver),
            update_available=True,
            update_latest_version=remote_ver,
            update_latest_url=asset_url,
            update_last_check_at=_now_iso(),
            update_last_error=None,
        )
        _log_upgrade("INFO", "检查完成：发现新版本 {}".format(remote_ver))
        return {"ok": True, "update_available": True, "latest_version": remote_ver}
    finally:
        _release_update_lock()


# ============================================================
# 自动升级后台线程
# ============================================================
def _auto_update_loop():
    """后台线程：按 update_check_interval_hours 周期检查 GitHub 新版。
    注意：auto_update_enabled=False 时不主动检查，但 manual / 启动钩子仍可触发。
    """
    logger.info("自动升级后台线程启动")
    # 首次启动延迟 30 秒（让服务先稳定 + Web UI 就绪）
    if STOP_EVENT.wait(30):
        return
    while not STOP_EVENT.is_set():
        try:
            cfg = _load_config()
            if not cfg.get("auto_update_enabled", True):
                logger.info("auto_update_enabled=False，30s 后重新检查开关")
                if STOP_EVENT.wait(30):
                    return
                continue
            interval_sec = int(cfg.get("update_check_interval_hours", 6)) * 3600
        except (OSError, ValueError) as exc:
            logger.warning("读取更新配置失败: %s", exc)
            if STOP_EVENT.wait(60):
                return
            continue

        # 检查（不升级）：如果发现新版，写 STATE 但不触发 do_update_now
        # 真正的升级由后台线程检测到 update_available=True 后启动
        # 但用户明确要"静默"→ 这里直接触发升级（无需 Web UI 介入）
        result = _do_check_now()
        if isinstance(result, dict) and result.get("update_available"):
            # 静默升级：立即进入升级流程
            _log_upgrade("INFO", "后台线程检测到新版本，触发静默升级")
            _do_update_now()
            # 升级完大概率进程被杀；即使没被杀，也等下次循环
            if STOP_EVENT.wait(60):
                return
            continue

        if STOP_EVENT.wait(interval_sec):
            return


def _post_upgrade_startup():
    """启动钩子：检查是否刚升级过（对比当前脚本与备份），写日志 + 清理。"""
    try:
        backups = []
        for name in os.listdir(os.environ.get("TEMP", ".")):
            if name.startswith("drcom_backup_") and name.endswith(".py"):
                backups.append(name)
        backups.sort(reverse=True)  # 最新在前
        if not backups:
            return
        # 最新备份
        newest = os.path.join(os.environ.get("TEMP", "."), backups[0])
        if not os.path.isfile(newest):
            return
        # 对比 hash
        def _h(p):
            hh = hashlib.sha256()
            try:
                with open(p, "rb") as f:
                    for c in iter(lambda: f.read(DOWNLOAD_CHUNK_BYTES), b""):
                        hh.update(c)
            except OSError:
                return None
            return hh.hexdigest()
        current_hash = _h(os.path.join(BASE_DIR, "联网_service.py"))
        backup_hash = _h(newest)
        if current_hash and backup_hash and current_hash != backup_hash:
            _log_upgrade("INFO", "升级完成（v{}）：当前脚本与备份不同".format(VERSION))
            _set_update_state(
                update_state="success",
                update_progress=100,
                update_progress_message="已升级到 v{}".format(VERSION),
                update_target_version=VERSION,
                update_success_at=_now_iso(),
                update_last_error=None,
            )
        else:
            _log_upgrade("INFO", "启动钩子：未检测到脚本变更")

        # 清理 7 天前的旧备份
        cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
        for name in backups[1:]:
            p = os.path.join(os.environ.get("TEMP", "."), name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    _log_upgrade("INFO", "清理过期备份：{}".format(name))
            except OSError:
                pass

        # 恢复 AppExit（与 setup.iss 一致：Default Restart + 0 Restart）
        try:
            _set_nssm_appexit("Default Restart")
            _set_nssm_appexit("0 Restart")
            _log_upgrade("INFO", "AppExit 已恢复为 Default Restart / 0 Restart")
        except Exception as exc:  # noqa: BLE001
            _log_upgrade("WARN", "恢复 AppExit 失败: {}".format(exc))
    except Exception as exc:  # noqa: BLE001
        _log_upgrade("WARN", "启动钩子异常: {}".format(exc))


def _schedule_success_clear():
    """5 分钟后清掉绿 banner（避免每次启动都显示）。"""
    success_at = _get_update_field("update_success_at")
    if not success_at:
        return
    try:
        sa = datetime.fromisoformat(success_at)
        delta = (datetime.now() - sa).total_seconds()
        if delta >= UPGRADE_SUCCESS_TTL_SEC:
            _set_update_state(update_state=None, update_progress_message="", update_success_at=None)
    except ValueError:
        _set_update_state(update_state=None, update_progress_message="", update_success_at=None)


# ============================================================
# Web API
# ============================================================
def _send_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_bytes(handler, status, content_type, body, filename=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if filename:
        handler.send_header("Content-Disposition", 'attachment; filename="{}"'.format(filename))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("请求体不是合法 JSON")


def api_get_status():
    return _snapshot_state()


def api_get_config():
    cfg = _load_config()
    cfg = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
    with PWD_LOCK:
        pwd_set = _PWD_VALUE is not None
    return {
        **cfg,
        "password_status": "set" if pwd_set else "missing",
    }


def api_post_config(payload):
    try:
        cfg_in = _read_json_body_safe(payload)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    if not isinstance(cfg_in, dict):
        return 400, {"ok": False, "error": "请求体必须是 JSON 对象"}
    try:
        _save_config(cfg_in)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except OSError as exc:
        logger.error("写 config.json 失败: %s", exc)
        return 500, {"ok": False, "error": "写文件失败"}
    logger.info("配置已更新")
    return 200, {"ok": True}


def _read_json_body_safe(payload):
    """payload 已是 dict（do_POST 已解析）。重复保险：再校验一次。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return payload


def api_post_password(payload):
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "请求体必须是 JSON 对象"}
    pwd = payload.get("password")
    if not isinstance(pwd, str) or len(pwd) < 1:
        return 400, {"ok": False, "error": "password 必须是非空字符串"}
    try:
        _save_password_to_disk(pwd)
    except (OSError, ValueError) as exc:
        logger.error("写 password.txt 失败: %s", exc)
        return 500, {"ok": False, "error": "写文件失败"}
    logger.info("密码已更新")
    return 200, {"ok": True}


def api_post_login():
    """异步触发 run_once('manual')。"""
    with STATE_LOCK:
        if STATE["login_in_progress"]:
            return 200, {"triggered": False, "reason": "already_in_progress"}
    t = threading.Thread(target=run_once, args=("manual",), daemon=True)
    t.start()
    return 200, {"triggered": True}


def api_get_log_tail(offset, max_lines):
    offset = max(0, int(offset or 0))
    max_lines = max(1, min(int(max_lines or 200), 5000))
    if not os.path.isfile(LOG_FILE):
        return {"lines": [], "next_offset": 0, "total_size": 0}
    total = os.path.getsize(LOG_FILE)
    if offset >= total:
        return {"lines": [], "next_offset": total, "total_size": total}
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError as exc:
        return {"lines": [], "next_offset": offset, "total_size": total, "error": str(exc)}
    # 拆分行为保留最后 max_lines 行
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        text = data.decode("latin-1", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        next_offset = total  # 已截断，next_offset 标记为文件末尾
    else:
        next_offset = offset + len(data)
    return {"lines": lines, "next_offset": next_offset, "total_size": total}


def api_get_log_file_path():
    return {"path": LOG_FILE}


def api_get_log_download():
    if not os.path.isfile(LOG_FILE):
        return None, b""
    try:
        with open(LOG_FILE, "rb") as f:
            data = f.read()
    except OSError:
        return None, b""
    return "campus_login_{}.log".format(datetime.now().strftime("%Y%m%d_%H%M%S")), data


def api_get_about():
    s = _snapshot_state()
    return {
        "version": VERSION,
        "service_started_at": s.get("service_started_at"),
        "service_uptime_sec": s.get("service_uptime_sec"),
        "data_dir": BASE_DIR,
        "log_file": LOG_FILE,
        "config_file": CONFIG_FILE,
        "password_file": PASSWORD_FILE,
        "log_dir": str(LOG_DIR),
    }


def api_post_restart(handler):
    """返回响应后用 os._exit(0) 退出，由 NSSM 重启。"""
    def _delayed_exit():
        STOP_EVENT.set()
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_delayed_exit, daemon=True).start()
    return 200, {"ok": True, "message": "服务正在重启"}


# ============================================================
# 自动升级 API（v1.3 新增）
# ============================================================
def api_get_update_status():
    """GET /api/update/status — 当前升级状态快照。"""
    _schedule_success_clear()  # 顺手清理过期绿 banner
    cfg = _load_config()
    snap = _snapshot_state()
    return {
        "enabled": bool(cfg.get("auto_update_enabled", True)),
        "local_version": VERSION,
        "latest_version": snap.get("update_latest_version"),
        "latest_url": snap.get("update_latest_url"),
        "update_available": bool(snap.get("update_available")),
        "state": snap.get("update_state"),
        "progress_pct": int(snap.get("update_progress") or 0),
        "progress_message": snap.get("update_progress_message") or "",
        "last_check_at": snap.get("update_last_check_at"),
        "last_error": snap.get("update_last_error"),
        "target_version": snap.get("update_target_version"),
        "check_interval_hours": int(cfg.get("update_check_interval_hours", 6)),
        "min_free_disk_mb": int(cfg.get("update_min_free_disk_mb", 200)),
    }


def api_post_update_check(payload):
    """POST /api/update/check — 立即触发一次 GitHub 检查（不等后台线程）。"""
    # 异步执行，避免阻塞 HTTP 响应
    def _runner():
        try:
            _do_check_now()
        except Exception as exc:  # noqa: BLE001
            _log_upgrade("ERROR", "手动检查异常: {}".format(exc))
    threading.Thread(target=_runner, daemon=True).start()
    return 200, {"ok": True, "message": "已提交检查任务"}


def api_post_update_install(payload):
    """POST /api/update/install — 立即开始升级。"""
    # 异步执行完整升级流程（耗时较长，HTTP 先返回）
    def _runner():
        try:
            _do_update_now()
        except Exception as exc:  # noqa: BLE001
            _log_upgrade("ERROR", "手动升级异常: {}".format(exc))
            _set_update_state(update_state="error", update_progress=0,
                              update_progress_message="升级异常：{}".format(exc),
                              update_last_error=str(exc))
    threading.Thread(target=_runner, daemon=True).start()
    return 200, {"ok": True, "message": "已提交升级任务"}


def api_post_update_toggle(payload):
    """POST /api/update/toggle — 切换 auto_update_enabled（payload: {enabled}）。"""
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "请求体必须是 JSON 对象"}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return 400, {"ok": False, "error": "enabled 必须是布尔值"}
    try:
        cfg = _load_config()
        cfg["auto_update_enabled"] = enabled
        _save_config(cfg)
    except (ValueError, OSError) as exc:
        return 400, {"ok": False, "error": str(exc)}
    _log_upgrade("INFO", "auto_update_enabled 改为 {}".format(enabled))
    return 200, {"ok": True, "auto_update_enabled": enabled}


def api_get_update_history():
    """GET /api/update/history — 返回 logs/upgrade.log 最后 UPGRADE_HISTORY_MAX_LINES 行。"""
    lines = []
    if os.path.isfile(UPGRADE_LOG_FILE):
        try:
            with open(UPGRADE_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            lines = [ln.rstrip("\r\n") for ln in all_lines[-UPGRADE_HISTORY_MAX_LINES:]]
        except OSError as exc:
            return {"lines": [], "error": str(exc), "path": UPGRADE_LOG_FILE}
    return {"lines": lines, "path": UPGRADE_LOG_FILE}


# ============================================================
# HTTP Handler
# ============================================================
class _Handler(BaseHTTPRequestHandler):
    server_version = "DrcomAutoLogin/{}".format(VERSION)

    # 静默 BaseHTTPRequestHandler 默认的 stderr 访问日志（避免污染 service_stderr.log）
    def log_message(self, format, *args):
        pass

    def _parse_url(self):
        if "?" in self.path:
            path, qs = self.path.split("?", 1)
        else:
            path, qs = self.path, ""
        params = {}
        for kv in qs.split("&"):
            if not kv:
                continue
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    params[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
                except Exception:  # noqa: BLE001
                    params[k] = v
        return path, params

    def _serve_html(self):
        """返回内嵌的 SPA HTML 页面。"""
        body = _HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, params = self._parse_url()
        try:
            if path == "/" or path == "/index.html":
                self._serve_html()
                return
            if path == "/api/status":
                _send_json(self, 200, api_get_status())
                return
            if path == "/api/config":
                _send_json(self, 200, api_get_config())
                return
            if path == "/api/log_tail":
                offset = params.get("offset", "0")
                maxn = params.get("max", "200")
                _send_json(self, 200, api_get_log_tail(offset, maxn))
                return
            if path == "/api/log_file_path":
                _send_json(self, 200, api_get_log_file_path())
                return
            if path == "/api/log_download":
                fname, data = api_get_log_download()
                if fname is None:
                    _send_json(self, 404, {"error": "log file not found"})
                else:
                    _send_bytes(self, 200, "text/plain; charset=utf-8", data, filename=fname)
                return
            if path == "/api/about":
                _send_json(self, 200, api_get_about())
                return
            if path == "/api/health":
                _send_json(self, 200, {"ok": True, "version": VERSION})
                return
            # —— 自动升级（v1.3 新增）——
            if path == "/api/update/status":
                _send_json(self, 200, api_get_update_status())
                return
            if path == "/api/update/history":
                _send_json(self, 200, api_get_update_history())
                return
            _send_json(self, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("GET %s 异常: %s", path, exc)
            _send_json(self, 500, {"error": "internal: {}".format(exc)})

    def do_POST(self):
        path, _params = self._parse_url()
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            _send_json(self, 400, {"ok": False, "error": "请求体不是合法 JSON"})
            return

        try:
            if path == "/api/login":
                status, body = api_post_login()
                _send_json(self, status, body)
                return
            if path == "/api/config":
                status, body = api_post_config(payload)
                _send_json(self, status, body)
                return
            if path == "/api/password":
                status, body = api_post_password(payload)
                _send_json(self, status, body)
                return
            if path == "/api/restart":
                status, body = api_post_restart(self)
                _send_json(self, status, body)
                return
            # —— 自动升级（v1.3 新增）——
            if path == "/api/update/check":
                status, body = api_post_update_check(payload)
                _send_json(self, status, body)
                return
            if path == "/api/update/install":
                status, body = api_post_update_install(payload)
                _send_json(self, status, body)
                return
            if path == "/api/update/toggle":
                status, body = api_post_update_toggle(payload)
                _send_json(self, status, body)
                return
            _send_json(self, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("POST %s 异常: %s", path, exc)
            _send_json(self, 500, {"ok": False, "error": "internal: {}".format(exc)})


# ============================================================
# HTML 单页应用（4 标签：状态 / 配置 / 日志 / 关于）
# 完全内联 CSS/JS，无任何外部资源（CDN/font/img），校园网离线也能用。
# ============================================================
_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Dr.COM 校园网自动登录</title>
<style>
/* ============================================================
   1. 设计令牌 — 浅色（默认；DeepSeek 风格：淡蓝/淡紫渐变）
   ============================================================ */
:root {
  --bg-base: #f5f6ff;
  --bg-tint-a: #eef2ff;
  --bg-tint-b: #f5f3ff;
  --surface: #ffffff;
  --surface-2: #f8f9ff;
  --surface-3: #f0f3ff;
  --topbar-bg: rgba(255, 255, 255, 0.78);
  --hint-bg: rgba(99, 102, 241, 0.06);
  --hint-border: rgba(99, 102, 241, 0.18);
  --text: #1a1a2e;
  --text-strong: #0f172a;
  --text-muted: #475569;
  --text-faint: #64748b;
  --primary: #3b5bdb;
  --primary-strong: #4f46e5;
  --primary-soft: rgba(59, 91, 219, 0.10);
  --primary-softer: rgba(59, 91, 219, 0.06);
  --ok: #10b981;
  --ok-soft: rgba(16, 185, 129, 0.12);
  --ok-softer: rgba(16, 185, 129, 0.06);
  --warn: #f59e0b;
  --warn-soft: rgba(245, 158, 11, 0.14);
  --err: #ef4444;
  --err-soft: rgba(239, 68, 68, 0.12);
  --border: rgba(15, 23, 42, 0.07);
  --border-strong: rgba(15, 23, 42, 0.12);
  --track: #cbd5e1;
  --log-bg: #0f172a;
  --log-text: #e2e8f0;
  --log-dim: #64748b;
  --radius-lg: 20px;
  --radius-md: 16px;
  --radius-sm: 12px;
  --radius-pill: 999px;
  --shadow-1: 0 4px 24px rgba(15, 23, 42, 0.04), 0 1px 2px rgba(15, 23, 42, 0.03);
  --shadow-2: 0 10px 40px rgba(15, 23, 42, 0.06), 0 2px 6px rgba(15, 23, 42, 0.04);
  --shadow-lift: 0 16px 48px rgba(15, 23, 42, 0.10), 0 4px 12px rgba(15, 23, 42, 0.04);
}

/* ============================================================
   2. 设计令牌 — 深色（html[data-theme=dark] 激活）
   ============================================================ */
[data-theme="dark"] {
  --bg-base: #0f172a;
  --bg-tint-a: #1e293b;
  --bg-tint-b: #1a1a3a;
  --surface: #1e293b;
  --surface-2: #243044;
  --surface-3: #2a3548;
  --topbar-bg: rgba(15, 23, 42, 0.78);
  --hint-bg: rgba(129, 140, 248, 0.10);
  --hint-border: rgba(129, 140, 248, 0.24);
  --text: #f1f5f9;
  --text-strong: #ffffff;
  --text-muted: #94a3b8;
  --text-faint: #64748b;
  --primary: #818cf8;
  --primary-strong: #a5b4fc;
  --primary-soft: rgba(129, 140, 248, 0.18);
  --primary-softer: rgba(129, 140, 248, 0.10);
  --ok: #34d399;
  --ok-soft: rgba(52, 211, 153, 0.18);
  --ok-softer: rgba(52, 211, 153, 0.08);
  --warn: #fbbf24;
  --warn-soft: rgba(251, 191, 36, 0.18);
  --err: #f87171;
  --err-soft: rgba(248, 113, 113, 0.18);
  --border: rgba(255, 255, 255, 0.08);
  --border-strong: rgba(255, 255, 255, 0.14);
  --track: #475569;
  --log-bg: #080f1f;
  --log-text: #cbd5e1;
  --log-dim: #475569;
  --shadow-1: 0 4px 24px rgba(0, 0, 0, 0.30), 0 1px 2px rgba(0, 0, 0, 0.20);
  --shadow-2: 0 10px 40px rgba(0, 0, 0, 0.40), 0 2px 6px rgba(0, 0, 0, 0.25);
  --shadow-lift: 0 16px 48px rgba(0, 0, 0, 0.55), 0 4px 12px rgba(0, 0, 0, 0.30);
}

/* ============================================================
   3. 基础排版与背景层（渐变 + 1px 网格底纹叠加）
   ============================================================ */
*, *::before, *::after { box-sizing: border-box; }
html, body { height: auto; }
body {
  margin: 0;
  min-height: 100vh;
  background:
    radial-gradient(ellipse 80% 50% at 20% -10%, var(--bg-tint-a) 0%, transparent 60%),
    radial-gradient(ellipse 60% 40% at 90% 10%, var(--bg-tint-b) 0%, transparent 60%),
    linear-gradient(180deg, var(--bg-base) 0%, var(--surface) 100%);
  background-attachment: fixed;
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", sans-serif;
  font-size: 14.5px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  transition: background-color 0.25s ease, color 0.25s ease;
  position: relative;
  overflow-x: hidden;
}
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(to right, rgba(15, 23, 42, 0.045) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(15, 23, 42, 0.045) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
  z-index: 0;
  opacity: 1;
}
[data-theme="dark"] body::before {
  background-image:
    linear-gradient(to right, rgba(255, 255, 255, 0.04) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(255, 255, 255, 0.04) 1px, transparent 1px);
}
h1, h2, h3 { margin: 0; font-weight: 650; letter-spacing: -0.01em; }
a { color: var(--primary); text-decoration: none; }
a:hover { color: var(--primary-strong); }
.mono { font-family: "Cascadia Mono", "JetBrains Mono", "Consolas", "SFMono-Regular", "Courier New", monospace; font-variant-numeric: tabular-nums; }
.muted { color: var(--text-muted); }
.skip {
  position: absolute; left: -9999px; top: 0; z-index: 99;
  background: var(--surface); border: 1px solid var(--border); border-radius: 0 0 var(--radius-sm) 0;
  padding: 8px 14px;
}
.skip:focus { left: 0; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 18px 22px 64px; position: relative; z-index: 1; }

/* ============================================================
   4. 顶栏（玻璃质感）+ 分段控件
   ============================================================ */
.topbar {
  position: sticky; top: 0; z-index: 40;
  background: var(--topbar-bg);
  backdrop-filter: saturate(180%) blur(18px);
  -webkit-backdrop-filter: saturate(180%) blur(18px);
  border-bottom: 1px solid var(--border);
}
.topbar-inner {
  max-width: 1180px; margin: 0 auto; padding: 14px 22px 10px;
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
}
.brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
.brand-mark {
  width: 32px; height: 32px; display: inline-flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, var(--primary) 0%, var(--primary-strong) 100%);
  border-radius: 10px; color: #fff; font-size: 16px; font-weight: 700;
  box-shadow: 0 4px 12px rgba(59, 91, 219, 0.25);
}
.brand-name { font-size: 16px; font-weight: 650; color: var(--text-strong); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ver-badge {
  font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
  color: var(--primary); background: var(--primary-softer);
  padding: 3px 10px; border-radius: var(--radius-pill); white-space: nowrap;
  border: 1px solid var(--primary-soft);
}
.topbar-actions { display: flex; align-items: center; gap: 10px; }
.liveness {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--text-muted);
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: var(--radius-pill); padding: 6px 14px; white-space: nowrap;
  transition: background-color 0.2s ease, border-color 0.2s ease;
}
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; background: var(--text-muted); transition: background-color 0.2s ease; }
.dot-ok { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-softer); }
.dot-err { background: var(--err); box-shadow: 0 0 0 3px var(--err-soft); }
.dot-warn { background: var(--warn); box-shadow: 0 0 0 3px var(--warn-soft); }
.dot-unknown { background: var(--text-muted); box-shadow: 0 0 0 3px var(--primary-softer); }
.dot-pulse { animation: pulseDot 2s ease-in-out infinite; }
@keyframes pulseDot { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.45; transform: scale(0.85); } }
[data-theme="dark"] .dot-ok { box-shadow: 0 0 0 3px var(--ok-softer); }

.icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 38px; height: 38px; border-radius: var(--radius-sm);
  border: 1px solid var(--border); background: var(--surface-2); color: var(--text);
  cursor: pointer; transition: border-color 0.15s, color 0.15s, background-color 0.15s, transform 0.15s;
}
.icon-btn:hover { border-color: var(--primary); color: var(--primary); background: var(--primary-softer); }
.icon-btn:active { transform: scale(0.94); }
.theme-icon { display: block; }
[data-theme="dark"] .theme-icon-sun { display: none; }
[data-theme="light"] .theme-icon-moon { display: none; }

.topbar-nav { max-width: 1180px; margin: 0 auto; padding: 0 22px 14px; overflow-x: auto; }
.tablist {
  display: inline-flex; gap: 4px; padding: 5px;
  background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-pill);
}
.tab {
  display: inline-flex; align-items: center; gap: 6px;
  border: 0; background: transparent; color: var(--text-muted);
  font: inherit; font-size: 13.5px; font-weight: 550;
  padding: 8px 18px; border-radius: var(--radius-pill); cursor: pointer; white-space: nowrap;
  transition: background-color 0.18s, color 0.18s, box-shadow 0.18s;
}
.tab:hover { color: var(--text-strong); }
.tab[aria-selected="true"] {
  background: var(--surface); color: var(--primary); font-weight: 650;
  box-shadow: var(--shadow-1);
}
.tab:focus-visible, .icon-btn:focus-visible, .btn:focus-visible,
.field input:focus-visible, .field select:focus-visible, .log-toolbar input:focus-visible {
  outline: 2px solid var(--primary); outline-offset: 2px;
}

/* ============================================================
   5. 顶部细提示条（sparkle + 一行小字 + → 跳转）
   ============================================================ */
.hint-strip {
  display: flex; align-items: center; gap: 10px; justify-content: center;
  margin: 0 auto 18px; padding: 10px 18px; max-width: 1180px;
  background: var(--hint-bg); border: 1px solid var(--hint-border);
  border-radius: var(--radius-pill); color: var(--text-muted);
  font-size: 13px; line-height: 1;
  position: relative; z-index: 1;
  animation: slideDown 0.32s cubic-bezier(0.2, 0.9, 0.3, 1.1);
}
.hint-strip-icon { font-size: 14px; }
.hint-strip-text { color: var(--text); }
.hint-strip-link {
  display: inline-flex; align-items: center; gap: 3px;
  color: var(--primary); font-weight: 600;
  padding: 2px 10px; border-radius: var(--radius-pill);
  background: var(--surface); border: 1px solid var(--hint-border);
  transition: transform 0.15s, background-color 0.15s;
}
.hint-strip-link:hover { transform: translateX(2px); background: var(--primary-softer); }
@keyframes slideDown { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: none; } }

/* ============================================================
   6. 面板 / 卡片 / KPI（静态 opacity: 1 兜底，防止白屏）
   ============================================================ */
.panel { display: none; }
.panel.active {
  display: block;
  opacity: 1;
  animation: panelFadeIn 0.28s cubic-bezier(0.2, 0.9, 0.3, 1.1);
}
@keyframes panelFadeIn {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
.grid {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-1);
  padding: 22px;
  transition: box-shadow 0.2s ease, transform 0.2s ease, border-color 0.2s ease;
}
.card:hover { box-shadow: var(--shadow-2); }
.kpi { display: flex; flex-direction: column; gap: 8px; min-height: 124px; }
.kpi-label {
  font-size: 12px; font-weight: 650;
  color: var(--text-muted);
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.kpi-value {
  display: inline-flex; align-items: center; gap: 10px;
  font-size: 22px; font-weight: 700; line-height: 1.3;
  color: var(--text-strong); word-break: break-all;
}
.kpi-value.small { font-size: 15.5px; font-weight: 600; }
.kpi-sub { font-size: 12px; color: var(--text-faint); }
.kpi-alert { border-color: var(--warn); box-shadow: 0 0 0 3px var(--warn-soft), var(--shadow-1); }
.kpi-alert-err { border-color: var(--err); box-shadow: 0 0 0 3px var(--err-soft), var(--shadow-1); }
.tone-ok { color: var(--ok); }
.tone-err { color: var(--err); }
.tone-warn { color: var(--warn); }
.tone-muted { color: var(--text-muted); }
.action-card {
  display: flex; flex-direction: column; align-items: center; gap: 14px;
  text-align: center; padding: 32px 22px; margin-top: 16px;
  background: linear-gradient(180deg, var(--surface) 0%, var(--surface-2) 100%);
}

/* ============================================================
   7. 按钮 / 表单 / 开关 / 徽章
   ============================================================ */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 10px 20px; border-radius: var(--radius-pill);
  border: 1px solid var(--primary); background: var(--primary); color: #fff;
  font: inherit; font-size: 14px; font-weight: 600; cursor: pointer; text-decoration: none;
  transition: background-color 0.18s, border-color 0.18s, color 0.18s, transform 0.12s, box-shadow 0.18s, opacity 0.15s;
  box-shadow: 0 4px 14px rgba(59, 91, 219, 0.22);
}
.btn:hover:not(:disabled) { background: var(--primary-strong); border-color: var(--primary-strong); box-shadow: 0 6px 20px rgba(79, 70, 229, 0.30); }
.btn:active:not(:disabled) { transform: translateY(1px); box-shadow: 0 2px 6px rgba(59, 91, 219, 0.20); }
.btn:disabled { opacity: 0.55; cursor: not-allowed; box-shadow: none; }
.btn-secondary {
  background: var(--surface); color: var(--primary); border-color: var(--border);
  box-shadow: none;
}
.btn-secondary:hover:not(:disabled) { background: var(--primary-softer); border-color: var(--primary); color: var(--primary); box-shadow: none; }
.btn-danger { background: transparent; color: var(--err); border-color: var(--err); box-shadow: none; }
.btn-danger:hover:not(:disabled) { background: var(--err); color: #fff; border-color: var(--err); box-shadow: 0 4px 14px rgba(239, 68, 68, 0.25); }
.btn-lg { padding: 14px 36px; font-size: 15px; border-radius: var(--radius-pill); min-width: 200px; }
.btn-spinner {
  display: none; width: 14px; height: 14px; border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, 0.45); border-top-color: #fff;
  animation: spin 0.7s linear infinite;
}
.btn.loading .btn-spinner { display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
.section { margin-bottom: 16px; }
.section-head { margin-bottom: 16px; }
.section-title { font-size: 15.5px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; color: var(--text-strong); }
.section-desc { font-size: 12.5px; color: var(--text-muted); margin-top: 4px; }
.field { position: relative; margin-bottom: 18px; }
.field:last-child { margin-bottom: 0; }
.field > label:not(.switch) { display: block; font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--text-strong); }
.field input[type=text], .field input[type=number], .field input[type=password], .field select, .log-toolbar input {
  width: 100%; padding: 12px 14px; font: inherit; font-size: 14px;
  color: var(--text); background: var(--surface-2);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  transition: border-color 0.15s, box-shadow 0.15s, background-color 0.15s;
}
.field input:focus, .field select:focus, .log-toolbar input:focus {
  outline: none; border-color: var(--primary); background: var(--surface);
  box-shadow: 0 0 0 4px var(--primary-soft);
}
.field input.is-invalid, .field select.is-invalid { border-color: var(--err); box-shadow: 0 0 0 4px var(--err-soft); }
.field input[type=number].cfg-lg, .field input[type=text].cfg-lg { padding: 14px 16px; font-size: 15px; }
.hint { font-size: 12px; color: var(--text-faint); margin-top: 6px; }
.err { font-size: 12px; color: var(--err); margin-top: 6px; }
.err:empty { display: none; }
.switch { position: relative; display: inline-flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
.switch input { position: absolute; width: 1px; height: 1px; opacity: 0; margin: 0; }
.switch .track {
  position: relative; width: 46px; height: 26px; border-radius: var(--radius-pill);
  background: var(--track); flex: none; transition: background-color 0.2s;
}
.switch .track::after {
  content: ''; position: absolute; top: 3px; left: 3px;
  width: 20px; height: 20px; border-radius: 50%; background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25); transition: transform 0.2s;
}
.switch input:checked + .track { background: var(--ok); }
.switch input:checked + .track::after { transform: translateX(20px); }
.switch input:focus-visible + .track { outline: 2px solid var(--primary); outline-offset: 2px; }
.switch-label { font-size: 13.5px; font-weight: 600; }
.badge {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 12px; font-weight: 650; padding: 3px 10px;
  border-radius: var(--radius-pill); border: 1px solid transparent; white-space: nowrap;
}
.badge-ok { background: var(--ok-soft); color: var(--ok); }
.badge-err { background: var(--err-soft); color: var(--err); }
.badge-muted { background: var(--surface-2); color: var(--text-muted); border-color: var(--border); }
.card-alert { border-color: var(--err); box-shadow: 0 0 0 3px var(--err-soft), var(--shadow-1); }
.save-bar {
  position: sticky; bottom: 16px; z-index: 20;
  display: flex; justify-content: flex-end; gap: 12px; align-items: center; flex-wrap: wrap;
  padding: 14px 18px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-md); box-shadow: var(--shadow-lift);
}
.save-bar .muted { margin-right: auto; font-size: 13px; }

/* ============================================================
   8. 日志终端
   ============================================================ */
.log-toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
.log-toolbar .grow { flex: 1 1 220px; min-width: 180px; }
.log-box {
  background: var(--log-bg); color: var(--log-text);
  border: 1px solid var(--border); border-radius: var(--radius-md);
  padding: 16px 18px; height: min(58vh, 540px); overflow: auto;
  font-family: "Cascadia Mono", "JetBrains Mono", "Consolas", "SFMono-Regular", "Courier New", monospace;
  font-size: 12.5px; line-height: 1.7; white-space: pre-wrap; word-break: break-all;
}
.log-box::-webkit-scrollbar { width: 10px; height: 10px; }
.log-box::-webkit-scrollbar-thumb { background: #475569; border-radius: 6px; }
.log-box::-webkit-scrollbar-track { background: transparent; }
.log-box.is-empty { color: var(--log-dim); font-style: italic; }
.log-meta {
  display: flex; gap: 16px; flex-wrap: wrap;
  font-size: 12px; color: var(--text-muted); margin-top: 12px;
  padding: 10px 14px; background: var(--surface-2); border-radius: var(--radius-sm); border: 1px solid var(--border);
}

/* ============================================================
   9. 关于
   ============================================================ */
.info {
  display: grid; grid-template-columns: 140px 1fr; gap: 12px 20px;
  font-size: 13.5px; margin: 0;
}
.info dt { color: var(--text-muted); padding-top: 2px; }
.info dd { margin: 0; word-break: break-all; color: var(--text-strong); }
code.path {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 3px 10px; font-family: "Cascadia Mono", "JetBrains Mono", "Consolas", monospace; font-size: 12.5px;
  color: var(--text-strong);
}
.link-row { display: flex; gap: 10px; flex-wrap: wrap; }

/* ============================================================
   10. Toast
   ============================================================ */
.toasts {
  position: fixed; top: 18px; right: 18px; z-index: 100;
  display: flex; flex-direction: column; gap: 10px;
  max-width: min(92vw, 380px); pointer-events: none;
}
.toast {
  display: flex; gap: 10px; align-items: flex-start;
  background: var(--surface); border: 1px solid var(--border);
  border-left: 4px solid var(--primary);
  border-radius: var(--radius-sm); box-shadow: var(--shadow-lift);
  padding: 12px 16px; font-size: 13.5px; color: var(--text);
  animation: toastIn 0.24s cubic-bezier(0.2, 0.9, 0.3, 1.2);
}
.toast.success { border-left-color: var(--ok); }
.toast.error { border-left-color: var(--err); }
.toast.warn { border-left-color: var(--warn); }
.toast.out { animation: toastOut 0.26s ease forwards; }
.toast-icon { line-height: 1.5; font-size: 16px; flex: none; }
.toast-msg { word-break: break-word; }
@keyframes toastIn { from { opacity: 0; transform: translateX(24px) scale(0.97); } to { opacity: 1; transform: none; } }
@keyframes toastOut { to { opacity: 0; transform: translateX(24px); } }

/* ============================================================
   10.5 升级横幅（v1.3 新增）— 黄/红/绿三色变体 + 进度条
   ============================================================ */
.update-banner {
  display: flex; align-items: flex-start; gap: 14px;
  padding: 14px 18px; margin-bottom: 16px;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md);
  box-shadow: var(--shadow-1);
  animation: panelFadeIn 0.24s cubic-bezier(0.2, 0.9, 0.3, 1.1);
}
.update-banner[hidden] { display: none; }
.update-banner-icon {
  width: 36px; height: 36px; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--primary-softer); color: var(--primary);
  border-radius: 10px; font-size: 18px; font-weight: 700;
}
.update-banner-body { flex: 1 1 auto; min-width: 0; }
.update-banner-title { font-size: 14px; font-weight: 650; color: var(--text-strong); margin-bottom: 2px; }
.update-banner-desc { font-size: 12.5px; color: var(--text-muted); word-break: break-word; }
.update-progress {
  margin-top: 10px; height: 6px; border-radius: var(--radius-pill);
  background: var(--surface-2); overflow: hidden;
}
.update-progress[hidden] { display: none; }
.update-progress-bar {
  height: 100%; background: var(--primary); border-radius: var(--radius-pill);
  transition: width 0.2s ease;
}
.update-banner-action {
  flex: none;
  padding: 6px 14px; border-radius: var(--radius-pill);
  border: 1px solid var(--border); background: var(--surface-2); color: var(--text);
  font: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer;
  transition: background-color 0.15s, border-color 0.15s, color 0.15s;
}
.update-banner-action[hidden] { display: none; }
.update-banner-action:hover { background: var(--primary-softer); border-color: var(--primary); color: var(--primary); }
.update-banner-dismiss {
  flex: none; width: 28px; height: 28px; padding: 0;
  border: 0; background: transparent; color: var(--text-faint);
  cursor: pointer; border-radius: var(--radius-sm);
  font-size: 14px; line-height: 1;
  transition: background-color 0.15s, color 0.15s;
}
.update-banner-dismiss:hover { background: var(--surface-2); color: var(--text); }
.update-banner.warn {
  background: var(--warn-soft); border-color: var(--warn);
}
.update-banner.warn .update-banner-icon { background: var(--warn); color: #fff; }
.update-banner.warn .update-banner-progress-bar { background: var(--warn); }
.update-banner.error {
  background: var(--err-soft); border-color: var(--err);
}
.update-banner.error .update-banner-icon { background: var(--err); color: #fff; }
.update-banner.success {
  background: var(--ok-soft); border-color: var(--ok);
}
.update-banner.success .update-banner-icon { background: var(--ok); color: #fff; }

/* ============================================================
   11. 响应式（≤720px 平板；≤480px 手机）
   ============================================================ */
@media (max-width: 720px) {
  .wrap { padding: 14px 16px 52px; }
  .topbar-inner { padding: 12px 16px 10px; }
  .topbar-nav { padding: 0 16px 12px; }
  .hint-strip { padding: 9px 14px; font-size: 12.5px; flex-wrap: wrap; }
  .grid { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .brand-name { font-size: 15px; }
  .kpi { min-height: 100px; }
  .kpi-value { font-size: 19px; }
  .kpi-value.small { font-size: 14.5px; }
  .info { grid-template-columns: 1fr; gap: 4px; }
  .info dt { margin-top: 10px; }
  .btn-lg { width: 100%; }
  .save-bar { justify-content: stretch; bottom: 12px; }
  .save-bar .btn { flex: 1 1 auto; }
  .card { padding: 18px; }
}
@media (max-width: 480px) {
  body { font-size: 14px; }
  .wrap { padding: 12px 12px 44px; }
  .topbar-inner { padding: 10px 12px 8px; }
  .topbar-nav { padding: 0 12px 10px; }
  .hint-strip { font-size: 12px; padding: 8px 12px; }
  .grid { grid-template-columns: 1fr; gap: 12px; }
  .kpi { min-height: 88px; padding: 16px; }
  .kpi-value { font-size: 18px; }
  .kpi-value.small { font-size: 14px; }
  .card { padding: 16px; border-radius: var(--radius-sm); }
  .action-card { padding: 24px 16px; }
  .tab { padding: 7px 14px; font-size: 13px; }
  .liveness { padding: 5px 12px; font-size: 12.5px; }
  .save-bar { padding: 12px; flex-direction: column; align-items: stretch; }
  .save-bar .muted { margin-right: 0; margin-bottom: 4px; text-align: center; }
  .save-bar .btn { width: 100%; }
  .log-meta { font-size: 11.5px; padding: 8px 12px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; }
}
</style>
<script>
/* 首屏主题：localStorage 优先，否则跟随系统 prefers-color-scheme（在 <style> 之后、body 之前执行，避免闪烁） */
(function () {
  var theme = 'light';
  try {
    var saved = localStorage.getItem('drcom-theme');
    if (saved === 'dark' || saved === 'light') {
      theme = saved;
    } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      theme = 'dark';
    }
  } catch (e) { /* 隐私模式下 localStorage 不可用，回落亮色 */ }
  document.documentElement.setAttribute('data-theme', theme);
})();
</script>
</head>
<body>
<a class="skip" href="#main">跳到主要内容</a>

<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">D</span>
      <span class="brand-name">Dr.COM 校园网自动登录</span>
      <span class="ver-badge" id="ver-badge">v-</span>
    </div>
    <div class="topbar-actions">
      <span class="liveness" role="status" aria-live="polite" title="服务实时状态">
        <span class="dot dot-unknown dot-pulse" id="liveness-dot" aria-hidden="true"></span>
        <span id="liveness-text">连接中…</span>
      </span>
      <button class="icon-btn" id="btn-theme" type="button" aria-label="切换亮色 / 暗色主题" title="切换主题">
        <svg class="theme-icon theme-icon-sun" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4"></circle>
          <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"></path>
        </svg>
        <svg class="theme-icon theme-icon-moon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"></path>
        </svg>
      </button>
    </div>
  </div>
  <div class="topbar-nav">
    <div class="tablist" role="tablist" aria-label="功能分区">
      <button class="tab" id="tab-status" role="tab" type="button" aria-selected="true" aria-controls="panel-status" data-tab="status">📊 状态</button>
      <button class="tab" id="tab-config" role="tab" type="button" aria-selected="false" tabindex="-1" aria-controls="panel-config" data-tab="config">⚙️ 配置</button>
      <button class="tab" id="tab-log" role="tab" type="button" aria-selected="false" tabindex="-1" aria-controls="panel-log" data-tab="log">📝 日志</button>
      <button class="tab" id="tab-about" role="tab" type="button" aria-selected="false" tabindex="-1" aria-controls="panel-about" data-tab="about">ℹ️ 关于</button>
    </div>
  </div>
</header>

<div class="hint-strip" role="note">
  <span class="hint-strip-icon" aria-hidden="true">✨</span>
  <span class="hint-strip-text">本服务仅监听 127.0.0.1，所有数据保存在本机；密码仅保存到 password.txt。</span>
  <a class="hint-strip-link" href="https://github.com/TSS-Small-sunshine/DrcomAutoLogin-Windows" target="_blank" rel="noopener noreferrer">查看源码 →</a>
</div>

<main class="wrap" id="main">

  <!-- ============ 状态 ============ -->
  <section class="panel active" id="panel-status" role="tabpanel" aria-labelledby="tab-status" tabindex="-1">
    <!-- v1.3 新增：自动升级横幅（默认 hidden，由 JS 按 state 控制显隐） -->
    <div id="update-banner" class="update-banner" hidden>
      <div class="update-banner-icon" id="update-banner-icon" aria-hidden="true">⬆</div>
      <div class="update-banner-body">
        <div class="update-banner-title" id="update-banner-title">检查更新...</div>
        <div class="update-banner-desc" id="update-banner-desc"></div>
        <div class="update-progress" id="update-progress" hidden>
          <div class="update-progress-bar" id="update-progress-bar" style="width:0%"></div>
        </div>
      </div>
      <button class="update-banner-action" id="update-banner-action" type="button" hidden></button>
      <button class="update-banner-dismiss" id="update-banner-dismiss" type="button" aria-label="关闭横幅">✕</button>
    </div>

    <div class="grid">
      <article class="card kpi" id="card-net">
        <div class="kpi-label">网络可达性</div>
        <div class="kpi-value" id="kpi-net">
          <span class="dot dot-unknown" id="kpi-net-dot" aria-hidden="true"></span>
          <span id="kpi-net-text">未知</span>
        </div>
        <div class="kpi-sub" id="kpi-net-sub">等待首次检查</div>
      </article>

      <article class="card kpi" id="card-online">
        <div class="kpi-label">在线状态</div>
        <div class="kpi-value" id="kpi-online">
          <span class="dot dot-unknown" id="kpi-online-dot" aria-hidden="true"></span>
          <span id="kpi-online-text">未知</span>
        </div>
        <div class="kpi-sub" id="kpi-online-sub">登录结果：-</div>
      </article>

      <article class="card kpi">
        <div class="kpi-label">当前账号</div>
        <div class="kpi-value small mono" id="kpi-account">-</div>
        <div class="kpi-sub" id="kpi-account-sub">来自配置文件</div>
      </article>

      <article class="card kpi">
        <div class="kpi-label">上次登录时间</div>
        <div class="kpi-value small" id="kpi-lastlogin">从未</div>
        <div class="kpi-sub" id="kpi-lastlogin-sub">尚无登录记录</div>
      </article>

      <article class="card kpi" id="card-error">
        <div class="kpi-label">上次错误</div>
        <div class="kpi-value small" id="kpi-error">无</div>
        <div class="kpi-sub" id="kpi-error-sub">最近一次检查未报错</div>
      </article>

      <article class="card kpi">
        <div class="kpi-label">下次检查</div>
        <div class="kpi-value mono" id="kpi-next">未计划</div>
        <div class="kpi-sub" id="kpi-next-sub">-</div>
      </article>
    </div>

    <div class="card action-card">
      <button class="btn btn-lg" id="btn-login" type="button">
        <span class="btn-spinner" aria-hidden="true"></span>
        <span id="btn-login-label">🔄 立即登录</span>
      </button>
      <p class="muted" id="login-hint" style="margin:0;font-size:12.5px;">点击按钮立即触发一次完整的网络检查与登录流程。</p>
    </div>
  </section>

  <!-- ============ 配置 ============ -->
  <section class="panel" id="panel-config" role="tabpanel" aria-labelledby="tab-config" tabindex="-1">
    <div class="card section" id="card-password">
      <div class="section-head">
        <h2 class="section-title">账户与登录密码 <span class="badge badge-muted" id="pwd-badge">状态未知</span></h2>
        <p class="section-desc" style="margin:0;">账号 + 运营商 + 密码构成本机登录校园网的完整凭据。密码仅保存于本机 password.txt，保存后立即生效，无需重启。</p>
      </div>
      <div class="field">
        <label for="cfg-account">账号</label>
        <input type="text" id="cfg-account" class="cfg-lg" placeholder="学号 / 工号（纯数字）" autocomplete="off" spellcheck="false" inputmode="numeric">
        <div class="hint">仅支持数字，例如 2023123456</div>
        <div class="err" id="err-account" role="alert"></div>
      </div>
      <div class="field">
        <label for="cfg-suffix">运营商</label>
        <select id="cfg-suffix">
          <option value="">校园用户（无后缀）</option>
          <option value="@yd">中国移动 @yd</option>
          <option value="@dx">中国电信 @dx</option>
          <option value="@lt">中国联通 @lt</option>
        </select>
        <div class="hint">宽带运营商不同，认证域名后缀也不同</div>
      </div>
      <div class="field">
        <label for="pwd-new">账户登录密码</label>
        <input type="password" id="pwd-new" autocomplete="new-password">
        <div class="hint">至少 1 个字符</div>
      </div>
      <div class="field">
        <label for="pwd-confirm">再次输入账户登录密码</label>
        <input type="password" id="pwd-confirm" autocomplete="new-password">
        <div class="err" id="err-pwd" role="alert"></div>
      </div>
      <button class="btn btn-secondary" id="btn-save-pwd" type="button">🔑 保存账户登录密码</button>
    </div>

    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">认证服务器</h2>
        <p class="section-desc" style="margin:0;">校园网认证网关地址与端口。</p>
      </div>
      <div class="field">
        <label for="cfg-host">认证服务器地址</label>
        <input type="text" id="cfg-host" class="cfg-lg" placeholder="例如 172.16.80.3" autocomplete="off" spellcheck="false">
        <div class="hint">认证网关的 IP 或域名</div>
        <div class="err" id="err-host" role="alert"></div>
      </div>
      <div class="field">
        <label for="cfg-port">认证端口</label>
        <input type="number" id="cfg-port" class="cfg-lg" min="1" max="65535" step="1" inputmode="numeric">
        <div class="hint">取值 1-65535，通常为 80</div>
        <div class="err" id="err-port" role="alert"></div>
      </div>
    </div>

    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">自动化</h2>
        <p class="section-desc" style="margin:0;">后台线程按间隔自动检查网络，掉线即重新登录。</p>
      </div>
      <div class="field">
        <label class="switch" for="cfg-auto-enabled">
          <input type="checkbox" id="cfg-auto-enabled">
          <span class="track" aria-hidden="true"></span>
          <span class="switch-label">启用周期自检</span>
        </label>
        <div class="hint">关闭后仅在启动时与手动点击时检查</div>
      </div>
      <div class="field">
        <label for="cfg-auto-interval">检查间隔</label>
        <select id="cfg-auto-interval">
          <option value="5">5 分钟</option>
          <option value="15">15 分钟</option>
          <option value="30">30 分钟</option>
          <option value="60">60 分钟</option>
          <option value="120">120 分钟</option>
        </select>
        <div class="hint">连续失败时服务会自动退避（5 → 60 分钟）</div>
      </div>
      <div class="field">
        <label for="cfg-network-timeout">网络等待超时（秒）</label>
        <input type="number" id="cfg-network-timeout" class="cfg-lg" min="10" max="300" step="1" inputmode="numeric">
        <div class="hint">每次检查等待校园网可达的最长时间，10-300 秒</div>
        <div class="err" id="err-timeout" role="alert"></div>
      </div>
      <!-- v1.3 新增：自动升级字段 -->
      <div class="field">
        <label class="switch" for="cfg-auto-update-enabled">
          <input type="checkbox" id="cfg-auto-update-enabled">
          <span class="track" aria-hidden="true"></span>
          <span class="switch-label">启用自动升级（GitHub 检测）</span>
        </label>
        <div class="hint">关闭后仅在启动时与手动点击时检查 GitHub 新版</div>
      </div>
      <div class="field">
        <label for="cfg-update-interval">自动升级检查间隔</label>
        <select id="cfg-update-interval">
          <option value="6">6 小时</option>
          <option value="12">12 小时</option>
          <option value="24">24 小时</option>
        </select>
        <div class="hint">服务会定期访问 GitHub API 检查新版（未认证 60 req/h）</div>
      </div>
      <div class="field">
        <label for="cfg-update-disk">下载前最小剩余磁盘（MB）</label>
        <input type="number" id="cfg-update-disk" class="cfg-lg" min="50" max="10240" step="1" inputmode="numeric">
        <div class="hint">下载安装包前要求磁盘剩余 ≥ 此值（50-10240 MB，默认 200）</div>
      </div>
    </div>

    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">Web UI</h2>
        <p class="section-desc" style="margin:0;">管理页面本身的服务端口，修改后需重启服务生效。</p>
      </div>
      <div class="field">
        <label for="cfg-ui-port">监听端口</label>
        <input type="number" id="cfg-ui-port" class="cfg-lg" min="1024" max="65535" step="1" inputmode="numeric">
        <div class="hint">取值 1024-65535，仅监听 127.0.0.1，默认 8848</div>
        <div class="err" id="err-ui-port" role="alert"></div>
      </div>
    </div>

    <div class="save-bar">
      <span class="muted" id="config-state">配置在打开本页时读取</span>
      <button class="btn btn-lg" id="btn-save-config" type="button">💾 保存配置</button>
    </div>
  </section>

  <!-- ============ 日志 ============ -->
  <section class="panel" id="panel-log" role="tabpanel" aria-labelledby="tab-log" tabindex="-1">
    <div class="card">
      <div class="log-toolbar">
        <input type="text" class="grow" id="log-filter" placeholder="过滤关键字（留空显示全部）" aria-label="日志关键字过滤" autocomplete="off" spellcheck="false">
        <button class="btn btn-secondary" id="btn-log-refresh" type="button">🔄 刷新</button>
        <button class="btn btn-secondary" id="btn-log-download" type="button">⬇ 下载日志</button>
        <label class="switch" for="log-autoscroll">
          <input type="checkbox" id="log-autoscroll" checked>
          <span class="track" aria-hidden="true"></span>
          <span class="switch-label">自动滚动</span>
        </label>
      </div>
      <div class="log-box is-empty" id="log-box" role="log" aria-live="off" tabindex="0">暂无日志</div>
      <div class="log-meta">
        <span id="log-count">0 行</span>
        <span id="log-offset">offset 0</span>
        <span id="log-size">0 字节</span>
        <span id="log-updated">未更新</span>
      </div>
    </div>
  </section>

  <!-- ============ 关于 ============ -->
  <section class="panel" id="panel-about" role="tabpanel" aria-labelledby="tab-about" tabindex="-1">
    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">版本信息</h2>
      </div>
      <dl class="info">
        <dt>版本号</dt><dd id="about-version">-</dd>
        <dt>服务启动时间</dt><dd id="about-started">-</dd>
        <dt>已运行时长</dt><dd id="about-uptime" class="mono">-</dd>
      </dl>
    </div>

    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">文件位置</h2>
      </div>
      <dl class="info">
        <dt>配置文件</dt><dd id="about-config"><code class="path">-</code></dd>
        <dt>日志文件</dt><dd id="about-log"><code class="path">-</code></dd>
        <dt>数据目录</dt><dd id="about-data"><code class="path">-</code></dd>
      </dl>
    </div>

    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">访问入口</h2>
      </div>
      <div class="link-row">
        <a class="btn btn-secondary" id="about-local" href="http://127.0.0.1:8848" target="_blank" rel="noopener">🖥 本机管理页面</a>
        <a class="btn" href="https://github.com/TSS-Small-sunshine/DrcomAutoLogin-Windows" target="_blank" rel="noopener noreferrer">📦 在 GitHub 上查看</a>
      </div>
      <p class="hint" style="margin-top:14px;">
        本页面仅监听本机回环地址（127.0.0.1），局域网内其他设备无法访问；所有配置、密码与日志文件都保存在程序所在的数据目录中，删除目录即彻底清除。
      </p>
    </div>

    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">管理操作</h2>
      </div>
      <div class="btn-row">
        <button class="btn btn-secondary" id="btn-restart" type="button">🔁 重启服务</button>
        <button class="btn btn-danger" id="btn-uninstall" type="button">🗑 卸载服务</button>
        <!-- v1.3 新增：升级历史按钮 -->
        <button class="btn btn-secondary" id="btn-update-history" type="button">📜 查看升级历史</button>
      </div>
      <p class="hint" id="admin-hint" style="margin-top:14px;"></p>
    </div>
  </section>
</main>

<div class="toasts" id="toasts" role="region" aria-live="polite" aria-label="通知"></div>

<!-- v1.3 新增：升级历史弹窗（默认隐藏，由 JS 控制） -->
<div id="update-history-modal" class="update-modal" hidden role="dialog" aria-modal="true" aria-labelledby="update-history-title">
  <div class="update-modal-backdrop" id="update-history-backdrop"></div>
  <div class="update-modal-card card">
    <div class="section-head" style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
      <h2 class="section-title" id="update-history-title">📜 升级历史</h2>
      <button class="icon-btn" id="update-history-close" type="button" aria-label="关闭">✕</button>
    </div>
    <p class="hint" id="update-history-path" style="margin:0 0 10px;"></p>
    <pre class="update-modal-log" id="update-history-log">加载中…</pre>
    <div class="btn-row" style="margin-top:14px;justify-content:flex-end;">
      <button class="btn btn-secondary" id="update-history-refresh" type="button">🔄 刷新</button>
      <button class="btn" id="update-history-close-btn" type="button">关闭</button>
    </div>
  </div>
</div>

<style>
/* 升级历史弹窗（v1.3 新增；放在 body 末尾避免影响其他 CSS） */
.update-modal {
  position: fixed; inset: 0; z-index: 200;
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
}
.update-modal[hidden] { display: none; }
.update-modal-backdrop {
  position: absolute; inset: 0;
  background: rgba(15, 23, 42, 0.55);
  animation: panelFadeIn 0.2s ease;
}
.update-modal-card {
  position: relative; z-index: 1;
  width: min(720px, 100%); max-height: 80vh;
  display: flex; flex-direction: column;
  background: var(--surface);
}
.update-modal-log {
  flex: 1 1 auto; min-height: 280px; max-height: 60vh; overflow: auto;
  background: var(--log-bg); color: var(--log-text);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 14px 16px;
  font-family: "Cascadia Mono", "JetBrains Mono", "Consolas", monospace;
  font-size: 12.5px; line-height: 1.65; white-space: pre-wrap; word-break: break-all;
  margin: 0;
}
</style>

<script>
(function () {
  'use strict';

  /* ============================================================
     分区 1/6 · 工具函数
     ============================================================ */
  var THEME_KEY = 'drcom-theme';
  var POLL_STATUS_MS = 3000;
  var POLL_LOG_MS = 2000;
  var LOG_MAX_LINES = 4000;

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function text(el, v) { if (el) el.textContent = v; }

  function fmtIso(iso) {
    if (!iso) return '-';
    return String(iso).replace('T', ' ');
  }

  function fmtTimeOnly(iso) {
    if (!iso || typeof iso !== 'string') return '-';
    var i = iso.indexOf('T');
    var s = i >= 0 ? iso.slice(i + 1) : iso;
    return s.length >= 8 ? s.slice(0, 8) : s;
  }

  function fmtDuration(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    var out = [];
    if (d) out.push(d + ' 天');
    if (d || h) out.push(h + ' 小时');
    out.push(m + ' 分');
    out.push(s + ' 秒');
    return out.join(' ');
  }

  function fmtCountdown(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    return (h > 0 ? pad(h) + ':' : '') + pad(m) + ':' + pad(s);
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' 字节';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(2) + ' MB';
  }

  function nowClock() {
    var d = new Date();
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  /* Toast：右上角浮出，默认 3 秒自动消失 */
  function toast(msg, type, ms) {
    var box = $('toasts');
    if (!box) return;
    var el = document.createElement('div');
    var icon = type === 'success' ? '✅' : (type === 'error' ? '❌' : (type === 'warn' ? '⚠️' : 'ℹ️'));
    el.className = 'toast ' + (type || 'info');
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    el.innerHTML = '<span class="toast-icon" aria-hidden="true">' + icon + '</span>' +
                   '<span class="toast-msg">' + esc(msg) + '</span>';
    box.appendChild(el);
    var life = ms || 3000;
    setTimeout(function () { el.classList.add('out'); }, life);
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, life + 260);
  }

  /* ============================================================
     分区 2/6 · API 客户端（路径 / 方法 / 字段严格对应后端）
     ============================================================ */
  function getJson(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) { return r.json(); });
  }

  function postJson(url, body) {
    var opt = { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opt.body = JSON.stringify(body);
    return fetch(url, opt).then(function (r) { return r.json(); });
  }

  var API = {
    status: getJson.bind(null, '/api/status'),
    config: getJson.bind(null, '/api/config'),
    about: getJson.bind(null, '/api/about'),
    logPath: getJson.bind(null, '/api/log_file_path'),
    logTail: function (offset, max) {
      return getJson('/api/log_tail?offset=' + offset + '&max=' + max);
    },
    login: function () { return postJson('/api/login'); },
    saveConfig: function (cfg) { return postJson('/api/config', cfg); },
    savePassword: function (pwd) { return postJson('/api/password', { password: pwd }); },
    restart: function () { return postJson('/api/restart'); }
  };

  /* ============================================================
     分区 3/6 · 主题
     ============================================================ */
  function systemTheme() {
    return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  }

  function applyTheme(theme, persist) {
    document.documentElement.setAttribute('data-theme', theme);
    var btn = $('btn-theme');
    if (btn) btn.setAttribute('aria-label', theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题');
    if (persist) {
      try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* 忽略隐私模式限制 */ }
    }
  }

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }

  function bindTheme() {
    var btn = $('btn-theme');
    if (btn) {
      btn.addEventListener('click', function () {
        var next = currentTheme() === 'dark' ? 'light' : 'dark';
        applyTheme(next, true);
        toast(next === 'dark' ? '已切换到暗色主题' : '已切换到亮色主题', 'info', 2000);
      });
    }
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onChange = function () {
        var saved = null;
        try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
        if (saved !== 'dark' && saved !== 'light') applyTheme(systemTheme(), false);
      };
      if (mq.addEventListener) mq.addEventListener('change', onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  }

  /* ============================================================
     分区 4/6 · 状态刷新
     ============================================================ */
  var statusTimer = null;
  var countdownDeadline = null;
  var countdownAt = '';
  var loginWatch = null;

  function setDot(id, tone, pulse) {
    var el = $(id);
    if (!el) return;
    el.className = 'dot dot-' + tone + (pulse ? ' dot-pulse' : '');
  }

  function setLiveness(tone, msg, pulse) {
    setDot('liveness-dot', tone, pulse);
    text($('liveness-text'), msg);
  }

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function renderStatus(s) {
    if (!s || typeof s !== 'object') { setLiveness('unknown', '状态未知', true); return; }

    /* —— 顶栏实时圆点 —— */
    if (s.online === true) setLiveness('ok', '已登录', false);
    else if (s.online === false) setLiveness('err', '未登录', false);
    else setLiveness('unknown', '状态未知', true);

    /* —— 网络可达性 —— */
    if (s.network_reachable === true) {
      setDot('kpi-net-dot', 'ok', false);
      text($('kpi-net-text'), '可达');
      $('kpi-net').className = 'kpi-value tone-ok';
    } else if (s.network_reachable === false) {
      setDot('kpi-net-dot', 'err', false);
      text($('kpi-net-text'), '不可达');
      $('kpi-net').className = 'kpi-value tone-err';
    } else {
      setDot('kpi-net-dot', 'unknown', true);
      text($('kpi-net-text'), '未知');
      $('kpi-net').className = 'kpi-value tone-muted';
    }
    text($('kpi-net-sub'), '上次检查 ' + fmtTimeOnly(s.last_check_at));

    /* —— 在线状态 —— */
    if (s.online === true) {
      setDot('kpi-online-dot', 'ok', false);
      text($('kpi-online-text'), '已登录');
      $('kpi-online').className = 'kpi-value tone-ok';
    } else if (s.online === false) {
      setDot('kpi-online-dot', 'err', false);
      text($('kpi-online-text'), '未登录');
      $('kpi-online').className = 'kpi-value tone-err';
    } else {
      setDot('kpi-online-dot', 'unknown', true);
      text($('kpi-online-text'), '未知');
      $('kpi-online').className = 'kpi-value tone-muted';
    }
    text($('kpi-online-sub'), '登录结果：' + (s.last_login_success === true ? '成功' : (s.last_login_success === false ? '失败' : '-')));

    /* —— 当前账号 —— */
    text($('kpi-account'), s.current_account ? s.current_account : '未配置');
    $('kpi-account').className = 'kpi-value small mono' + (s.current_account ? '' : ' tone-muted');
    text($('kpi-account-sub'), s.current_account ? '账号 + 运营商后缀' : '请到「配置」页填写账号');

    /* —— 上次登录时间 —— */
    text($('kpi-lastlogin'), s.last_login_at ? fmtIso(s.last_login_at) : '从未');
    $('kpi-lastlogin').className = 'kpi-value small' + (s.last_login_at ? '' : ' tone-muted');
    text($('kpi-lastlogin-sub'), s.last_login_at ? '最近一次登录尝试' : '尚无登录记录');

    /* —— 上次错误（有值 → 警示色边框） —— */
    var cardErr = $('card-error');
    if (s.last_error) {
      text($('kpi-error'), s.last_error);
      $('kpi-error').className = 'kpi-value small tone-warn';
      cardErr.className = 'card kpi kpi-alert';
      text($('kpi-error-sub'), '发生于 ' + fmtTimeOnly(s.last_check_at));
    } else {
      text($('kpi-error'), '无');
      $('kpi-error').className = 'kpi-value small tone-muted';
      cardErr.className = 'card kpi';
      text($('kpi-error-sub'), '最近一次检查未报错');
    }

    /* —— 下次检查：记录截止时间，由前端每秒倒计时（不再请求后端） —— */
    if (s.next_check_at && isNum(s.next_check_in_sec)) {
      countdownDeadline = Date.now() + Math.max(0, s.next_check_in_sec) * 1000;
      countdownAt = s.next_check_at;
    } else {
      countdownDeadline = null;
      countdownAt = '';
    }
    tickCountdown();

    /* —— 登录按钮 —— */
    if (loginWatch) {
      if (s.login_in_progress) loginWatch.sawProgress = true;
      else if (loginWatch.sawProgress || (s.last_check_at && s.last_check_at !== loginWatch.baseCheckAt)) {
        finishLogin('登录流程已完成，请查看上方状态与日志。', 'success');
      } else if (Date.now() > loginWatch.deadline) {
        finishLogin('等待登录结果超时，请查看日志确认。', 'warn');
      }
    }
    var btn = $('btn-login');
    if (btn) btn.disabled = !!s.login_in_progress || !!loginWatch;
  }

  function tickCountdown() {
    var el = $('kpi-next');
    if (!el) return;
    if (countdownDeadline === null) {
      el.textContent = '未计划';
      el.className = 'kpi-value mono tone-muted';
      text($('kpi-next-sub'), '周期自检未启用或尚未排期');
      return;
    }
    var left = Math.max(0, Math.round((countdownDeadline - Date.now()) / 1000));
    el.textContent = fmtCountdown(left);
    el.className = 'kpi-value mono' + (left <= 10 ? ' tone-warn' : '');
    text($('kpi-next-sub'), '剩余 ' + left + ' 秒 · 预计 ' + fmtTimeOnly(countdownAt) + ' 执行');
  }

  function pollStatus() {
    if (document.hidden) return;
    API.status().then(renderStatus).catch(function () {
      setLiveness('unknown', '服务无响应', true);
    });
  }

  function finishLogin(msg, type) {
    loginWatch = null;
    var btn = $('btn-login');
    if (btn) { btn.classList.remove('loading'); btn.disabled = false; }
    text($('btn-login-label'), '🔄 立即登录');
    text($('login-hint'), msg || '点击按钮立即触发一次完整的网络检查与登录流程。');
    if (type) toast(msg, type);
  }

  function bindLogin() {
    var btn = $('btn-login');
    if (!btn) return;
    btn.addEventListener('click', function () {
      if (loginWatch) return;
      btn.classList.add('loading');
      btn.disabled = true;
      text($('btn-login-label'), '正在登录…');
      text($('login-hint'), '已提交登录请求，正在等待服务端返回结果…');
      loginWatch = {
        deadline: Date.now() + 120000,
        sawProgress: false,
        baseCheckAt: null
      };
      API.status().then(function (s) { loginWatch && (loginWatch.baseCheckAt = s.last_check_at); }).catch(function () {});
      API.login().then(function (r) {
        if (r && r.triggered) {
          toast('已触发登录检查', 'success');
        } else if (r && r.reason === 'already_in_progress') {
          finishLogin('服务端已有登录流程在执行，请稍候。', 'warn');
        } else {
          finishLogin('登录请求被忽略：' + ((r && r.reason) || '未知原因'), 'warn');
        }
      }).catch(function () {
        finishLogin('登录请求失败，请检查服务是否在运行。', 'error');
      });
    });
  }

  function startStatusPolling() {
    if (statusTimer) return;
    pollStatus();
    statusTimer = setInterval(pollStatus, POLL_STATUS_MS);
  }

  /* ============================================================
     分区 5/6 · 配置读写
     ============================================================ */
  function clearErrors() {
    ['err-host', 'err-port', 'err-account', 'err-timeout', 'err-ui-port', 'err-pwd'].forEach(function (id) {
      text($(id), '');
    });
    ['cfg-host', 'cfg-port', 'cfg-account', 'cfg-network-timeout', 'cfg-ui-port', 'pwd-new', 'pwd-confirm'].forEach(function (id) {
      var el = $(id);
      if (el) el.classList.remove('is-invalid');
    });
  }

  function setFieldError(fieldId, errId, msg) {
    text($(errId), msg);
    var el = $(fieldId);
    if (el) el.classList.toggle('is-invalid', !!msg);
  }

  function isIntString(v) { return /^\d+$/.test(v); }
  function inRange(v, lo, hi) { var n = Number(v); return isFinite(n) && n >= lo && n <= hi; }

  function setPwdBadge(status) {
    var badge = $('pwd-badge');
    var card = $('card-password');
    if (!badge) return;
    if (status === 'set') {
      badge.textContent = '✅ 已设置';
      badge.className = 'badge badge-ok';
      if (card) card.className = 'card section';
    } else {
      badge.textContent = '❌ 未设置';
      badge.className = 'badge badge-err';
      if (card) card.className = 'card section card-alert';
    }
  }

  function loadConfig() {
    return API.config().then(function (c) {
      if (!c || typeof c !== 'object') throw new Error('bad payload');
      $('cfg-host').value = c.host || '';
      $('cfg-port').value = (typeof c.port === 'number') ? c.port : 80;
      $('cfg-account').value = c.account || '';
      $('cfg-suffix').value = c.suffix || '';
      $('cfg-auto-enabled').checked = !!c.auto_check_enabled;
      $('cfg-auto-interval').value = String(c.auto_check_interval_min || 30);
      $('cfg-network-timeout').value = c.network_wait_timeout_sec || 60;
      $('cfg-ui-port').value = c.ui_port || 8848;
      setPwdBadge(c.password_status);
      clearErrors();
      text($('config-state'), '已从服务端读取，修改后点击保存');
    }).catch(function () {
      toast('读取配置失败，请稍后重试', 'error');
    });
  }

  function collectConfig() {
    return {
      host: $('cfg-host').value.trim(),
      port: parseInt($('cfg-port').value, 10),
      account: $('cfg-account').value.trim(),
      suffix: $('cfg-suffix').value,
      auto_check_enabled: $('cfg-auto-enabled').checked,
      auto_check_interval_min: parseInt($('cfg-auto-interval').value, 10),
      network_wait_timeout_sec: parseInt($('cfg-network-timeout').value, 10),
      ui_port: parseInt($('cfg-ui-port').value, 10)
    };
  }

  /* 纯前端校验，不通过则不发请求 */
  function validateConfig() {
    var ok = true;
    var host = $('cfg-host').value.trim();
    var port = $('cfg-port').value.trim();
    var account = $('cfg-account').value.trim();
    var timeout = $('cfg-network-timeout').value.trim();
    var uiPort = $('cfg-ui-port').value.trim();

    ['err-host', 'err-port', 'err-account', 'err-timeout', 'err-ui-port'].forEach(function (id) { text($(id), ''); });
    ['cfg-host', 'cfg-port', 'cfg-account', 'cfg-network-timeout', 'cfg-ui-port'].forEach(function (id) {
      var el = $(id); if (el) el.classList.remove('is-invalid');
    });

    if (!host) { setFieldError('cfg-host', 'err-host', '认证服务器地址不能为空'); ok = false; }
    if (!isIntString(port) || !inRange(port, 1, 65535)) {
      setFieldError('cfg-port', 'err-port', '端口必须是 1-65535 之间的整数'); ok = false;
    }
    if (!account) { setFieldError('cfg-account', 'err-account', '账号不能为空'); ok = false; }
    else if (!isIntString(account)) { setFieldError('cfg-account', 'err-account', '账号只能是数字（学号 / 工号）'); ok = false; }
    if (!isIntString(timeout) || !inRange(timeout, 10, 300)) {
      setFieldError('cfg-network-timeout', 'err-timeout', '超时必须是 10-300 之间的整数'); ok = false;
    }
    if (!isIntString(uiPort) || !inRange(uiPort, 1024, 65535)) {
      setFieldError('cfg-ui-port', 'err-ui-port', 'UI 端口必须是 1024-65535 之间的整数'); ok = false;
    }
    return ok;
  }

  function bindConfig() {
    var saveBtn = $('btn-save-config');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        if (!validateConfig()) {
          toast('表单校验未通过，请修正标红字段', 'warn');
          return;
        }
        saveBtn.disabled = true;
        API.saveConfig(collectConfig()).then(function (r) {
          if (r && r.ok) {
            toast('配置已保存', 'success');
            text($('config-state'), '已保存 · ' + nowClock());
          } else {
            toast('保存失败：' + ((r && r.error) || '未知错误'), 'error');
          }
        }).catch(function () {
          toast('保存请求失败，请检查服务状态', 'error');
        }).then(function () { saveBtn.disabled = false; });
      });
    }

    var pwdBtn = $('btn-save-pwd');
    if (pwdBtn) {
      pwdBtn.addEventListener('click', function () {
        var p1 = $('pwd-new').value;
        var p2 = $('pwd-confirm').value;
        text($('err-pwd'), '');
        if (!p1) { setFieldError('pwd-new', 'err-pwd', '账户登录密码不能为空'); return; }
        if (p1 !== p2) { setFieldError('pwd-confirm', 'err-pwd', '两次输入的账户登录密码不一致'); return; }
        pwdBtn.disabled = true;
        API.savePassword(p1).then(function (r) {
          if (r && r.ok) {
            toast('账户登录密码已更新', 'success');
            $('pwd-new').value = '';
            $('pwd-confirm').value = '';
            clearErrors();
            loadConfig();
          } else {
            setFieldError('pwd-new', 'err-pwd', (r && r.error) || '保存失败');
          }
        }).catch(function () {
          setFieldError('pwd-new', 'err-pwd', '请求失败，请检查服务状态');
        }).then(function () { pwdBtn.disabled = false; });
      });
    }
  }

  /* ============================================================
     分区 6/6 · 日志 / 关于 / 启动
     ============================================================ */
  var logOffset = 0;
  var logLines = [];
  var logTimer = null;
  var logUpdatedAt = null;

  function renderLog() {
    var box = $('log-box');
    if (!box) return;
    var keep = box.scrollTop;
    var keyword = ($('log-filter').value || '').trim().toLowerCase();
    var view = logLines;
    if (keyword) {
      view = logLines.filter(function (l) { return l.toLowerCase().indexOf(keyword) !== -1; });
    }
    box.textContent = view.length ? view.join('\n') : '暂无日志';
    box.className = 'log-box' + (view.length ? '' : ' is-empty');
    if ($('log-autoscroll').checked) box.scrollTop = box.scrollHeight;
    else box.scrollTop = keep;
    text($('log-count'), (keyword ? view.length + ' / ' + logLines.length : view.length) + ' 行');
  }

  /* 增量拉取：只追加新内容，保留已有滚动位置 */
  function fetchLog(force) {
    if (document.hidden && !force) return Promise.resolve();
    if (force) { logOffset = 0; logLines = []; }
    return API.logTail(logOffset, 500).then(function (r) {
      if (!r || typeof r !== 'object') return;
      var incoming = r.lines || [];
      if (incoming.length) {
        logLines = logLines.concat(incoming);
        if (logLines.length > LOG_MAX_LINES) logLines = logLines.slice(logLines.length - LOG_MAX_LINES);
      }
      if (isNum(r.next_offset)) logOffset = r.next_offset;
      logUpdatedAt = nowClock();
      renderLog();
      text($('log-offset'), 'offset ' + logOffset);
      text($('log-size'), fmtBytes(r.total_size || 0));
      text($('log-updated'), '更新于 ' + logUpdatedAt);
    }).catch(function () {
      text($('log-updated'), '拉取失败');
    });
  }

  function reloadLog() { return fetchLog(true); }

  function bindLog() {
    var filter = $('log-filter');
    if (filter) filter.addEventListener('input', renderLog);
    var auto = $('log-autoscroll');
    if (auto) auto.addEventListener('change', function () { if (auto.checked) renderLog(); });
    var refresh = $('btn-log-refresh');
    if (refresh) refresh.addEventListener('click', function () {
      reloadLog().then(function () { toast('日志已刷新', 'success', 2000); });
    });
    var dl = $('btn-log-download');
    if (dl) dl.addEventListener('click', function () {
      var a = document.createElement('a');
      a.href = '/api/log_download';
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      toast('已开始下载完整日志', 'info', 2000);
    });
  }

  function startLogPolling() {
    if (logTimer) return;
    fetchLog(true);
    logTimer = setInterval(function () { fetchLog(false); }, POLL_LOG_MS);
  }

  var uptimeBase = null; /* { sec: 服务端返回的秒数, at: 本地读取时刻 } */

  function loadAbout() {
    return API.about().then(function (r) {
      if (!r || typeof r !== 'object') return;
      text($('ver-badge'), 'v' + (r.version || '-'));
      text($('about-version'), r.version || '-');
      text($('about-started'), fmtIso(r.service_started_at));
      uptimeBase = { sec: Number(r.service_uptime_sec) || 0, at: Date.now() };
      text($('about-uptime'), fmtDuration(uptimeBase.sec));
      if (r.config_file) $('about-config').innerHTML = '<code class="path">' + esc(r.config_file) + '</code>';
      if (r.data_dir) $('about-data').innerHTML = '<code class="path">' + esc(r.data_dir) + '</code>';
      if (r.log_file) $('about-log').innerHTML = '<code class="path">' + esc(r.log_file) + '</code>';
    }).catch(function () {
      text($('about-version'), '读取失败');
    }).then(function () {
      /* /api/log_file_path 提供日志文件的绝对路径，作为权威来源优先展示 */
      return API.logPath().then(function (p) {
        if (p && p.path) $('about-log').innerHTML = '<code class="path">' + esc(p.path) + '</code>';
      }).catch(function () { /* 关于信息已由 /api/about 兜底 */ });
    });
  }

  function bindAbout() {
    var local = $('about-local');
    if (local && window.location && window.location.origin && window.location.origin.indexOf('http') === 0) {
      local.href = window.location.origin;
      local.textContent = '🖥 本机管理页面（' + window.location.origin + '）';
    }
    var btnRestart = $('btn-restart');
    if (btnRestart) btnRestart.addEventListener('click', function () {
      if (!window.confirm('确认重启服务？NSSM 将自动重新拉起进程。')) return;
      API.restart().then(function (r) {
        toast((r && r.message) || '服务正在重启，3 秒后请刷新页面', 'success');
      }).catch(function () { toast('重启请求失败', 'error'); });
    });
    var btnUninstall = $('btn-uninstall');
    if (btnUninstall) btnUninstall.addEventListener('click', function () {
      text($('admin-hint'), '请以管理员身份运行程序目录下的 uninstall.bat 完成卸载。');
    });
  }

  /* —— 标签页（分段控件，支持键盘左右方向键） —— */
  function activateTab(name, focus) {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
    tabs.forEach(function (t) {
      var on = t.dataset.tab === name;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
      if (on && focus) t.focus();
    });
    ['status', 'config', 'log', 'about'].forEach(function (n) {
      var p = $('panel-' + n);
      if (p) p.classList.toggle('active', n === name);
    });
    if (name === 'config') loadConfig();
    else if (name === 'about') { loadAbout(); }
    else if (name === 'log') reloadLog();
  }

  function bindTabs() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
    tabs.forEach(function (tab, idx) {
      tab.addEventListener('click', function () { activateTab(tab.dataset.tab, false); });
      tab.addEventListener('keydown', function (ev) {
        var next = null;
        if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') next = (idx + 1) % tabs.length;
        else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') next = (idx - 1 + tabs.length) % tabs.length;
        else if (ev.key === 'Home') next = 0;
        else if (ev.key === 'End') next = tabs.length - 1;
        if (next === null) return;
        ev.preventDefault();
        activateTab(tabs[next].dataset.tab, true);
      });
    });
  }

  function boot() {
    bindTheme();
    bindTabs();
    bindLogin();
    bindConfig();
    bindLog();
    bindAbout();
    bindUpdate();

    startStatusPolling();
    startLogPolling();
    startUpdatePolling();
    loadAbout();

    setInterval(tickCountdown, 1000);
    setInterval(function () {
      if (document.hidden) return;
      if (uptimeBase) text($('about-uptime'), fmtDuration(uptimeBase.sec + (Date.now() - uptimeBase.at) / 1000));
    }, 1000);

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { pollStatus(); fetchLog(false); pollUpdate(); }
    });
  }

  /* ============================================================
     分区 7/7 · 自动升级（v1.3 新增）— 状态轮询 / 横幅渲染 / 按钮
     ============================================================ */
  var updateTimer = null;
  var updateStateCache = null;
  var updateSuccessHideAt = 0;
  var POLL_UPDATE_MS = 5000;

  function getUpdateJson(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) { return r.json(); });
  }
  function postUpdateJson(url, body) {
    var opt = { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opt.body = JSON.stringify(body);
    return fetch(url, opt).then(function (r) { return r.json(); });
  }

  function renderUpdateBanner(data) {
    var banner = $('update-banner');
    if (!banner) return;
    var state = data && data.state;
    if (!state) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.classList.remove('warn', 'error', 'success');
    var icon = $('update-banner-icon');
    var title = $('update-banner-title');
    var desc = $('update-banner-desc');
    var progress = $('update-progress');
    var pbar = $('update-progress-bar');
    var action = $('update-banner-action');
    action.hidden = true;
    progress.hidden = true;

    var ver = data.target_version || data.latest_version || '';
    var verStr = ver ? 'v' + ver : '';

    if (state === 'downloading') {
      banner.classList.add('warn');
      icon.textContent = '⬇';
      title.textContent = '正在下载新版本 ' + verStr;
      desc.textContent = data.progress_message || ('下载完成后将自动升级，服务将短暂中断约 60 秒');
      progress.hidden = false;
      pbar.style.width = (data.progress_pct || 0) + '%';
    } else if (state === 'checking') {
      icon.textContent = '🔍';
      title.textContent = '正在检查更新';
      desc.textContent = data.progress_message || '正在访问 GitHub API...';
    } else if (state === 'upgrading') {
      banner.classList.add('error');
      icon.textContent = '⚙';
      title.textContent = '正在升级到 ' + verStr;
      desc.textContent = data.progress_message || '请稍候（约 60 秒）...';
    } else if (state === 'success') {
      banner.classList.add('success');
      icon.textContent = '✅';
      title.textContent = '已升级到 ' + verStr;
      desc.textContent = data.progress_message || ('升级成功（' + fmtTimeOnly(data.last_check_at) + '）');
      action.hidden = false;
      action.textContent = '知道了';
      // 7 秒后自动隐藏（也由 5 分钟服务端清理兜底）
      updateSuccessHideAt = Date.now() + 7000;
    } else if (state === 'error') {
      banner.classList.add('error');
      icon.textContent = '❌';
      title.textContent = '升级失败';
      desc.textContent = data.progress_message || data.last_error || '未知错误';
      action.hidden = false;
      action.textContent = '查看详情';
    } else {
      banner.hidden = true;
    }
  }

  function pollUpdate() {
    if (document.hidden) return;
    getUpdateJson('/api/update/status').then(function (r) {
      if (!r || typeof r !== 'object') return;
      updateStateCache = r;
      renderUpdateBanner(r);
      // 7 秒后自动隐藏绿 banner（前端兜底）
      if (r.state === 'success' && updateSuccessHideAt && Date.now() > updateSuccessHideAt) {
        banner.hidden = true;
      }
    }).catch(function () { /* 静默：升级状态非关键 */ });
  }

  function startUpdatePolling() {
    if (updateTimer) return;
    pollUpdate();
    updateTimer = setInterval(pollUpdate, POLL_UPDATE_MS);
  }

  function bindUpdate() {
    // 关闭按钮（仅 success / error 时有效）
    var dismiss = $('update-banner-dismiss');
    if (dismiss) dismiss.addEventListener('click', function () {
      var b = $('update-banner'); if (b) b.hidden = true;
    });
    // 横幅 action 按钮（success=知道了 → 隐藏；error=查看详情 → 打开历史弹窗）
    var action = $('update-banner-action');
    if (action) action.addEventListener('click', function () {
      var s = updateStateCache && updateStateCache.state;
      if (s === 'success') {
        var b = $('update-banner'); if (b) b.hidden = true;
      } else if (s === 'error') {
        openUpdateHistoryModal();
      }
    });
    // 升级历史按钮（关于面板）
    var btnHistory = $('btn-update-history');
    if (btnHistory) btnHistory.addEventListener('click', openUpdateHistoryModal);
    // 弹窗关闭
    var modalClose = $('update-history-close');
    var modalCloseBtn = $('update-history-close-btn');
    var backdrop = $('update-history-backdrop');
    function _closeModal() {
      var m = $('update-history-modal'); if (m) m.hidden = true;
    }
    if (modalClose) modalClose.addEventListener('click', _closeModal);
    if (modalCloseBtn) modalCloseBtn.addEventListener('click', _closeModal);
    if (backdrop) backdrop.addEventListener('click', _closeModal);
    // 弹窗刷新
    var refreshBtn = $('update-history-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', function () {
      loadUpdateHistoryLines();
    });
    // ESC 关闭弹窗
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') {
        var m = $('update-history-modal');
        if (m && !m.hidden) m.hidden = true;
      }
    });
  }

  function loadUpdateHistoryLines() {
    var log = $('update-history-log');
    if (log) log.textContent = '加载中…';
    return getUpdateJson('/api/update/history').then(function (r) {
      if (!r || typeof r !== 'object') {
        if (log) log.textContent = '加载失败';
        return;
      }
      if (r.path) {
        var p = $('update-history-path');
        if (p) p.textContent = '日志路径：' + r.path;
      }
      var lines = r.lines || [];
      if (log) {
        log.textContent = lines.length ? lines.join('\n') : '（暂无升级日志）';
        log.scrollTop = log.scrollHeight;
      }
    }).catch(function () {
      if (log) log.textContent = '加载失败，请稍后重试';
    });
  }

  function openUpdateHistoryModal() {
    var m = $('update-history-modal'); if (!m) return;
    m.hidden = false;
    loadUpdateHistoryLines();
  }

  /* —— 把升级字段纳入 loadConfig / collectConfig —— */
  /* 复用原 loadConfig（已读 /api/config + 填好所有原表单），在它的 .then 末尾再补两项新字段。
     不再发额外的 /api/config 请求。 */
  var _origLoadConfig = loadConfig;
  loadConfig = function () {
    return _origLoadConfig().then(function () {
      return API.config().then(function (c) {
        if (!c || typeof c !== 'object') return;
        var au = $('cfg-auto-update-enabled');
        if (au) au.checked = !!c.auto_update_enabled;
        var iv = $('cfg-update-interval');
        if (iv) iv.value = String(c.update_check_interval_hours || 6);
        var diskEl = $('cfg-update-disk');
        if (diskEl) diskEl.value = String(c.update_min_free_disk_mb || 200);
      }).catch(function () { /* 配置页：忽略二次拉取失败 */ });
    });
  };

  var _origCollectConfig = collectConfig;
  collectConfig = function () {
    var cfg = _origCollectConfig();
    cfg.auto_update_enabled = !!(($('cfg-auto-update-enabled') || {}).checked);
    var iv = parseInt(($('cfg-update-interval') || {}).value, 10);
    cfg.update_check_interval_hours = (iv === 12 || iv === 24) ? iv : 6;
    var disk = parseInt(($('cfg-update-disk') || {}).value, 10);
    cfg.update_min_free_disk_mb = (typeof disk === 'number' && !isNaN(disk) && disk >= 50 && disk <= 10240) ? disk : 200;
    return cfg;
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>
</body>
</html>
"""

# ============================================================
# 启动 / 信号 / 主入口
# ============================================================
HTTP_SERVER = None  # 全局引用，便于信号处理关闭
STOP_EVENT = threading.Event()


def _on_signal(signum, frame):
    name = {signal.SIGTERM: "SIGTERM", signal.SIGBREAK: "SIGBREAK", signal.SIGINT: "SIGINT"}.get(signum, str(signum))
    logger.info("收到信号 %s，准备退出", name)
    STOP_EVENT.set()
    if HTTP_SERVER is not None:
        # HTTPServer.shutdown() 可从另一线程调用以唤醒 serve_forever()
        threading.Thread(target=HTTP_SERVER.shutdown, daemon=True).start()


def main():
    global HTTP_SERVER

    logger.info("=" * 60)
    logger.info("Dr.COM 自动登录服务启动（Web UI 配置版 v%s）", VERSION)

    # 1. 载入配置
    cfg = _load_config()
    _set_state(current_account="{}{}".format(cfg.get("account", ""), cfg.get("suffix", "")))

    # 2. 载入密码
    _load_password_from_disk()
    if _get_password() is None:
        logger.warning("password.txt 不存在或为空，请通过 Web UI (http://127.0.0.1:%s) 设置密码", cfg.get("ui_port", 8848))

    # 3. 注册信号
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)

    # 4. 启动钩子：检查是否刚升级过（必须在后台线程之前跑）
    try:
        _post_upgrade_startup()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动钩子异常: %s", exc)

    # 5. 启动后台线程
    startup_thread = threading.Thread(target=_startup_trigger, name="startup-trigger", daemon=True)
    startup_thread.start()

    periodic_thread = threading.Thread(target=run_periodic, name="periodic-check", daemon=True)
    periodic_thread.start()

    # —— 自动升级后台线程（v1.3 新增）——
    auto_update_thread = threading.Thread(target=_auto_update_loop, name="auto-update", daemon=True)
    auto_update_thread.start()

    # 6. 启动 Web 服务器（主线程阻塞）
    ui_port = cfg.get("ui_port", 8848)
    try:
        HTTP_SERVER = ThreadingHTTPServer(("127.0.0.1", ui_port), _Handler)
    except OSError as exc:
        logger.error("绑定 127.0.0.1:%s 失败: %s；请修改 config.json 的 ui_port 后重启", ui_port, exc)
        sys.stderr.write("FATAL: bind 127.0.0.1:{} failed: {}\n".format(ui_port, exc))
        return 1

    _set_state(service_started_at=_now_iso())
    logger.info("Web UI 已就绪: http://127.0.0.1:%s", ui_port)
    logger.info("启动线程与周期自检线程已启动")

    try:
        HTTP_SERVER.serve_forever()
    finally:
        STOP_EVENT.set()
        logger.info("HTTP server 已停止，等待后台线程退出...")
        # 等所有线程最多 3 秒
        for t in (startup_thread, periodic_thread, auto_update_thread):
            t.join(timeout=3)
        logger.info("服务退出")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — 终极兜底
        try:
            logger.exception("main 未捕获异常: %s", exc)
        finally:
            sys.exit(1)
