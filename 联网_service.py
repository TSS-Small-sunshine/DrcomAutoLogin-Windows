# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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

import json
import logging
import os
import re
import signal
import sys
import threading
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer


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
from protocol import run_once  # noqa: F401  协议层共享；main() 透传给 web_api
import protocol as _protocol_mod

# _attach 在 main() 里完成（STOP_EVENT 必须在 _attach 之前已存在）。

# ============================================================
# EULA / CHANGELOG IO 已迁移到 eula.py（v2.0.2 解耦）
# ============================================================
import eula as _eula_mod

# _eula_mod._attach() 在 main() 里调用（需要 BASE_DIR）。
# ============================================================
# Web API + _Handler + _HTML_PAGE 已迁移到 web_api.py（v2.0.2 解耦）
# ============================================================
import web_api as _web_api_mod

# _web_api_mod._attach() 在 main() 里调用（需要 BASE_DIR / STATE / cfg / logger 等）。
# ============================================================
# 自动升级（v1.3 新增）已迁移到 auto_update.py（v2.0.2 解耦）
# ============================================================
import auto_update as _auto_update_mod

# _auto_update_mod._attach() 在 main() 里调用（需要 STATE / LOCK / 路径 / 函数）。


# ============================================================
# 后台线程
# ============================================================
def _startup_trigger():
    """启动后稍等几秒再跑首次检查（让 Web server 先就绪 + 网络稳定）。

    v2.0.2：包一层 try/except，避免 run_once 抛异常时 daemon 线程静默死亡
    （之前的写法如果 _load_config 或 run_once 出错，整个启动触发就废了，
    只能靠 run_periodic 的 30min interval 救场 —— 用户感知为「自启动不触发」）。
    """
    if STOP_EVENT.wait(3):
        return
    try:
        run_once("startup")
    except Exception as exc:  # noqa: BLE001
        logger.exception("startup trigger 未捕获异常: %s", exc)


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

        # 计算 wait_sec：取 interval 与剩余退避时间的较小者。
        # 历史 bug（v2.0.2 修复）：原代码写 max(interval, delta)，导致
        #   - 开机早期网络未稳 → _startup_trigger 触发 run_once → wait_network 失败 → 5min 退避
        #   - run_periodic 第一次循环读 BACKOFF.until 后仍按 max 算出 30min interval
        #   - 用户感知：自启动后 30+ 分钟内没有任何登录尝试
        # 正确语义：退避已到期（delta<=0）→ 立即重试；否则取 min(interval, delta) 让退避生效
        bu = _backoff_until()
        wait_sec = interval_sec
        if bu:
            try:
                bu_dt = datetime.fromisoformat(bu)
                delta = (bu_dt - datetime.now()).total_seconds()
                if delta <= 0:
                    # 退避到期：立即触发一次 run_once（不等 interval）
                    wait_sec = 0
                elif delta < interval_sec:
                    # 退避 < interval：服从退避（按 delta 比 interval 大 → max 写法的旧 bug）
                    wait_sec = delta
                # else: delta >= interval → 保持 interval（合理：周期性不能比 interval 还短）
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
        _auto_update_mod._post_upgrade_startup()
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
    # 4.8 把共享状态注入 auto_update 模块（后台线程 / 升级流程需要）
    _auto_update_mod._attach(
        logger=logger,
        state=STATE,
        state_lock=STATE_LOCK,
        update_lock=UPDATE_LOCK,
        tools_dir=TOOLS_DIR,
        nssm_path=NSSM_PATH,
        upgrade_log_file=UPGRADE_LOG_FILE,
        log_dir=LOG_DIR,
        base_dir=BASE_DIR,
        load_config=_load_config,
        save_config=_save_config,
        now_iso=_now_iso,
        stop_event=STOP_EVENT,
    )

    # 5. 启动后台线程
    startup_thread = threading.Thread(target=_startup_trigger, name="startup-trigger", daemon=True)
    startup_thread.start()

    periodic_thread = threading.Thread(target=run_periodic, name="periodic-check", daemon=True)
    periodic_thread.start()

    # —— 自动升级后台线程（v1.3 新增）——
    auto_update_thread = threading.Thread(target=_auto_update_mod._auto_update_loop, name="auto-update", daemon=True)
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
