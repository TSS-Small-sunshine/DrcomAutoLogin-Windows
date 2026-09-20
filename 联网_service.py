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
<title>Dr.COM 校园网自动登录</title>
<style>
* { box-sizing: border-box; }
:root {
  --bg: #f0f2f5;
  --card: #ffffff;
  --text: #1f1f1f;
  --text-muted: #6b7280;
  --primary: #1890ff;
  --primary-hover: #40a9ff;
  --success: #52c41a;
  --error: #ff4d4f;
  --warning: #faad14;
  --border: #e8e8e8;
  --log-bg: #1e1e1e;
  --log-text: #d4d4d4;
}
[data-theme="dark"] {
  --bg: #141414;
  --card: #1f1f1f;
  --text: #e6e6e6;
  --text-muted: #9ca3af;
  --primary: #4fc3f7;
  --primary-hover: #29b6f6;
  --border: #303030;
  --log-bg: #0a0a0a;
  --log-text: #cccccc;
}
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif; background: var(--bg); color: var(--text); transition: background 0.2s, color 0.2s; min-height: 100vh; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 12px; }
h1 { font-size: 22px; margin: 0; }
.tabs { display: flex; gap: 0; border-bottom: 2px solid var(--border); margin-bottom: 20px; overflow-x: auto; }
.tab { padding: 10px 18px; cursor: pointer; border: none; background: none; color: var(--text-muted); font-size: 14px; font-family: inherit; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: color 0.2s, border-color 0.2s; white-space: nowrap; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--primary); border-bottom-color: var(--primary); font-weight: 600; }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 20px; }
.card { background: var(--card); border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); border: 1px solid var(--border); }
.card-label { font-size: 11px; color: var(--text-muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }
.card-value { font-size: 16px; font-weight: 500; word-break: break-all; }
.status-ok { color: var(--success); }
.status-bad { color: var(--error); }
.status-warn { color: var(--warning); }
.muted { color: var(--text-muted); font-size: 13px; }
.btn { padding: 8px 16px; border: 1px solid var(--primary); border-radius: 4px; background: var(--primary); color: #fff; cursor: pointer; font-size: 14px; font-family: inherit; transition: all 0.15s; }
.btn:hover { background: var(--primary-hover); border-color: var(--primary-hover); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary { background: transparent; color: var(--primary); }
.btn-secondary:hover { background: var(--primary); color: #fff; }
.btn-danger { background: var(--error); border-color: var(--error); }
.btn-danger:hover { background: #ff7875; border-color: #ff7875; }
.btn-big { padding: 14px 36px; font-size: 16px; font-weight: 500; }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
.section { background: var(--card); border-radius: 8px; padding: 20px; border: 1px solid var(--border); margin-bottom: 16px; }
.section h3 { margin: 0 0 16px 0; font-size: 16px; }
.form-group { margin-bottom: 16px; }
.form-group > label { display: block; margin-bottom: 6px; font-weight: 500; font-size: 14px; }
.form-group .hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.form-group input[type=text], .form-group input[type=number], .form-group input[type=password], .form-group select { width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 4px; background: var(--card); color: var(--text); font-size: 14px; font-family: inherit; }
.form-group input:focus, .form-group select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 2px rgba(24,144,255,0.15); }
.form-group .error { color: var(--error); font-size: 12px; margin-top: 4px; min-height: 16px; }
.checkbox-group { display: flex; align-items: center; gap: 8px; }
.checkbox-group input[type=checkbox] { width: 18px; height: 18px; cursor: pointer; }
.toast { position: fixed; bottom: 24px; right: 24px; padding: 12px 20px; background: var(--card); border-radius: 4px; box-shadow: 0 4px 16px rgba(0,0,0,0.18); border-left: 4px solid var(--primary); z-index: 1000; animation: slideIn 0.25s; max-width: 420px; }
.toast.error { border-left-color: var(--error); }
.toast.success { border-left-color: var(--success); }
@keyframes slideIn { from { transform: translateX(120%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
.log-box { background: var(--log-bg); color: var(--log-text); padding: 16px; border-radius: 4px; height: 520px; overflow-y: auto; font-family: "Cascadia Code", "Consolas", "Courier New", monospace; font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-all; }
.log-toolbar { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; flex-wrap: wrap; }
.log-toolbar input[type=text] { flex: 1; min-width: 200px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 4px; background: var(--card); color: var(--text); font-size: 13px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 500; vertical-align: middle; }
.badge.set { background: rgba(82,196,26,0.12); color: var(--success); }
.badge.missing { background: rgba(255,77,79,0.12); color: var(--error); }
.about-grid { display: grid; grid-template-columns: 140px 1fr; gap: 10px 20px; font-size: 14px; margin: 0; }
.about-grid dt { color: var(--text-muted); font-weight: 500; }
.about-grid dd { margin: 0; word-break: break-all; font-family: "Cascadia Code", "Consolas", monospace; font-size: 13px; }
code.path { background: var(--bg); padding: 2px 6px; border-radius: 3px; font-family: "Cascadia Code", "Consolas", monospace; font-size: 12px; }
@media (max-width: 640px) {
  .container { padding: 12px; }
  h1 { font-size: 18px; }
  .about-grid { grid-template-columns: 1fr; gap: 4px 0; }
  .about-grid dt { margin-top: 8px; }
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🎓 Dr.COM 校园网自动登录</h1>
    <button class="btn btn-secondary" id="btn-theme" onclick="toggleTheme()">🌙 夜间模式</button>
  </div>
  <div class="tabs" id="tabs">
    <button class="tab active" data-tab="status">📊 状态</button>
    <button class="tab" data-tab="config">⚙️ 配置</button>
    <button class="tab" data-tab="log">📝 日志</button>
    <button class="tab" data-tab="about">ℹ️ 关于</button>
  </div>

  <div id="tab-status" class="tab-panel active">
    <div class="cards" id="status-cards"><div class="card"><div class="muted">加载中...</div></div></div>
    <div class="section" style="text-align: center;">
      <button class="btn btn-big" id="btn-login" onclick="triggerLogin()">🔄 立即登录</button>
      <p class="muted" id="login-status-text" style="margin-top: 12px;">点击按钮立即触发一次完整检查</p>
    </div>
  </div>

  <div id="tab-config" class="tab-panel">
    <div class="section">
      <h3>认证参数</h3>
      <div class="form-group">
        <label>认证服务器地址 (HOST)</label>
        <input type="text" id="cfg-host" placeholder="例如 172.16.80.3">
        <div class="hint">校园网认证网关 IP 或域名</div>
      </div>
      <div class="form-group">
        <label>端口 (PORT)</label>
        <input type="number" id="cfg-port" min="1" max="65535">
      </div>
      <div class="form-group">
        <label>账号</label>
        <input type="text" id="cfg-account" placeholder="学号 / 工号">
      </div>
      <div class="form-group">
        <label>运营商后缀</label>
        <select id="cfg-suffix">
          <option value="">校园用户 (无后缀)</option>
          <option value="@yd">移动 (@yd)</option>
          <option value="@dx">电信 (@dx)</option>
          <option value="@lt">联通 (@lt)</option>
        </select>
      </div>
    </div>
    <div class="section">
      <h3>自检设置</h3>
      <div class="form-group">
        <div class="checkbox-group">
          <input type="checkbox" id="cfg-auto-enabled">
          <label for="cfg-auto-enabled" style="margin: 0;">启用周期自检</label>
        </div>
      </div>
      <div class="form-group">
        <label>检查间隔 (分钟)</label>
        <select id="cfg-auto-interval">
          <option value="5">5 分钟</option>
          <option value="15">15 分钟</option>
          <option value="30">30 分钟</option>
          <option value="60">60 分钟</option>
          <option value="120">120 分钟</option>
        </select>
      </div>
      <div class="form-group">
        <label>网络等待超时 (秒)</label>
        <input type="number" id="cfg-network-timeout" min="10" max="300">
        <div class="hint">每次检查等待校园网可达的最长时间，10-300 秒</div>
      </div>
    </div>
    <div class="section">
      <h3>Web UI</h3>
      <div class="form-group">
        <label>UI 端口</label>
        <input type="number" id="cfg-ui-port" min="1024" max="65535">
        <div class="hint">修改后需重启服务生效；避免与系统端口冲突</div>
      </div>
    </div>
    <div class="section">
      <h3>密码 <span class="badge" id="pwd-badge"></span></h3>
      <div class="form-group">
        <label>新密码</label>
        <input type="password" id="cfg-password" placeholder="留空表示不修改">
        <div class="hint">至少 1 个字符；保存后立即生效（不必重启）</div>
        <div class="error" id="pwd-error"></div>
      </div>
      <button class="btn" onclick="savePassword()">修改密码</button>
    </div>
    <div style="text-align: right;">
      <button class="btn btn-big" onclick="saveConfig()">💾 保存配置</button>
    </div>
  </div>

  <div id="tab-log" class="tab-panel">
    <div class="log-toolbar">
      <input type="text" id="log-filter" placeholder="过滤关键字 (留空显示全部)" oninput="applyFilter()">
      <button class="btn btn-secondary" onclick="refreshLog(true)">🔄 刷新</button>
      <button class="btn btn-secondary" onclick="downloadLog()">⬇ 下载完整日志</button>
      <label class="checkbox-group" style="margin-left: auto;">
        <input type="checkbox" id="log-autoscroll" checked>
        <span class="muted" style="font-size: 13px;">自动滚动</span>
      </label>
    </div>
    <div class="log-box" id="log-box">(日志为空)</div>
    <p class="muted" id="log-info" style="margin-top: 8px;"></p>
  </div>

  <div id="tab-about" class="tab-panel">
    <div class="section">
      <h3>版本信息</h3>
      <dl class="about-grid">
        <dt>版本</dt><dd id="about-version">-</dd>
        <dt>服务启动时间</dt><dd id="about-started">-</dd>
        <dt>已运行时长</dt><dd id="about-uptime">-</dd>
      </dl>
    </div>
    <div class="section">
      <h3>文件位置</h3>
      <dl class="about-grid">
        <dt>数据目录</dt><dd id="about-data-dir">-</dd>
        <dt>日志文件</dt><dd id="about-log-path">-</dd>
        <dt>配置文件</dt><dd id="about-config-path">-</dd>
        <dt>密码文件</dt><dd id="about-password-path">-</dd>
      </dl>
    </div>
    <div class="section">
      <h3>管理操作</h3>
      <div class="btn-row">
        <button class="btn btn-secondary" onclick="restartService()">🔁 重启服务</button>
        <button class="btn btn-danger" onclick="showUninstallHint()">🗑 卸载服务</button>
      </div>
      <p class="muted" id="admin-hint" style="margin-top: 12px;"></p>
    </div>
  </div>
</div>

<script>
(function() {
  'use strict';

  // ---------- 工具 ----------
  function $(id) { return document.getElementById(id); }
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function(c) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
  function fmtTime(sec) {
    if (sec === null || sec === undefined || isNaN(sec)) return '-';
    sec = Math.max(0, parseInt(sec, 10));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    return h + 'h ' + m + 'm ' + s + 's';
  }
  function toast(msg, type) {
    var el = document.createElement('div');
    el.className = 'toast' + (type ? ' ' + type : '');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function() { if (el.parentNode) el.parentNode.removeChild(el); }, 3500);
  }

  // ---------- API ----------
  var API = {
    status: function() { return fetch('/api/status').then(function(r){ return r.json(); }); },
    config: function() { return fetch('/api/config').then(function(r){ return r.json(); }); },
    postConfig: function(body) { return fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(function(r){ return r.json(); }); },
    postPassword: function(pwd) { return fetch('/api/password', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pwd})}).then(function(r){ return r.json(); }); },
    triggerLogin: function() { return fetch('/api/login', {method:'POST'}).then(function(r){ return r.json(); }); },
    logTail: function(offset, max) { return fetch('/api/log_tail?offset=' + offset + '&max=' + max).then(function(r){ return r.json(); }); },
    about: function() { return fetch('/api/about').then(function(r){ return r.json(); }); },
    restart: function() { return fetch('/api/restart', {method:'POST'}).then(function(r){ return r.json(); }); }
  };

  // ---------- 主题 ----------
  function applyTheme(theme) {
    if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
    else document.documentElement.removeAttribute('data-theme');
    var btn = $('btn-theme');
    if (btn) btn.textContent = theme === 'dark' ? '☀️ 日间模式' : '🌙 夜间模式';
  }
  window.toggleTheme = function() {
    var cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    var next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem('theme', next); } catch (e) {}
  };
  try {
    var saved = localStorage.getItem('theme');
    if (saved === 'dark' || saved === 'light') applyTheme(saved);
  } catch (e) {}

  // ---------- Tab ----------
  document.querySelectorAll('.tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      var target = tab.dataset.tab;
      document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
      document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
      tab.classList.add('active');
      $('tab-' + target).classList.add('active');
      if (target === 'config') loadConfig();
      else if (target === 'about') loadAbout();
      else if (target === 'log') resetLog();
    });
  });

  // ---------- 状态 ----------
  var statusTimer = null;
  function refreshStatus() {
    API.status().then(function(s) { renderStatus(s); }).catch(function(e) { console.error(e); });
  }
  function renderStatus(s) {
    var cards = [
      {label: '网络可达性', value: s.network_reachable === null ? '⏳ 未知' : (s.network_reachable ? '✅ 可达' : '❌ 不可达'),
       cls: s.network_reachable === null ? 'status-warn' : (s.network_reachable ? 'status-ok' : 'status-bad')},
      {label: '在线状态', value: s.online === null ? '⏳ 未知' : (s.online ? '✅ 已登录' : '❌ 未登录'),
       cls: s.online === null ? 'status-warn' : (s.online ? 'status-ok' : 'status-bad')},
      {label: '当前账号', value: s.current_account || '-', cls: ''},
      {label: '上次登录时间', value: s.last_login_at || '从未', cls: ''},
      {label: '上次错误', value: s.last_error || '无', cls: s.last_error ? 'status-bad' : ''},
      {label: '下次检查',
       value: s.next_check_at ? (s.next_check_at + ' (剩余 ' + (s.next_check_in_sec || 0) + 's)') : '未计划',
       cls: ''}
    ];
    var html = cards.map(function(c) {
      return '<div class="card"><div class="card-label">' + escapeHtml(c.label) + '</div>' +
             '<div class="card-value ' + c.cls + '">' + escapeHtml(c.value) + '</div></div>';
    }).join('');
    $('status-cards').innerHTML = html;
    var btn = $('btn-login');
    btn.disabled = !!s.login_in_progress;
    $('login-status-text').textContent = s.login_in_progress ? '⏳ 登录进行中...' : '点击按钮立即触发一次完整检查';
  }
  window.triggerLogin = function() {
    API.triggerLogin().then(function(r) {
      if (r.triggered) toast('已触发登录', 'success');
      else toast('已忽略：' + (r.reason || '未知'), 'error');
    }).catch(function() { toast('请求失败', 'error'); });
  };
  function startStatusPolling() {
    if (statusTimer) return;
    refreshStatus();
    statusTimer = setInterval(refreshStatus, 3000);
  }

  // ---------- 配置 ----------
  function loadConfig() {
    API.config().then(function(c) {
      $('cfg-host').value = c.host || '';
      $('cfg-port').value = c.port || 801;
      $('cfg-account').value = c.account || '';
      $('cfg-suffix').value = c.suffix || '';
      $('cfg-auto-enabled').checked = !!c.auto_check_enabled;
      $('cfg-auto-interval').value = String(c.auto_check_interval_min || 30);
      $('cfg-network-timeout').value = c.network_wait_timeout_sec || 60;
      $('cfg-ui-port').value = c.ui_port || 8848;
      var badge = $('pwd-badge');
      if (c.password_status === 'set') {
        badge.textContent = '✅ 已设置';
        badge.className = 'badge set';
      } else {
        badge.textContent = '❌ 未设置';
        badge.className = 'badge missing';
      }
    }).catch(function() { toast('加载配置失败', 'error'); });
  }
  window.saveConfig = function() {
    var cfg = {
      host: $('cfg-host').value.trim(),
      port: parseInt($('cfg-port').value, 10),
      account: $('cfg-account').value.trim(),
      suffix: $('cfg-suffix').value,
      auto_check_enabled: $('cfg-auto-enabled').checked,
      auto_check_interval_min: parseInt($('cfg-auto-interval').value, 10),
      network_wait_timeout_sec: parseInt($('cfg-network-timeout').value, 10),
      ui_port: parseInt($('cfg-ui-port').value, 10)
    };
    API.postConfig(cfg).then(function(r) {
      if (r.ok) toast('配置已保存', 'success');
      else toast('保存失败：' + (r.error || '未知错误'), 'error');
    }).catch(function() { toast('请求失败', 'error'); });
  };
  window.savePassword = function() {
    var pwd = $('cfg-password').value;
    var errEl = $('pwd-error');
    errEl.textContent = '';
    if (!pwd) { errEl.textContent = '密码不能为空'; return; }
    API.postPassword(pwd).then(function(r) {
      if (r.ok) {
        toast('密码已更新', 'success');
        $('cfg-password').value = '';
        loadConfig();
      } else {
        errEl.textContent = r.error || '保存失败';
      }
    }).catch(function() { errEl.textContent = '请求失败'; });
  };

  // ---------- 日志 ----------
  var logOffset = 0;
  var logBuffer = '';
  var logFilter = '';
  var logTimer = null;
  window.applyFilter = function() {
    logFilter = $('log-filter').value;
    renderLog();
  };
  window.refreshLog = function(force) {
    if (force) { logOffset = 0; logBuffer = ''; }
    API.logTail(logOffset, 500).then(function(r) {
      if (r.lines && r.lines.length > 0) {
        logBuffer += r.lines.join('\n') + '\n';
        logOffset = r.next_offset;
      }
      renderLog();
      $('log-info').textContent = 'offset: ' + logOffset + ' / size: ' + (r.total_size || 0) + ' bytes';
    }).catch(function(e) { console.error(e); });
  };
  function renderLog() {
    var box = $('log-box');
    var text = logBuffer;
    if (logFilter) {
      text = logBuffer.split('\n').filter(function(l) { return l.indexOf(logFilter) !== -1; }).join('\n');
    }
    box.textContent = text || '(日志为空)';
    if ($('log-autoscroll').checked) box.scrollTop = box.scrollHeight;
  }
  window.resetLog = function() { logOffset = 0; logBuffer = ''; refreshLog(); };
  window.downloadLog = function() {
    window.location.href = '/api/log_download';
  };
  function startLogPolling() {
    if (logTimer) return;
    refreshLog();
    logTimer = setInterval(function() { refreshLog(); }, 2000);
  }

  // ---------- 关于 ----------
  function loadAbout() {
    API.about().then(function(r) {
      $('about-version').textContent = r.version || '-';
      $('about-started').textContent = r.service_started_at || '-';
      $('about-uptime').textContent = fmtTime(r.service_uptime_sec);
      $('about-data-dir').innerHTML = '<code class="path">' + escapeHtml(r.data_dir) + '</code>';
      $('about-log-path').innerHTML = '<code class="path">' + escapeHtml(r.log_file) + '</code>';
      $('about-config-path').innerHTML = '<code class="path">' + escapeHtml(r.config_file) + '</code>';
      $('about-password-path').innerHTML = '<code class="path">' + escapeHtml(r.password_file) + '</code>';
    }).catch(function() { toast('加载关于信息失败', 'error'); });
  }
  window.restartService = function() {
    if (!confirm('确认重启服务？NSSM 将自动重新拉起进程。')) return;
    API.restart().then(function() {
      toast('服务正在重启，3 秒后请刷新浏览器', 'success');
    }).catch(function() { toast('重启请求失败', 'error'); });
  };
  window.showUninstallHint = function() {
    $('admin-hint').textContent = '请以管理员身份运行同目录下的 uninstall.bat 完成卸载。';
  };

  // ---------- 启动 ----------
  startStatusPolling();
  startLogPolling();

  // 每秒刷新一次"已运行时长"显示（仅在 About 标签可见时）
  setInterval(function() {
    if ($('tab-about').classList.contains('active')) {
      API.about().then(function(r) {
        $('about-uptime').textContent = fmtTime(r.service_uptime_sec);
      }).catch(function() {});
    }
  }, 1000);
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