# -*- coding: utf-8 -*-
"""
联网_service.py — 星尘闪连 (Stardust Flash Link) — Dr.COM 校园网自动登录（Web UI 配置版 v2.0.1）

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
from version import VERSION  # noqa: E402  保持原行号兼容：VERSION 原本在 line 51
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
GITHUB_REPO = "TSS-Small-sunshine/StardustFlashLink"
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

# GitHub 镜像（国内加速；首个 None 表示主源，按顺序 fallback）
# 镜像格式：prefix + 原始 URL；原始 URL 必须是 https://... 开头以避免双斜杠。
GITHUB_API_MIRRORS = (
    None,                    # 主源 api.github.com
    "https://gh-proxy.com",
    "https://ghfast.top",
    "https://mirror.ghproxy.com",
)
GITHUB_DOWNLOAD_MIRRORS = (
    None,                    # 主源 objects.githubusercontent.com / github.com
    "https://gh-proxy.com",
    "https://ghfast.top",
    "https://mirror.ghproxy.com",
)
GITHUB_API_REQUEST_TIMEOUT_SEC = 15  # 镜像 fallback 时单次超时
GITHUB_DOWNLOAD_REQUEST_TIMEOUT_SEC = 60  # 镜像 fallback 时下载单段超时

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
# 网络操作 / run_once 已迁移到 protocol.py（v2.0.2 解耦）
# ============================================================
from protocol import (
    wait_network,
    discover_network,
    is_online,
    login,
    run_once,
)
import protocol as _protocol_mod

# _attach 在 main() 里完成（STOP_EVENT 必须在 _attach 之前已存在）。

# ============================================================
# EULA / CHANGELOG IO 已迁移到 eula.py（v2.0.2 解耦）
# ============================================================
from eula import api_get_changelog as _eula_api_get_changelog
import eula as _eula_mod

# _eula_mod._attach() 在 main() 里调用（需要 BASE_DIR）。
# ============================================================
# Web API + _Handler + _HTML_PAGE 已迁移到 web_api.py（v2.0.2 解耦）
# ============================================================
import web_api as _web_api_mod

# _web_api_mod._attach() 在 main() 里调用（需要 BASE_DIR / STATE / cfg / logger 等）。


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
    """依次尝试 GitHub 主源 + 镜像拉 releases/latest API。

    返回 (version, asset_url, digest, size, published_at) 元组；全部失败返回 None。
    digest 形如 "sha256:abcd..."，已剥掉前缀。
    主源超时/失败时 fallback 到下一个镜像。
    """
    primary_url = GITHUB_RELEASES_API
    for mirror in GITHUB_API_MIRRORS:
        target = primary_url if mirror is None else mirror.rstrip("/") + "/" + primary_url
        try:
            req = urllib.request.Request(
                target,
                headers={
                    "User-Agent": GITHUB_UA,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
            )
            timeout = HTTP_TIMEOUT_SEC if mirror is None else GITHUB_API_REQUEST_TIMEOUT_SEC
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning(
                "GitHub 检查镜像 %s 失败: %s",
                "primary" if mirror is None else mirror,
                exc,
            )
            continue
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("GitHub API JSON 解析失败 (%s): %s", target, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("GitHub API 返回非 dict: %s", target)
            continue
        tag = data.get("tag_name")
        if not isinstance(tag, str) or not tag:
            logger.warning("GitHub API 返回无 tag_name: %s", target)
            continue
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
            logger.warning("GitHub API 返回无 .exe asset: %s", target)
            continue
        published = data.get("published_at")
        return version, asset_url, digest, size, published
    return None


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
    """按顺序尝试主源 + 镜像下载 installer；任一成功即返回字节数。
    所有镜像都失败则抛最后一次异常。

    progress_callback(downloaded_bytes, total_bytes_or_None) 每 ~200ms 触发。
    """
    primary_url = url
    last_exc = None
    for mirror in GITHUB_DOWNLOAD_MIRRORS:
        target = primary_url if mirror is None else mirror.rstrip("/") + "/" + primary_url
        try:
            return _do_download_installer(
                target, dest_path, expected_size, progress_callback,
                timeout=DOWNLOAD_TIMEOUT_SEC if mirror is None else GITHUB_DOWNLOAD_REQUEST_TIMEOUT_SEC,
            )
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            logger.warning(
                "下载镜像 %s 失败: %s",
                "primary" if mirror is None else mirror,
                exc,
            )
            last_exc = exc
            continue
    raise last_exc if last_exc is not None else OSError("所有下载镜像都失败")


def _do_download_installer(url, dest_path, expected_size, progress_callback, timeout):
    """单镜像流式下载（_download_installer 的实际下载实现）。

    成功返回写入字节数；失败抛 (URLError / OSError / TimeoutError)。
    """
    last_report = [0.0]
    last_bytes = [0]

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": GITHUB_UA, "Accept": "application/octet-stream"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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

    # 4.5 把共享状态注入 protocol 模块（必须在 run_once 被任何线程调用之前）
    _protocol_mod._attach(
        logger=logger,
        state=STATE,
        state_lock=STATE_LOCK,
        backoff=BACKOFF,
        backoff_lock=BACKOFF_LOCK,
        run_lock=RUN_LOCK,
        pwd_value=_PWD_VALUE,
        pwd_lock=PWD_LOCK,
        load_password_from_disk=_load_password_from_disk,
        get_password=_get_password,
        load_config=_load_config,
        set_state=_set_state,
        set_backoff=_set_backoff,
        reset_backoff=_reset_backoff,
        backoff_until=_backoff_until,
        now_iso=_now_iso,
        stop_event=STOP_EVENT,
    )
    # 4.6 把 BASE_DIR 注入 eula 模块（CHANGELOG / EULA IO 需要）
    _eula_mod._attach(base_dir=BASE_DIR)
    # 4.7 把共享状态注入 web_api 模块（HTTP 路由 / _Handler / _HTML_PAGE 需要）
    _web_api_mod._attach(
        logger=logger,
        run_lock=RUN_LOCK,
        base_dir=BASE_DIR,
        log_file=LOG_FILE,
        log_dir=LOG_DIR,
        state=STATE,
        state_lock=STATE_LOCK,
        pwd_value=_PWD_VALUE,
        pwd_lock=PWD_LOCK,
        config_file=CONFIG_FILE,
        password_file=PASSWORD_FILE,
        upgrade_log_file=UPGRADE_LOG_FILE,
        default_config=DEFAULT_CONFIG,
        allowed_suffixes=ALLOWED_SUFFIXES,
        allowed_intervals=ALLOWED_INTERVALS,
        allowed_update_intervals=ALLOWED_UPDATE_INTERVALS,
        load_config=_load_config,
        save_config=_save_config,
        save_password_to_disk=_save_password_to_disk,
        validate_config=_validate_config,
        snapshot_state=_snapshot_state,
        get_password=_get_password,
        now_iso=_now_iso,
        stop_event=STOP_EVENT,
        run_once_fn=run_once,
    )

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
        HTTP_SERVER = ThreadingHTTPServer(("127.0.0.1", ui_port), _web_api_mod._Handler)
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
