# 更新日志

本项目的所有重要变更都会记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范。

## [v2.0.0] - 2026-09-20

进入 2.0 时代。

### ✨ 新增
- **安装器视觉改版**：从用户自制 logo 改用纯生成的蔚蓝档案（Blue Archive）经典蓝渐变背景（`#A0D8EF` → `#3D7DC9` → `#1B3A6B`），164×314 24-bit BMP 嵌入安装器左侧品牌横幅
- **EULA 用户协议**（`packaging/branding/EULA.rtf`，ISCC `LicenseFile` 原生支持）：9 节完整条款 —— 服务范围 / 许可 / 用户责任 / 免责声明 / 隐私政策 / 第三方组件 / 协议修改 / 终止 / 适用法律
- **Web UI「关于」面板**新增 "📜 查看更新日志" 按钮 → 弹窗显示本文件内容
- **升级成功横幅**新增 "📋 查看更新日志" 快捷链接

### 🔧 变更
- 项目重命名为「星尘闪连 (Stardust Flash Link)」：setup.iss `MyAppName` 改；安装包文件名前缀仍为 `DrcomAutoLogin-Setup-v*`（保留历史 release URL 兼容）
- 内部版本号语义：从 1.x 公开版本线进入 2.x 公开版本线
- Web UI 顶部 `<title>` 与品牌标识改为 "星尘闪连"
- README 增加 logo + 5 徽章带 + 18 项 TOC

### 🐛 修复
- 无（2.0 主要是视觉 / 法务姿态升级，技术栈不变）

---

## [v1.4.0] - 2026-09-20

### ✨ 新增
- **项目重命名为「星尘闪连 (Stardust Flash Link)」**
- 安装包新增 `branding\app.ico`（应用图标，256/128/64/48/32/24/16 多尺寸 ICO）
- 安装包新增 `branding\wizard.bmp`（164×314 24-bit 安装器左侧品牌横幅）
- README 美化（顶部 logo + 5 徽章 + 18 项 TOC）
- 联网_service.py 嵌入 HTML `<title>` 与品牌标识改为 "星尘闪连"

### 🔧 变更
- `MyAppName` 改 "星尘闪连 (Stardust Flash Link)"
- `MyAppPublisher` 改 "星尘闪连"

### 🐛 修复
- EULA 文档新增（详见 v2.0.0）

---

## [v1.3.5] - 2026-09-20

### ✨ 新增
- **GitHub 国内镜像加速**：`_check_github_latest` 和 `_download_installer` 按 `GITHUB_API_MIRRORS` 列表（`None` 主源 + `https://gh-proxy.com` / `ghfast.top` / `mirror.ghproxy.com` 三个镜像）串行 fallback；主源超时/失败时自动尝试镜像，解决国内 `api.github.com` 不可达问题
- **手动触发更新按钮**：Web UI「配置」面板「自动化」card 末尾新增 "🔍 立即检查更新" 和 "⬆️ 立即升级" 按钮，调用 v1.3 已有的 `/api/update/check` 和 `/api/update/install` 端点（零新增 API）

---

## [v1.3.4] - 2026-09-20

### ✨ 新增
- **日志分级筛选**：Web UI「日志」面板顶部新增等级筛选 chip（全部 / INFO / WARN / ERROR）
- 后端 `GET /api/log_tail` 新增可选 `level` query 参数（`info` / `warning` / `error` / `debug` / `critical`），按等级过滤返回行
- 响应体新增 `level_filter` 字段（向后兼容：旧客户端忽略未知字段）

---

## [v1.3.3] - 2026-09-20

### ✨ 新增
- **配置导入/导出**：Web UI「配置」面板新增 "📤 导出配置" 和 "📥 导入配置" 按钮
- 导出：把 `config.json` + `password.txt`（如有）+ `manifest.json` 打包成 `config-export-<时间戳>.zip` 下载
- 导入：上传 zip 后做合法性校验（manifest schema_version、config 字段校验、password 非空），通过后原子写入磁盘并返回 `{need_restart: true}`

### 🔧 变更
- 纯标准库 `zipfile` + `io` 实现，不引入新依赖

---

## [v1.3.2] - 2026-09-20

### 🐛 修复
- **自动升级流程两个关键 bug**：
  1. `_launch_installer` 启动 installer 时未用 `DETACHED_PROCESS` flag —— installer 进程继承父 Python 的 console handle + process group，Python 被 NSSM 杀掉时 installer 被连带杀掉，升级半途而废。修复：用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` 让 installer 完全脱离父进程生命周期
  2. 升级期间 `_set_nssm_appexit("Disabled")` 写了一个 NSSM 非法值（AppExit 合法值只有 `Default | Exit | Success | Failure | Codes`），导致升级后服务无法启动。修复：升级透明策略 —— 不再写 AppExit，保留用户原值

---

## [v1.3.1] - 2026-09-20

### 🐛 修复
- **v1.3 前后端字段没收口**：`_save_config` 在校验前 merge 默认值（兜底），老 config.json 缺 `update_min_free_disk_mb` 等 v1.3 字段时不再报错
- **版本比较 bug**：`_parse_version` 不识别 `-fix` / `-rc1` 等非数字后缀，`_parse_version("1.3-fix")` 与 `1.3` 比较时错误地返回 0（"已是最新"），导致 v1.3 服务无法识别并升级到 `v1.3-fix` / `v1.3.1`；重写解析逻辑，遇非数字后缀追加 sentinel `999`，使 hotfix 版本严格大于同主版本号

---

## [v1.3] - 2026-09-20

### ✨ 新增
- **静默自动升级**：服务后台定期检查 GitHub `/releases/latest` —— 本机版本落后则自动下载安装器、校验 SHA256、备份当前脚本、调 Inno Setup 静默安装、服务自动重启
- Web UI 状态面板顶部新增升级状态横幅（warn / success / error 三态）
- Web UI「配置」面板「自动化」card 新增「启用自动升级」开关与「检查间隔」下拉（6 / 12 / 24 小时）
- Web UI 关于面板新增「查看升级历史」按钮（弹窗显示 `logs/upgrade.log`）
- 后端 `GET /api/update/status` / `POST /api/update/check` / `POST /api/update/install` / `POST /api/update/toggle` / `GET /api/update/history` 5 个端点
- 后台线程 `_auto_update_loop` 周期检查 + 静默触发升级

### 🔧 变更
- 配置面板布局调整：「账户与登录密码」card（账号 + 运营商 + 密码）整体上移到顶部

---

## [v1.2] - 2026-09-20

### ✨ 新增
- **Web UI 重设计为 DeepSeek 风格**：极简、淡蓝/淡紫渐变背景、细腻网格底纹、大圆角、柔和阴影、大字号 KPI、pill 按钮、状态点呼吸动效、顶部细提示条
- **安装器在升级时自动先停服务再覆盖文件**——避免旧 Python 进程持有 `联网_service.py` 句柄导致新版本装不上

### 🐛 修复
- 修复 v1.1 的配置字段前后端没收口问题
- 修复安装器 NSSM AppParameters 引号问题（含空格路径下服务无法启动）

---

## [v1.1] - 2026-09-20

### ✨ 新增
- **安装包内嵌 Python 3.12 运行时**：终端用户无需预装 Python
- **GitHub Actions 自动构建流水线**：push 后自动产出 `.exe` 并发布到 `installer` Release
- **Web UI 现代化重设计**：亮 / 暗主题、KPI 卡片、分段控件、终端日志、Toast

### 🐛 修复
- 修复 `.panel.active` 白屏（动画未推进时永久停在 `opacity:0`，补静态兜底）

---

## [v1.0] - 2026-09-20

### 🎉 首个公开发布版本

- **Windows 校园网自动登录**：NSSM 服务托管、Web UI 配置、周期自检与指数退避、一键安装 / 卸载、可选 Inno Setup 打包
- 适配福建农业职业技术学院校园网认证网关
- MIT 许可证开源
