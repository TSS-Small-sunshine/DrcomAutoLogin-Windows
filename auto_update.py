# -*- coding: utf-8 -*-
"""auto_update.py — 自动升级（v1.3 新增）业务层。

职责范围：
    - GitHub releases/latest 探测（主源 + 镜像 fallback）
    - 安装器下载（主源 + 镜像 fallback + 进度回调）
    - SHA256 校验 + 备份当前服务脚本
    - NSSM 注册表操作（备份 AppExit 等透明升级策略）
    - 后台线程 _auto_update_loop（按 cfg.update_check_interval_hours 周期检查）
    - 启动钩子 _post_upgrade_startup（升级完成后清理）
    - 自动升级状态机 _set_update_state / _acquire_update_lock / _release_update_lock

设计：
    - 不直接持有 STATE / LOCK；通过 _attach() 由 联网_service.py 在 main() 注入
    - 共享字典 / 锁 / 函数 / 路径常量挂到本模块 globals，原函数体裸名引用无须重写

依赖：仅 Python 3 标准库。
"""

import datetime as _dt  # noqa: F401  备用 datetime 别名
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from version import VERSION


# ============================================================
# 升级常量（从 联网_service.py 逐字搬入）
# ============================================================
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



# ============================================================
# 自动升级函数（从 联网_service.py 逐字搬入）
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
# 运行时引用注入（由 联网_service.py 在 import 时调用）
# ============================================================
def _attach(*, logger, state, state_lock, update_lock,
            tools_dir, nssm_path, upgrade_log_file, log_dir, base_dir,
            load_config, save_config, now_iso,
            stop_event):
    """由 联网_service.py 调用，注入共享对象到本模块 globals。

    与 web_api 的 _attach 策略一致：所有共享名字直接绑到本模块 globals，
    让原 auto_update 函数体的 _load_config / STATE_LOCK / NSSM_PATH 等
    裸名引用无须重写即可工作。
    """
    g = globals()
    g["logger"] = logger
    g["STATE"] = state
    g["STATE_LOCK"] = state_lock
    g["UPDATE_LOCK"] = update_lock
    g["TOOLS_DIR"] = tools_dir
    g["NSSM_PATH"] = nssm_path
    g["UPGRADE_LOG_FILE"] = upgrade_log_file
    g["LOG_DIR"] = log_dir
    g["BASE_DIR"] = base_dir
    g["_load_config"] = load_config
    g["_save_config"] = save_config
    g["_now_iso"] = now_iso
    g["STOP_EVENT"] = stop_event


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
