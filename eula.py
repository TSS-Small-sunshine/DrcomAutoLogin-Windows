# -*- coding: utf-8 -*-
"""eula.py — EULA / CHANGELOG 等静态文本文件 IO。

职责范围：
    - api_get_changelog() — 读取仓库根 CHANGELOG.md 并包装成 HTTP 响应
    - read_eula()        — EULA 文件 IO 钩子（当前未启用，留接口）

设计：
    - 与 web_api.py 解耦：文件 IO 错误统一转成 (status_code, payload) 元组
    - 由 web_api.py 在收到 GET /api/changelog 时调用 api_get_changelog()
    - 路径来自 BASE_DIR，与原版保持一致
"""

import os

from version import VERSION


# ============================================================
# 共享路径（由 联网_service.py 在 import 时注入 BASE_DIR）
# ============================================================
_BASE_DIR = None


def _attach(*, base_dir):
    """由 联网_service.py 调用，注入 BASE_DIR。

    解耦目标：eula.py 不直接 import 联网_service（避免循环）。
    """
    global _BASE_DIR
    _BASE_DIR = base_dir


# ============================================================
# CHANGELOG
# ============================================================
def api_get_changelog():
    """GET /api/changelog — 返回仓库根目录 CHANGELOG.md 内容（UTF-8 文本）。

    路径说明：CHANGELOG.md 与 联网_service.py 同在仓库根目录（即 BASE_DIR）。
    安装场景下 Inno Setup 把 CHANGELOG.md 复制到 {app}（与 联网_service.py 同级），
    所以生产环境也是 BASE_DIR/CHANGELOG.md，与开发环境一致。

    返回 (status_code, dict) 元组 — 便于 web_api.py 直接转发。
    """
    if _BASE_DIR is None:
        return 500, {"error": "eula module not attached", "content": ""}
    cl_path = os.path.join(_BASE_DIR, "CHANGELOG.md")
    cl_path = os.path.normpath(cl_path)
    try:
        with open(cl_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        # 缺失 / 权限 / 编码等任何 OS 级错误都走这里，统一返回 500
        return 500, {"error": "read changelog failed: {}".format(exc), "content": ""}
    return 200, {
        "content": content,
        "size": len(content),
        "version": VERSION,
    }


# ============================================================
# EULA（预留接口；当前未挂到 HTTP 路由）
# ============================================================
def read_eula():
    """读取 BASE_DIR/EULA.md（如存在）。文件不存在 → 返回 None。

    后续如需在 Web UI 展示 EULA，可在 web_api.py 加 /api/eula 路由
    直接 return read_eula()。
    """
    if _BASE_DIR is None:
        return None
    eula_path = os.path.join(_BASE_DIR, "EULA.md")
    eula_path = os.path.normpath(eula_path)
    if not os.path.isfile(eula_path):
        return None
    try:
        with open(eula_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None