# -*- coding: utf-8 -*-
"""
联网_service.py — Dr.COM 校园网自动登录（Web UI 配置版 v2.0）

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

import json
import logging
import os
import random
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ============================================================
# 常量
# ============================================================
VERSION = "2.0"
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
}

ALLOWED_SUFFIXES = ("", "@yd", "@dx", "@lt")
ALLOWED_INTERVALS = (5, 15, 30, 60, 120)


# ============================================================
# 路径解析
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PASSWORD_FILE = os.path.join(BASE_DIR, "password.txt")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "campus_login.log")

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
    """校验 + 写入。失败抛 ValueError。"""
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
   1. 设计令牌 —— 亮色（默认）
   ============================================================ */
:root {
  --bg: #f4f6fa;
  --card: #ffffff;
  --card-2: #f7f9fc;
  --topbar-bg: rgba(255, 255, 255, 0.86);
  --text: #161a20;
  --muted: #68727f;
  --primary: #2f6feb;
  --primary-strong: #1d5ad4;
  --primary-soft: rgba(47, 111, 235, 0.12);
  --ok: #16a34a;
  --ok-soft: rgba(22, 163, 74, 0.14);
  --warn: #d97706;
  --warn-soft: rgba(217, 119, 6, 0.16);
  --err: #dc2626;
  --err-soft: rgba(220, 38, 38, 0.14);
  --border: #e3e8ef;
  --track: #d5dbe4;
  --log-bg: #0f1216;
  --log-text: #d7dde5;
  --log-dim: #7b8794;
  --radius: 12px;
  --radius-sm: 9px;
  --shadow: 0 1px 2px rgba(16, 24, 40, 0.05), 0 10px 28px -18px rgba(16, 24, 40, 0.45);
  --shadow-lift: 0 2px 6px rgba(16, 24, 40, 0.08), 0 18px 40px -20px rgba(16, 24, 40, 0.5);
}

/* ============================================================
   2. 设计令牌 —— 暗色（由 html[data-theme=dark] 激活；
         首屏脚本用 prefers-color-scheme 决定初始值）
   ============================================================ */
[data-theme="dark"] {
  --bg: #0d1014;
  --card: #161a21;
  --card-2: #1b2029;
  --topbar-bg: rgba(13, 16, 20, 0.86);
  --text: #e7ecf3;
  --muted: #98a2b3;
  --primary: #5b9bff;
  --primary-strong: #7cb0ff;
  --primary-soft: rgba(91, 155, 255, 0.16);
  --ok: #3ecf72;
  --ok-soft: rgba(62, 207, 114, 0.16);
  --warn: #f0b34a;
  --warn-soft: rgba(240, 179, 74, 0.18);
  --err: #ff6b6b;
  --err-soft: rgba(255, 107, 107, 0.16);
  --border: #262c36;
  --track: #2c333d;
  --log-bg: #080a0d;
  --log-text: #d7dde5;
  --log-dim: #6b7683;
  --shadow: 0 1px 2px rgba(0, 0, 0, 0.45), 0 12px 30px -18px rgba(0, 0, 0, 0.9);
  --shadow-lift: 0 2px 8px rgba(0, 0, 0, 0.5), 0 20px 44px -22px rgba(0, 0, 0, 0.95);
}

/* ============================================================
   3. 基础排版
   ============================================================ */
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", sans-serif;
  font-size: 14px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  transition: background-color 0.2s ease, color 0.2s ease;
}
h1, h2, h3 { margin: 0; font-weight: 650; }
a { color: var(--primary); }
.mono { font-family: "Cascadia Mono", "Consolas", "SFMono-Regular", "Courier New", monospace; font-variant-numeric: tabular-nums; }
.muted { color: var(--muted); }
.skip {
  position: absolute; left: -9999px; top: 0; z-index: 99;
  background: var(--card); border: 1px solid var(--border); border-radius: 0 0 var(--radius-sm) 0;
  padding: 8px 14px; text-decoration: none;
}
.skip:focus { left: 0; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 22px 20px 60px; }

/* ============================================================
   4. 顶栏 + 分段控件
   ============================================================ */
.topbar {
  position: sticky; top: 0; z-index: 40;
  background: var(--topbar-bg);
  backdrop-filter: saturate(180%) blur(12px);
  -webkit-backdrop-filter: saturate(180%) blur(12px);
  border-bottom: 1px solid var(--border);
}
.topbar-inner {
  max-width: 1120px; margin: 0 auto; padding: 12px 20px 10px;
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
}
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand-mark { font-size: 20px; line-height: 1; }
.brand-name { font-size: 16px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ver-badge {
  font-size: 11px; font-weight: 600; letter-spacing: 0.02em;
  color: var(--primary); background: var(--primary-soft);
  padding: 2px 8px; border-radius: 999px; white-space: nowrap;
}
.topbar-actions { display: flex; align-items: center; gap: 10px; }
.liveness {
  display: inline-flex; align-items: center; gap: 7px;
  font-size: 13px; color: var(--muted);
  background: var(--card); border: 1px solid var(--border);
  border-radius: 999px; padding: 5px 12px; white-space: nowrap;
}
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; background: var(--muted); }
.dot-ok { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-soft); }
.dot-err { background: var(--err); box-shadow: 0 0 0 3px var(--err-soft); }
.dot-warn { background: var(--warn); box-shadow: 0 0 0 3px var(--warn-soft); }
.dot-unknown { background: var(--muted); box-shadow: 0 0 0 3px var(--warn-soft); }
.dot-pulse { animation: pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
.icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 36px; height: 36px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--card); color: var(--text);
  cursor: pointer; transition: border-color 0.15s, color 0.15s, transform 0.15s;
}
.icon-btn:hover { border-color: var(--primary); color: var(--primary); }
.icon-btn:active { transform: scale(0.95); }
.theme-icon { display: block; }
[data-theme="dark"] .theme-icon-sun { display: none; }
[data-theme="light"] .theme-icon-moon { display: none; }
.topbar-nav { max-width: 1120px; margin: 0 auto; padding: 0 20px 12px; overflow-x: auto; }
.tablist {
  display: inline-flex; gap: 4px; padding: 4px;
  background: var(--card-2); border: 1px solid var(--border); border-radius: 13px;
}
.tab {
  display: inline-flex; align-items: center; gap: 6px;
  border: 0; background: transparent; color: var(--muted);
  font: inherit; font-size: 13.5px; font-weight: 500;
  padding: 7px 16px; border-radius: 9px; cursor: pointer; white-space: nowrap;
  transition: background-color 0.15s, color 0.15s, box-shadow 0.15s;
}
.tab:hover { color: var(--text); }
.tab[aria-selected="true"] {
  background: var(--card); color: var(--primary); font-weight: 650; box-shadow: var(--shadow);
}
.tab:focus-visible, .icon-btn:focus-visible, .btn:focus-visible,
.field input:focus-visible, .field select:focus-visible, .log-toolbar input:focus-visible {
  outline: 2px solid var(--primary); outline-offset: 2px;
}

/* ============================================================
   5. 面板 / 卡片 / KPI
   ============================================================ */
.panel { display: none; }
.panel.active { display: block; opacity: 1; animation: fade 0.18s ease; }
@keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
.grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(228px, 1fr)); }
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px;
}
.kpi { display: flex; flex-direction: column; gap: 6px; min-height: 106px; }
.kpi-label { font-size: 12px; font-weight: 650; color: var(--muted); letter-spacing: 0.02em; }
.kpi-value {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 19px; font-weight: 650; line-height: 1.35; word-break: break-all;
}
.kpi-value.small { font-size: 15px; font-weight: 600; }
.kpi-sub { font-size: 12px; color: var(--muted); }
.kpi-alert { border-color: var(--warn); box-shadow: 0 0 0 3px var(--warn-soft), var(--shadow); }
.kpi-alert-err { border-color: var(--err); box-shadow: 0 0 0 3px var(--err-soft), var(--shadow); }
.tone-ok { color: var(--ok); }
.tone-err { color: var(--err); }
.tone-warn { color: var(--warn); }
.tone-muted { color: var(--muted); }
.action-card {
  display: flex; flex-direction: column; align-items: center; gap: 12px;
  text-align: center; padding: 26px 18px; margin-top: 14px;
}

/* ============================================================
   6. 按钮 / 表单 / 开关 / 徽章
   ============================================================ */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 9px 18px; border-radius: var(--radius-sm);
  border: 1px solid var(--primary); background: var(--primary); color: #fff;
  font: inherit; font-size: 14px; font-weight: 600; cursor: pointer; text-decoration: none;
  transition: background-color 0.15s, border-color 0.15s, color 0.15s, transform 0.12s, opacity 0.15s;
}
.btn:hover:not(:disabled) { background: var(--primary-strong); border-color: var(--primary-strong); }
.btn:active:not(:disabled) { transform: translateY(1px); }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-secondary { background: var(--card); color: var(--primary); border-color: var(--border); }
.btn-secondary:hover:not(:disabled) { background: var(--primary-soft); border-color: var(--primary); color: var(--primary); }
.btn-danger { background: transparent; color: var(--err); border-color: var(--err); }
.btn-danger:hover:not(:disabled) { background: var(--err); color: #fff; border-color: var(--err); }
.btn-lg { padding: 13px 34px; font-size: 15px; border-radius: 11px; min-width: 196px; }
.btn-spinner {
  display: none; width: 15px; height: 15px; border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, 0.45); border-top-color: #fff;
  animation: spin 0.7s linear infinite;
}
.btn.loading .btn-spinner { display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
.section { margin-bottom: 14px; }
.section-head { margin-bottom: 14px; }
.section-title { font-size: 15px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.section-desc { font-size: 12.5px; color: var(--muted); margin-top: 3px; }
.field { position: relative; margin-bottom: 16px; }
.field:last-child { margin-bottom: 0; }
.field > label:not(.switch) { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.field input[type=text], .field input[type=number], .field input[type=password], .field select, .log-toolbar input {
  width: 100%; padding: 9px 12px; font: inherit; font-size: 13.5px;
  color: var(--text); background: var(--card-2);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  transition: border-color 0.15s, box-shadow 0.15s, background-color 0.15s;
}
.field input:focus, .field select:focus, .log-toolbar input:focus {
  outline: none; border-color: var(--primary); background: var(--card);
  box-shadow: 0 0 0 3px var(--primary-soft);
}
.field input.is-invalid, .field select.is-invalid { border-color: var(--err); box-shadow: 0 0 0 3px var(--err-soft); }
.hint { font-size: 12px; color: var(--muted); margin-top: 5px; }
.err { font-size: 12px; color: var(--err); margin-top: 5px; }
.err:empty { display: none; }
.switch { position: relative; display: inline-flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
.switch input { position: absolute; width: 1px; height: 1px; opacity: 0; margin: 0; }
.switch .track {
  position: relative; width: 46px; height: 26px; border-radius: 999px;
  background: var(--track); flex: none; transition: background-color 0.18s;
}
.switch .track::after {
  content: ''; position: absolute; top: 3px; left: 3px;
  width: 20px; height: 20px; border-radius: 50%; background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3); transition: transform 0.18s;
}
.switch input:checked + .track { background: var(--ok); }
.switch input:checked + .track::after { transform: translateX(20px); }
.switch input:focus-visible + .track { outline: 2px solid var(--primary); outline-offset: 2px; }
.switch-label { font-size: 13.5px; font-weight: 600; }
.badge {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 12px; font-weight: 650; padding: 3px 10px;
  border-radius: 999px; border: 1px solid transparent; white-space: nowrap;
}
.badge-ok { background: var(--ok-soft); color: var(--ok); }
.badge-err { background: var(--err-soft); color: var(--err); }
.badge-muted { background: var(--card-2); color: var(--muted); border-color: var(--border); }
.card-alert { border-color: var(--err); box-shadow: 0 0 0 3px var(--err-soft), var(--shadow); }
.save-bar {
  position: sticky; bottom: 12px; z-index: 20;
  display: flex; justify-content: flex-end; gap: 10px; align-items: center; flex-wrap: wrap;
  padding: 12px 14px; background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-lift);
}
.save-bar .muted { margin-right: auto; font-size: 12.5px; }

/* ============================================================
   7. 日志终端
   ============================================================ */
.log-toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.log-toolbar .grow { flex: 1 1 220px; min-width: 180px; }
.log-box {
  background: var(--log-bg); color: var(--log-text);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 14px 16px; height: min(58vh, 520px); overflow: auto;
  font-family: "Cascadia Mono", "Consolas", "SFMono-Regular", "Courier New", monospace;
  font-size: 12.5px; line-height: 1.65; white-space: pre-wrap; word-break: break-all;
}
.log-box::-webkit-scrollbar { width: 10px; height: 10px; }
.log-box::-webkit-scrollbar-thumb { background: #3a414b; border-radius: 6px; }
.log-box.is-empty { color: var(--log-dim); }
.log-meta {
  display: flex; gap: 16px; flex-wrap: wrap;
  font-size: 12px; color: var(--muted); margin-top: 10px;
}

/* ============================================================
   8. 关于
   ============================================================ */
.info { display: grid; grid-template-columns: 160px 1fr; gap: 10px 18px; font-size: 13.5px; }
.info dt { color: var(--muted); }
.info dd { margin: 0; word-break: break-all; }
code.path {
  background: var(--card-2); border: 1px solid var(--border); border-radius: 6px;
  padding: 2px 7px; font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12.5px;
}
.link-row { display: flex; gap: 10px; flex-wrap: wrap; }

/* ============================================================
   9. Toast
   ============================================================ */
.toasts {
  position: fixed; top: 16px; right: 16px; z-index: 100;
  display: flex; flex-direction: column; gap: 10px;
  max-width: min(92vw, 380px); pointer-events: none;
}
.toast {
  display: flex; gap: 9px; align-items: flex-start;
  background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--primary);
  border-radius: 10px; box-shadow: var(--shadow-lift); padding: 11px 14px; font-size: 13.5px;
  animation: toastIn 0.22s cubic-bezier(0.2, 0.9, 0.3, 1.2);
}
.toast.success { border-left-color: var(--ok); }
.toast.error { border-left-color: var(--err); }
.toast.warn { border-left-color: var(--warn); }
.toast.out { animation: toastOut 0.24s ease forwards; }
.toast-icon { line-height: 1.5; }
.toast-msg { word-break: break-word; }
@keyframes toastIn { from { opacity: 0; transform: translateX(20px) scale(0.97); } to { opacity: 1; transform: none; } }
@keyframes toastOut { to { opacity: 0; transform: translateX(20px); } }

/* ============================================================
   10. 响应式
   ============================================================ */
@media (max-width: 720px) {
  .wrap { padding: 16px 14px 48px; }
  .topbar-inner { padding: 10px 14px 8px; }
  .topbar-nav { padding: 0 14px 10px; }
  .grid { grid-template-columns: 1fr; }
  .brand-name { font-size: 14.5px; }
  .kpi { min-height: 0; }
  .info { grid-template-columns: 1fr; gap: 2px; }
  .info dt { margin-top: 10px; }
  .btn-lg { width: 100%; }
  .save-bar { justify-content: stretch; }
  .save-bar .btn { flex: 1 1 auto; }
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
      <span class="brand-mark" aria-hidden="true">🎓</span>
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

<main class="wrap" id="main">

  <!-- ============ 状态 ============ -->
  <section class="panel active" id="panel-status" role="tabpanel" aria-labelledby="tab-status" tabindex="-1">
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
    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">认证服务器</h2>
        <p class="section-desc" style="margin:0;">校园网认证网关参数，账号与运营商后缀会拼接为登录用户名。</p>
      </div>
      <div class="field">
        <label for="cfg-host">认证服务器地址</label>
        <input type="text" id="cfg-host" placeholder="例如 172.16.80.3" autocomplete="off" spellcheck="false">
        <div class="hint">认证网关的 IP 或域名</div>
        <div class="err" id="err-host" role="alert"></div>
      </div>
      <div class="field">
        <label for="cfg-port">认证端口</label>
        <input type="number" id="cfg-port" min="1" max="65535" step="1" inputmode="numeric">
        <div class="hint">取值 1-65535，通常为 80</div>
        <div class="err" id="err-port" role="alert"></div>
      </div>
      <div class="field">
        <label for="cfg-account">账号</label>
        <input type="text" id="cfg-account" placeholder="学号 / 工号（纯数字）" autocomplete="off" spellcheck="false" inputmode="numeric">
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
        <input type="number" id="cfg-network-timeout" min="10" max="300" step="1" inputmode="numeric">
        <div class="hint">每次检查等待校园网可达的最长时间，10-300 秒</div>
        <div class="err" id="err-timeout" role="alert"></div>
      </div>
    </div>

    <div class="card section">
      <div class="section-head">
        <h2 class="section-title">Web UI</h2>
        <p class="section-desc" style="margin:0;">管理页面本身的服务端口，修改后需重启服务生效。</p>
      </div>
      <div class="field">
        <label for="cfg-ui-port">监听端口</label>
        <input type="number" id="cfg-ui-port" min="1024" max="65535" step="1" inputmode="numeric">
        <div class="hint">取值 1024-65535，仅监听 127.0.0.1，默认 8848</div>
        <div class="err" id="err-ui-port" role="alert"></div>
      </div>
    </div>

    <div class="card section" id="card-password">
      <div class="section-head">
        <h2 class="section-title">密码 <span class="badge badge-muted" id="pwd-badge">状态未知</span></h2>
        <p class="section-desc" style="margin:0;">密码仅保存于本机 password.txt，保存后立即生效，无需重启。</p>
      </div>
      <div class="field">
        <label for="pwd-new">新密码</label>
        <input type="password" id="pwd-new" autocomplete="new-password">
        <div class="hint">至少 1 个字符</div>
      </div>
      <div class="field">
        <label for="pwd-confirm">确认新密码</label>
        <input type="password" id="pwd-confirm" autocomplete="new-password">
        <div class="err" id="err-pwd" role="alert"></div>
      </div>
      <button class="btn btn-secondary" id="btn-save-pwd" type="button">🔑 修改密码</button>
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
        <a class="btn btn-secondary" href="https://github.com/TSS-Small-sunshine/DrcomAutoLogin-Windows" target="_blank" rel="noopener noreferrer">📦 GitHub 仓库</a>
      </div>
      <p class="hint" style="margin-top:12px;">
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
      </div>
      <p class="hint" id="admin-hint" style="margin-top:12px;"></p>
    </div>
  </section>
</main>

<div class="toasts" id="toasts" role="region" aria-live="polite" aria-label="通知"></div>

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
        if (!p1) { setFieldError('pwd-new', 'err-pwd', '新密码不能为空'); return; }
        if (p1 !== p2) { setFieldError('pwd-confirm', 'err-pwd', '两次输入的密码不一致'); return; }
        pwdBtn.disabled = true;
        API.savePassword(p1).then(function (r) {
          if (r && r.ok) {
            toast('密码已更新', 'success');
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

    startStatusPolling();
    startLogPolling();
    loadAbout();

    setInterval(tickCountdown, 1000);
    setInterval(function () {
      if (document.hidden) return;
      if (uptimeBase) text($('about-uptime'), fmtDuration(uptimeBase.sec + (Date.now() - uptimeBase.at) / 1000));
    }, 1000);

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { pollStatus(); fetchLog(false); }
    });
  }

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

    # 4. 启动后台线程
    startup_thread = threading.Thread(target=_startup_trigger, name="startup-trigger", daemon=True)
    startup_thread.start()

    periodic_thread = threading.Thread(target=run_periodic, name="periodic-check", daemon=True)
    periodic_thread.start()

    # 5. 启动 Web 服务器（主线程阻塞）
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
        for t in (startup_thread, periodic_thread):
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