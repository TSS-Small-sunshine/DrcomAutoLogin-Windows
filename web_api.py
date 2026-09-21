# -*- coding: utf-8 -*-
"""web_api.py — HTTP 路由层（薄薄一层）。

职责范围：
    - HTTP 响应辅助（_send_json / _send_bytes / _read_json_body）
    - 所有 api_* 函数（HTTP 路由处理）
    - _Handler 类（do_GET / do_POST 调度）
    - _HTML_PAGE（单页应用，全部内联 CSS/JS）

设计：
    - 不持有业务逻辑；调用 protocol / auto_update / eula 模块
    - 与 main() 解耦：main() 通过 ThreadingHTTPServer((host, port), _Handler) 注入 handler
    - 共用共享资源（STATE / cfg / logger / 常量）通过 _attach() 由 联网_service.py 注入

依赖：仅 Python 3 标准库。
"""

import io
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler

from version import VERSION
import eula as _eula_mod


# ============================================================
# 配置导入/导出常量
# ============================================================
CONFIG_EXPORT_SCHEMA_VERSION = 1
CONFIG_EXPORT_TOOL = "DrcomAutoLogin-Windows"
CONFIG_IMPORT_MAX_BYTES = 4 * 1024 * 1024  # 4MB 安全上限


# ============================================================
# 运行时引用（由 联网_service.py 在 import 时注入）
# ============================================================
def _attach(*, logger, run_lock, base_dir, log_file, log_dir,
            # 字典（直接挂到本模块 globals，API 代码用原名访问）
            state, state_lock, pwd_value, pwd_lock,
            config_file, password_file, upgrade_log_file,
            default_config,
            allowed_suffixes, allowed_intervals, allowed_update_intervals,
            # 函数（直接挂到本模块 globals，API 代码用原名访问）
            load_config, save_config, save_password_to_disk,
            validate_config, snapshot_state, get_password, now_iso,
            stop_event, run_once_fn,
            # 注入到 web_api 模块命名空间，标志 / 兼容别名
            eula_api_get_changelog=None,
            # auto_update 模块的可选注入（commit 5 才存在；现 commit 4 暂不引用）
            auto_update_mod=None):
    """由 联网_service.py 调用，注入共享对象到本模块命名空间。

    设计要点：
        - 把所有共享名字直接绑到本模块 globals，让原 API 函数体里的 STATE / STOP_EVENT
          / BASE_DIR / UPGRADE_LOG_FILE 等**裸名引用**无须重写即可工作。
        - 与 联网_service.py 的解耦只通过 _attach() 一个入口完成。
    """
    g = globals()
    # 日志
    g["logger"] = logger
    # 字典与锁
    g["STATE"] = state
    g["STATE_LOCK"] = state_lock
    g["BACKOFF"] = state.get  # 占位，web_api 不直接读 BACKOFF
    g["RUN_LOCK"] = run_lock
    g["_PWD_VALUE"] = pwd_value
    g["PWD_LOCK"] = pwd_lock
    # 路径常量
    g["BASE_DIR"] = base_dir
    g["CONFIG_FILE"] = config_file
    g["PASSWORD_FILE"] = password_file
    g["LOG_FILE"] = log_file
    g["LOG_DIR"] = log_dir
    g["UPGRADE_LOG_FILE"] = upgrade_log_file
    # 字典常量
    g["DEFAULT_CONFIG"] = default_config
    g["ALLOWED_SUFFIXES"] = allowed_suffixes
    g["ALLOWED_INTERVALS"] = allowed_intervals
    g["ALLOWED_UPDATE_INTERVALS"] = allowed_update_intervals
    # 函数（保持原名以便 API 函数体无须改写）
    g["_load_config"] = load_config
    g["_save_config"] = save_config
    g["_save_password_to_disk"] = save_password_to_disk
    g["_validate_config"] = validate_config
    g["_snapshot_state"] = snapshot_state
    g["_get_password"] = get_password
    g["_now_iso"] = now_iso
    g["STOP_EVENT"] = stop_event
    g["run_once"] = run_once_fn
    # eula 模块的 api_get_changelog
    if eula_api_get_changelog is None:
        from eula import api_get_changelog as eula_api_get_changelog  # noqa: E402
    g["api_get_changelog"] = eula_api_get_changelog
    # auto_update 模块（commit 5 之后才注入；这里只是占位）
    g["_auto_update_mod"] = auto_update_mod


def _log(msg, *args, level=logging.INFO):
    """薄薄的 logger 包装；logger 未注入时静默。"""
    log = globals().get("logger")
    if log is None:
        return
    if level == logging.INFO:
        log.info(msg, *args)
    elif level == logging.WARNING:
        log.warning(msg, *args)
    elif level == logging.ERROR:
        log.error(msg, *args)
    elif level == logging.DEBUG:
        log.debug(msg, *args)
    else:
        log.log(level, msg, *args)


# ============================================================
# 原 Web API / _Handler（从 联网_service.py 逐字搬入）
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


# —— 日志等级正则：从行内提取 [LEVEL] 前缀（用于按等级过滤 / 配色） ——
_LOG_LEVEL_RE = re.compile(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]")
_VALID_LOG_LEVELS = ("debug", "info", "warning", "error", "critical")


def _parse_log_level(line):
    """从日志行提取等级；不识别返回 None。"""
    if not isinstance(line, str):
        return None
    m = _LOG_LEVEL_RE.search(line)
    return m.group(1).lower() if m else None


def api_get_log_tail(offset, max_lines, level=None):
    """返回日志尾部。

    level: None/空 = 不过滤；否则必须是 debug/info/warning/error/critical 之一。
    返回 {"lines": [...], "next_offset": int, "total_size": int, "level_filter": str|None,
          "error"?: str}；非法 level 通过 raise ValueError 让上层 do_GET 返 400。
    """
    offset = max(0, int(offset or 0))
    max_lines = max(1, min(int(max_lines or 200), 5000))
    level_filter = None
    if level is not None:
        level_str = str(level).strip().lower()
        if level_str:
            if level_str not in _VALID_LOG_LEVELS:
                raise ValueError("level 必须是 {} 之一".format("/".join(_VALID_LOG_LEVELS)))
            level_filter = level_str
    if not os.path.isfile(LOG_FILE):
        return {"lines": [], "next_offset": 0, "total_size": 0, "level_filter": level_filter}
    total = os.path.getsize(LOG_FILE)
    if offset >= total:
        return {"lines": [], "next_offset": total, "total_size": total, "level_filter": level_filter}
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError as exc:
        return {"lines": [], "next_offset": offset, "total_size": total, "error": str(exc), "level_filter": level_filter}
    # 拆分行为保留最后 max_lines 行
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        text = data.decode("latin-1", errors="replace")
    lines = text.splitlines()
    if level_filter:
        lines = [ln for ln in lines if _parse_log_level(ln) == level_filter]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        next_offset = total  # 已截断，next_offset 标记为文件末尾
    else:
        next_offset = offset + len(data)
    return {"lines": lines, "next_offset": next_offset, "total_size": total, "level_filter": level_filter}


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


def api_get_changelog():
    """CHANGELOG IO 已迁移到 eula.py（v2.0.2 解耦）。
    
    此函数由 _Handler.do_GET 调用，保留为薄壳以减少 do_GET 路由改动。
    commit 4 抽出 web_api.py 时将整体迁移。
    """
    return _eula_mod.api_get_changelog()


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
# 配置导入/导出（zip）
# ============================================================
def _build_config_export_zip():
    """把当前 config.json + password.txt（如有）+ manifest.json 打包成 bytes。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "schema_version": CONFIG_EXPORT_SCHEMA_VERSION,
            "exported_at": _now_iso(),
            "service_version": VERSION,
            "tool": CONFIG_EXPORT_TOOL,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        config_path = os.path.join(BASE_DIR, "config.json")
        if os.path.isfile(config_path):
            with open(config_path, "rb") as f:
                zf.writestr("config.json", f.read())
        pwd_path = os.path.join(BASE_DIR, "password.txt")
        if os.path.isfile(pwd_path):
            with open(pwd_path, "rb") as f:
                zf.writestr("password.txt", f.read())
    return buf.getvalue()


def api_get_config_export(handler):
    """GET /api/config/export — 导出 zip。"""
    try:
        data = _build_config_export_zip()
    except (OSError, zipfile.BadZipFile) as exc:
        logger.exception("config export failed: %s", exc)
        _send_json(handler, 500, {"ok": False, "error": "export failed: {}".format(exc)})
        return
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = "config-export-{}.zip".format(ts)
    _send_bytes(handler, 200, "application/zip", data, filename=fname)


def api_post_config_import(handler):
    """POST /api/config/import — 上传 zip 应用配置。"""
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        length = 0
    if length <= 0:
        _send_json(handler, 400, {"ok": False, "error": "empty body"})
        return
    if length > CONFIG_IMPORT_MAX_BYTES:
        _send_json(handler, 413, {"ok": False, "error": "body too large (>{} bytes)".format(CONFIG_IMPORT_MAX_BYTES)})
        return
    raw = handler.rfile.read(length)
    if not raw:
        _send_json(handler, 400, {"ok": False, "error": "empty body"})
        return
    applied = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                _send_json(handler, 400, {"ok": False, "error": "missing manifest.json"})
                return
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            if int(manifest.get("schema_version", -1)) != CONFIG_EXPORT_SCHEMA_VERSION:
                _send_json(handler, 400, {"ok": False, "error": "unsupported schema_version: {}".format(manifest.get("schema_version"))})
                return
            if "config.json" not in names:
                _send_json(handler, 400, {"ok": False, "error": "missing config.json"})
                return
            cfg_text = zf.read("config.json").decode("utf-8")
            cfg_data = json.loads(cfg_text)
            errors = _validate_config(cfg_data)
            if errors:
                _send_json(handler, 400, {"ok": False, "error": "config 校验失败: " + "; ".join(errors)})
                return
            _save_config(cfg_data)
            applied.append("config")
            if "password.txt" in names:
                pwd_bytes = zf.read("password.txt")
                if pwd_bytes.strip():
                    pwd_path = os.path.join(BASE_DIR, "password.txt")
                    tmp = pwd_path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(pwd_bytes)
                    os.replace(tmp, pwd_path)
                    applied.append("password")
                else:
                    logger.warning("import password.txt is empty, skipped")
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.exception("config import failed: %s", exc)
        _send_json(handler, 400, {"ok": False, "error": "import failed: {}".format(exc)})
        return
    _send_json(handler, 200, {"ok": True, "applied": applied, "need_restart": True})


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
                level = params.get("level", "")
                try:
                    payload = api_get_log_tail(offset, maxn, level)
                except ValueError as exc:
                    _send_json(self, 400, {"error": str(exc)})
                    return
                _send_json(self, 200, payload)
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
            # —— 更新日志（v2.0.0 新增）——
            if path == "/api/changelog":
                status, body = api_get_changelog()
                _send_json(self, status, body)
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
            # —— 配置导入/导出（zip）——
            if path == "/api/config/export":
                api_get_config_export(self)
                return
            _send_json(self, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("GET %s 异常: %s", path, exc)
            _send_json(self, 500, {"error": "internal: {}".format(exc)})

    def do_POST(self):
        path, _params = self._parse_url()
        # —— 二进制上传（zip）在 JSON 解析之前专路分发 —— 现有 JSON 类端点行为零变更
        if path == "/api/config/import":
            try:
                api_post_config_import(self)
            except Exception as exc:  # noqa: BLE001
                logger.exception("POST %s 异常: %s", path, exc)
                _send_json(self, 500, {"ok": False, "error": "internal: {}".format(exc)})
            return
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


_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>星尘闪连 (Stardust Flash Link) — Dr.COM 校园网自动登录</title>
<link rel="icon" type="image/png" href="branding/web-logo-32.png">
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

/* —— 配置导入导出按钮行 —— */
.config-io-row { display: flex; gap: 12px; margin: 16px 0; flex-wrap: wrap; }

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
/* —— 日志等级 chip 筛选 —— */
.log-level-filter { display: inline-flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-radius: var(--radius-pill);
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
  font: inherit; font-size: 12.5px; font-weight: 600; line-height: 1.2;
  cursor: pointer; user-select: none;
  transition: background-color 0.15s, border-color 0.15s, color 0.15s, box-shadow 0.15s, transform 0.1s;
}
.chip:hover { background: var(--surface-2); border-color: var(--primary-soft); }
.chip:active { transform: translateY(1px); }
.chip:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.chip.chip-active { background: var(--primary); border-color: var(--primary); color: #fff; box-shadow: 0 2px 8px rgba(59, 91, 219, 0.25); }
.chip-level[data-level="debug"] { color: var(--text-muted); }
.chip-level[data-level="info"] { color: var(--primary); }
.chip-level[data-level="warning"] { color: var(--warn); }
.chip-level[data-level="error"] { color: var(--err); }
.chip-level[data-level="critical"] { color: var(--err); font-weight: 700; }
.chip-level.chip-active[data-level="debug"] { background: var(--surface-2); border-color: var(--border); color: var(--text); }
.chip-level.chip-active[data-level="info"] { background: var(--primary); border-color: var(--primary); color: #fff; }
.chip-level.chip-active[data-level="warning"] { background: var(--warn); border-color: var(--warn); color: #fff; }
.chip-level.chip-active[data-level="error"],
.chip-level.chip-active[data-level="critical"] { background: var(--err); border-color: var(--err); color: #fff; }

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
      <span class="brand-mark" aria-hidden="true"><img src="branding/web-logo-64.png" alt="星尘闪连" /></span>
      <span class="brand-name">星尘闪连 — Dr.COM 校园网自动登录</span>
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
  <a class="hint-strip-link" href="https://github.com/TSS-Small-sunshine/StardustFlashLink" target="_blank" rel="noopener noreferrer">查看源码 →</a>
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
      <div class="field">
        <div class="hint" style="margin-bottom:8px;">手动触发</div>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button class="btn btn-secondary" id="btn-update-check-now" type="button">🔍 立即检查更新</button>
          <button class="btn btn-secondary" id="btn-update-install-now" type="button">⬆️ 立即升级</button>
        </div>
        <div class="hint" style="margin-top:6px;">点「立即检查更新」拉 GitHub；发现新版再点「立即升级」（升级前自动停服务，约 30-60 秒）</div>
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

    <div class="config-io-row">
      <button class="btn btn-secondary" id="btn-config-export" type="button">📤 导出配置</button>
      <button class="btn btn-secondary" id="btn-config-import" type="button">📥 导入配置</button>
      <input type="file" id="config-import-file" accept=".zip" style="display:none">
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
        <div class="log-level-filter" id="log-level-filter" role="group" aria-label="日志等级筛选">
          <button class="chip chip-level chip-active" data-level="" type="button" aria-pressed="true">全部</button>
          <button class="chip chip-level" data-level="info" type="button" aria-pressed="false">INFO</button>
          <button class="chip chip-level" data-level="warning" type="button" aria-pressed="false">WARN</button>
          <button class="chip chip-level" data-level="error" type="button" aria-pressed="false">ERROR</button>
        </div>
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
        <a class="btn" href="https://github.com/TSS-Small-sunshine/StardustFlashLink" target="_blank" rel="noopener noreferrer">📦 在 GitHub 上查看</a>
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
        <!-- v2.0.0 新增：查看更新日志按钮 -->
        <button class="btn btn-secondary" id="btn-changelog" type="button">📜 查看更新日志</button>
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

<!-- v2.0.0 新增：更新日志弹窗（默认隐藏，由 JS 控制） -->
<div class="changelog-modal" id="changelog-modal" hidden role="dialog" aria-modal="true" aria-labelledby="changelog-title">
  <div class="changelog-backdrop" id="changelog-backdrop"></div>
  <div class="changelog-dialog" role="document">
    <div class="changelog-header">
      <h2 class="changelog-title" id="changelog-title">📜 更新日志</h2>
      <button class="changelog-close" id="changelog-close" type="button" aria-label="关闭">✕</button>
    </div>
    <pre class="changelog-body" id="changelog-body">加载中...</pre>
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

/* 更新日志弹窗（v2.0.0 新增） */
.changelog-modal { position: fixed; inset: 0; z-index: 9999; display: flex; align-items: center; justify-content: center; }
.changelog-modal[hidden] { display: none; }
.changelog-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); }
.changelog-dialog { position: relative; max-width: 800px; max-height: 80vh; width: 90%; background: var(--surface, #fff); border-radius: 12px; display: flex; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.2); overflow: hidden; }
.changelog-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border, #e2e8f0); }
.changelog-title { font-size: 16px; font-weight: 700; margin: 0; color: var(--text-strong, #1a1a2e); }
.changelog-close { background: transparent; border: 0; font-size: 18px; cursor: pointer; color: var(--text-muted, #64748b); padding: 4px 8px; border-radius: 6px; }
.changelog-close:hover { background: var(--surface-2, #f5f7fb); color: var(--text-strong, #1a1a2e); }
.changelog-body { flex: 1; overflow: auto; padding: 20px; font-family: ui-monospace, "Cascadia Code", "Fira Code", Menlo, monospace; font-size: 12.5px; line-height: 1.6; white-space: pre-wrap; color: var(--text, #1a1a2e); margin: 0; background: var(--surface, #fff); }
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
    logTail: function (offset, max, level) {
      var url = '/api/log_tail?offset=' + encodeURIComponent(offset) + '&max=' + encodeURIComponent(max);
      if (level) url += '&level=' + encodeURIComponent(level);
      return getJson(url);
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
  var logLevel = '';  /* 当前等级筛选：'' 表示全部 */

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
    return API.logTail(logOffset, 500, logLevel).then(function (r) {
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
    /* —— 等级 chip 筛选：切 chip → 重拉（level 不同了 offset 不能再叠加） —— */
    var group = $('log-level-filter');
    if (group) {
      var chips = Array.prototype.slice.call(group.querySelectorAll('.chip-level'));
      chips.forEach(function (chip) {
        chip.addEventListener('click', function () {
          var picked = chip.getAttribute('data-level') || '';
          if (picked === logLevel) return;
          chips.forEach(function (c) {
            var on = c === chip;
            c.classList.toggle('chip-active', on);
            c.setAttribute('aria-pressed', on ? 'true' : 'false');
          });
          logLevel = picked;
          reloadLog();
        });
      });
    }
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
      // v2.0.0 新增：在 action 按钮旁加一个"📋 查看更新日志"快捷链接
      ensureChangelogShortcut(banner, action);
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
    // 更新日志按钮（关于面板，v2.0.0 新增）
    var btnCl = $('btn-changelog');
    if (btnCl) btnCl.addEventListener('click', openChangelogModal);
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
        var cm = $('changelog-modal');
        if (cm && !cm.hidden) cm.hidden = true;
      }
    });
    // 更新日志弹窗关闭按钮（v2.0.0 新增）
    var clClose = $('changelog-close');
    var clBackdrop = $('changelog-backdrop');
    if (clClose) clClose.addEventListener('click', closeChangelogModal);
    if (clBackdrop) clBackdrop.addEventListener('click', closeChangelogModal);
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

  // —— 更新日志（v2.0.0 新增）——
  function openChangelogModal() {
    var modal = $('changelog-modal');
    var body = $('changelog-body');
    if (!modal || !body) return;
    body.textContent = '加载中...';
    modal.hidden = false;
    fetch('/api/changelog').then(function (r) { return r.json(); }).then(function (data) {
      if (data && data.content) {
        body.textContent = data.content;
      } else {
        body.textContent = '加载失败：' + ((data && data.error) || '未知错误');
      }
    }).catch(function () {
      body.textContent = '请求失败，请检查服务状态';
    });
  }

  function closeChangelogModal() {
    var modal = $('changelog-modal');
    if (modal) modal.hidden = true;
  }

  // 在升级成功横幅 action 按钮旁动态插入一个"📋 查看更新日志"快捷链接
  function ensureChangelogShortcut(banner, action) {
    if (!banner || !action) return;
    var link = banner.querySelector('.changelog-shortcut');
    if (link) return;
    link = document.createElement('button');
    link.className = 'update-banner-link changelog-shortcut';
    link.type = 'button';
    link.textContent = '📋 查看更新日志';
    link.style.cssText = 'margin-left:8px;background:transparent;border:0;color:var(--primary);cursor:pointer;font-size:12.5px;text-decoration:underline;padding:0;';
    link.addEventListener('click', function (ev) {
      ev.stopPropagation();
      openChangelogModal();
    });
    if (action.parentNode) {
      action.parentNode.insertBefore(link, action.nextSibling);
    }
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

  // —— 配置导入导出 ——
  (function () {
    var exportBtn = $('btn-config-export');
    if (exportBtn) exportBtn.addEventListener('click', function () {
      window.location.href = '/api/config/export';
    });
    var importBtn = $('btn-config-import');
    var fileInput = $('config-import-file');
    if (importBtn && fileInput) {
      importBtn.addEventListener('click', function () { fileInput.click(); });
      fileInput.addEventListener('change', function () {
        var f = fileInput.files && fileInput.files[0];
        if (!f) return;
        toast('正在导入配置...', 'warn', 4000);
        fetch('/api/config/import', { method: 'POST', body: f })
          .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
          .then(function (r) {
            if (r.ok && r.body && r.body.ok) {
              var applied = (r.body.applied || []).join(', ');
              toast('导入成功（' + applied + '）。需重启服务生效。', 'success', 6000);
            } else {
              toast('导入失败: ' + ((r.body && r.body.error) || '未知错误'), 'error', 6000);
            }
          })
          .catch(function () { toast('请求失败，请检查服务状态', 'error', 6000); })
          .then(function () { fileInput.value = ''; });
      });
    }
  })();

  // —— 手动触发更新（v1.3.5 新增）——
  // 复用 /api/update/check 与 /api/update/install 端点（v1.3 已有）。
  (function () {
    var checkBtn = $('btn-update-check-now');
    var installBtn = $('btn-update-install-now');
    if (checkBtn) checkBtn.addEventListener('click', function () {
      toast('正在检查 GitHub 最新版本...', 'warn');
      postUpdateJson('/api/update/check')
        .then(function (data) {
          if (data && data.ok) {
            toast('检查完成：' + (data.message || 'OK'), 'success', 4000);
          } else {
            toast('检查失败: ' + ((data && data.error) || '未知错误'), 'error', 6000);
          }
          if (typeof pollUpdate === 'function') pollUpdate();
        })
        .catch(function () { toast('请求失败，请检查服务状态', 'error'); });
    });
    if (installBtn) installBtn.addEventListener('click', function () {
      if (!confirm('确认立即升级？\n\n升级期间（约 30-60 秒）：\n- 服务会短暂停止\n- Web UI 不可用\n- 进度通过升级状态横幅实时显示\n\n继续？')) return;
      toast('正在触发升级...', 'warn', 4000);
      postUpdateJson('/api/update/install')
        .then(function (data) {
          if (data && data.ok) {
            toast('升级已启动，请稍候（约 60 秒）', 'success', 6000);
          } else {
            toast('升级启动失败: ' + ((data && data.error) || '未知错误'), 'error', 6000);
          }
          if (typeof pollUpdate === 'function') pollUpdate();
        })
        .catch(function () { toast('请求失败', 'error'); });
    });
  })();

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>
</body>
</html>
"""

