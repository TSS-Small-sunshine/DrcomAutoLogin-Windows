<p align="center">
  <img src="packaging/branding/app.ico" alt="Logo" width="80"/>
</p>

<h1 align="center">星尘闪连</h1>

<p align="center">
  <b>Stardust Flash Link</b> · Windows 校园网自动登录
</p>

<p align="center">
  <a href="https://github.com/TSS-Small-sunshine/StardustFlashLink/releases/latest"><img src="https://img.shields.io/github/v/release/TSS-Small-sunshine/StardustFlashLink?style=flat-square&label=Release&color=blue" alt="Release"/></a>
  <a href="https://github.com/TSS-Small-sunshine/StardustFlashLink/blob/main/LICENSE"><img src="https://img.shields.io/github/license/TSS-Small-sunshine/StardustFlashLink?style=flat-square" alt="License"/></a>
  <a href="https://github.com/TSS-Small-sunshine/StardustFlashLink/stargazers"><img src="https://img.shields.io/github/stars/TSS-Small-sunshine/StardustFlashLink?style=flat-square" alt="Stars"/></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/platform-Windows%2010%2F11-blue?style=flat-square" alt="Platform"/></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/Python-3.12%20embedded-blueviolet?style=flat-square&logo=python&logoColor=white" alt="Python 3.12 embedded"/></a>
</p>

<p align="center">
  Windows 校园网认证网关自动登录工具：开机自启 + 周期自检 + 智能离线识别 + Web UI 图形化配置。
</p>

## 📑 目录

- [⚠️ 适用范围声明](#适用范围声明请先读这里)
- [✨ 功能特性](#功能特性)
- [📸 截图](#截图)
- [🏷 项目状态](#项目状态)
- [🛠 技术栈](#技术栈)
- [🧩 架构说明](#架构说明)
- [🔐 认证协议](#认证协议)
- [📂 目录结构](#目录结构)
- [🚀 快速开始](#快速开始)
- [🤖 自动构建（GitHub Actions）](#自动构建github-actions)
- [🖥 Web UI 说明](#web-ui-说明)
- [⚙️ 配置文件说明](#配置文件说明)
- [❓ 常见问题](#常见问题)
- [🔒 安全说明](#安全说明)
- [⚖️ 免责声明](#免责声明)
- [📜 许可证](#许可证)
- [📚 文档索引](#文档索引)
- [📝 版本记录](#版本记录)

---

## ⚠️ 适用范围声明（请先读这里）

> **❗ 本项目全部实测验证均在「福建农业职业技术学院」校园网环境下完成。**
>
> 下面这些默认值都是**在该校环境实测**得出的，不是通用值：
>
> - 默认认证网关 `172.16.80.3`
> - 认证端口：`80`（在线状态探测） / `801`（登录提交）
> - 运营商账号后缀规则：不带后缀（校园网）/ `@yd`（移动）/ `@dx`（电信）/ `@lt`（联通）
> - 登录接口路径 `/eportal/portal/login`、在线查询接口路径 `/drcom/chkstatus`
>
> **其它学校的网关地址、端口、接口路径、参数规则大概率不一样。**
> 换学校请自行抓包 / 查认证页面源码确认后修改 `config.json`，**本项目不保证可用**。
> 若接口路径不同，还需要修改 `联网_service.py` 中的接口地址。

---

## 功能特性

- **开机自动登录**：以 Windows 系统服务（NSSM）托管，系统启动时自动触发一次认证
- **Web UI 配置界面**：浏览器打开 `http://127.0.0.1:8848`，不用改源码、不用记配置项就能改账号 / 密码 / 间隔
- **周期自检 + 智能离线识别**：定时检测在线状态；不在校园网时静默跳过，失败后指数退避 `5 → 10 → 20 → 40 → 60` 分钟封顶
- **密码与代码分离**：密码只存在 `password.txt` 中，源码内**零硬编码凭据**
- **零第三方 Python 依赖**：全部使用 Python 标准库，受限网络环境也能跑
- **一键安装 / 一键卸载**：`install.bat` / `uninstall.bat` 全程自动
- **可选打包安装程序**：用 Inno Setup 6 打成 `.exe` 安装向导（`packaging\build.bat`）

---

## 📸 截图

> ⚠️ 截图占位 — 用户后续会提供背景图后补上。

- 安装向导
- Web UI 主界面
- 配置面板

<!-- 后续会加入：
<p align="center">
  <img src="docs/screenshot-install.png" alt="安装向导" width="600"/>
  <img src="docs/screenshot-webui.png" alt="Web UI" width="600"/>
</p>
-->

---

## 🏷 项目状态

**当前版本**：v2.0.0（2026-09-20） · **状态**：🟢 积极维护

[最新 Release](https://github.com/TSS-Small-sunshine/StardustFlashLink/releases/latest) ·
[更新日志](https://github.com/TSS-Small-sunshine/StardustFlashLink/releases) ·
[问题反馈](https://github.com/TSS-Small-sunshine/StardustFlashLink/issues)

---

## 技术栈

| 分类 | 技术 / 版本 | 说明 |
| --- | --- | --- |
| 运行环境 | Windows 10 / 11 x64（**安装包已内嵌 Python，无需预装**） | 安装包自带 CPython embeddable 运行时到 `python\`，目标机**不需要**任何 Python |
| 从源码运行 | **Python 3**（实测 3.14） | 仅「源码方式」（`install.bat` / 直接跑脚本）才需要自备 Python：需 `python.exe` 在 PATH 中，或装在 `C:\Python314\` |
| 依赖 | **Python 标准库，零第三方包** | `urllib` / `json` / `socket` / `http.server` / `threading` / `logging` |
| Web UI | `http.server`（stdlib）+ 内联 HTML / CSS / JS | 单文件内嵌页面，**无 CDN、无外部资源**，离线可用 |
| 服务托管 | **NSSM 2.24** | 由 `install.bat` 自动下载到 `tools\nssm.exe`，**不入库** |
| 打包（可选） | **Inno Setup 6** | `packaging\build.bat` 一键构建，产物在 `packaging\output\` |

> **不依赖**：Node.js、pip 包、外部数据库、任何在线 CDN。

---

## 架构说明

### 内部模块（`联网_service.py`）

| 区块 | 关键符号 | 职责 |
| --- | --- | --- |
| 常量与路径 | `VERSION`、`DEFAULT_CONFIG`、`BASE_DIR`、`CONFIG_FILE`、`PASSWORD_FILE`、`LOG_FILE` | 版本号、默认配置、文件位置解析（全部相对脚本目录） |
| 配置读写 | `_load_config`、`_validate_config`、`_save_config` | 读 `config.json`，缺失/损坏时自动生成默认值并校验合法性 |
| 密码管理 | `_load_password_from_disk`、`_get_password`、`_save_password_to_disk` | 从 `password.txt` 读取密码，仅存内存 + 文件，**不写日志、不回传 API** |
| 运行状态 | `STATE`、`BACKOFF`、`_set_state`、`_snapshot_state`、`_set_backoff`、`_reset_backoff` | 线程间共享状态（带锁）与退避计数 |
| 网络探测 | `wait_network`、`discover_network` | 等网关可达；获取用于登录表单的本机 IP / MAC |
| 认证协议 | `is_online`、`login` | 查询在线状态（chkstatus）、提交登录（eportal） |
| 调度 | `run_once`、`_startup_trigger`、`run_periodic` | 一次完整「等网络 → 查在线 → 登录」，以及启动触发 / 周期触发 |
| Web API | `api_get_status`、`api_get_config`、`api_post_config`、`api_post_password`、`api_post_login`、`api_get_log_tail`、`api_get_log_download`、`api_get_about`、`api_post_restart` | 供页面调用的 JSON 接口 |
| HTTP 服务 | `_Handler`(`BaseHTTPRequestHandler`)、`main()` | 静态页面 + API 路由，主线程阻塞在 `serve_forever()` |

### 线程模型

```
Windows 服务（NSSM）启动
        │
        ├── 线程 A：startup-trigger  ── 等待 3 秒 ──► run_once("startup")
        ├── 线程 B：periodic-check   ── 按自检间隔 ─► run_once("periodic")
        └── 主线程：HTTP Server 监听 127.0.0.1:8848
                        ▲
                        └── 浏览器 Web UI / 手动「立即登录」 ──► run_once("manual")
```

### 一次检查的执行流程

```
run_once(原因)
   │
   ├─ 1. 读取 config.json（每次实时读，改配置无需重启）
   ├─ 2. wait_network：每 2 秒探测 网关:端口，最长 network_wait_timeout_sec 秒
   │         └─ 超时不可达 → 判定「不在校园网」→ 静默返回，不计失败
   ├─ 3. is_online：GET /drcom/chkstatus 查在线状态
   │         ├─ 已在线   → 重置退避，结束
   │         └─ 未在线   → 继续
   └─ 4. login：POST 账号+密码 到 /eportal/portal/login
             ├─ 成功 → 记录 last_login_at，重置退避
             └─ 失败 → 记录 ERROR，进入指数退避
```

### 状态与退避

| 项 | 说明 |
| --- | --- |
| 退避档位 | `5 → 10 → 20 → 40 → 60` 分钟（`BACKOFF_LEVELS`），60 分钟封顶 |
| 触发条件 | 登录失败或连续检测异常 |
| 重置条件 | 检测到已在线 / 登录成功 |
| 实际等待 | `max(自检间隔, 剩余退避时间)`，Web UI「状态」页显示「下次检查」倒计时 |
| 离线静默 | 网关完全不可达时视为「不在校园网」，不刷错误、不拉长退避 |

### 数据与持久化

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 配置 | `<脚本目录>\config.json` | 缺失时自动生成默认值 |
| 密码 | `<脚本目录>\password.txt` | UTF-8 单行纯文本，需自行创建 |
| 业务日志 | `<脚本目录>\logs\campus_login.log` | 认证过程日志，持续追加**不自动轮转**，可随时手动清理 |
| 服务输出 | `<脚本目录>\logs\service_stdout.log` / `service_stderr.log` | NSSM 捕获的标准输出 / 错误，1 MB 轮转 |

---

## 认证协议

> 以下接口为**福建农业职业技术学院**实测结果，其它学校请自行确认。

### ① 查询在线状态

```
GET http://{HOST}/drcom/chkstatus?callback=cb&jsVersion=4.X     （端口 80）
```

- 响应为 **JSONP**：`cb({"result":1,"uid":"...","AC":"...","oltime":N,...})`
- `result == 1` → **已在线**；`result == 0` → 未在线

### ② 提交登录

```
GET http://{HOST}:801/eportal/portal/login?callback=dr{随机数}&login_method=1
    &user_account={账号}{后缀}&user_password={密码}
    &wlan_user_ip=..&wlan_user_ipv6=&wlan_user_mac=..
    &wlan_ac_ip=..&wlan_ac_name=..
    &jsVersion=4.1.3&lang=zh-cn&v={随机数}
```

- 响应为 **JSONP**：`dr{随机数}({"result":1,"msg":"...","ret_code":0})`
- `result == 1` → **登录成功**；否则 `msg` 字段为失败原因

### ③ 参数取值优先级（踩过坑的地方）

登录表单里的 `wlan_user_ip` / `wlan_user_mac` / `wlan_ac_ip` / `wlan_ac_name` **不能乱填**，该校门户脚本 `a41.js` 使用如下降级链（依次尝试，取第一个可用值）：

| 参数 | 取值优先级 |
| --- | --- |
| `wlan_user_ip` | 认证页重定向 URL 的 `wlanuserip` → `chkstatus` 响应的 `v46ip` → `ss5` → `v4ip` → `hex16ToString(ss3)` → 本机网卡 IP |
| `wlan_user_mac` | 认证页重定向 URL 的 `mac` → `chkstatus` 响应的 `ss4` → `olmac` → 占位常量 `000000000000` |
| `wlan_ac_ip` / `wlan_ac_name` | 认证页重定向 URL 的 `wlanacip` / `wlancname` |

> **本 Windows 版当前实现的简化版**（`discover_network()`）：
> 从 `chkstatus` 响应取 `v4ip` 作 `wlan_user_ip`，取 `olmac` 作 `wlan_user_mac`（做去 `:` / `-` 并转大写），
> `v4ip` 缺失时回退到 UDP `connect` 探测出的本机网卡 IP；
> `wlan_ac_ip` / `wlan_ac_name` 目前**发送空字符串**（`""`），该校网关对此接受。
> 若你在其它学校环境遇到登录被拒，优先按上表补齐这些参数。

### ④ 关于「重定向」

未认证时，网关通常会**把普通 HTTP 请求 302 重定向到认证页**（认证页 URL 上会带 `wlanuserip` / `mac` / `wlanacip` 等查询参数）。
浏览器里看到的就是那个页面；本项目的做法是**直接按上面的接口规则发请求**，不解析重定向 HTML，因此更稳定、更快。

---

## 目录结构

```
DrcomAutoLogin-Windows/
├── 联网_service.py            # 主程序：认证调度 + Web UI（Python 标准库，单文件）
├── install.bat                # 一键安装：注册并启动 Windows 服务
├── uninstall.bat              # 一键卸载：停止并移除服务
├── README.md                  # 本文件
├── LICENSE                    # MIT 许可证
├── .gitignore                 # 排除凭据 / 运行数据 / 构建产物
├── python/                    # 内嵌 Python 运行时（CI 构建时下载，不随仓库分发）
├── .github/workflows/build-installer.yml  # 自动构建安装程序并发布 Release
└── packaging/                 # 【可选】打包成安装程序
    ├── build.bat              # 构建入口（双击运行）
    ├── build.ps1              # 构建逻辑（自动准备 NSSM / Inno Setup）
    ├── setup.iss              # Inno Setup 6 脚本
    ├── LICENSE.txt            # 安装包内附的许可证
    ├── config.json.template   # 默认配置模板（安装时复制为 config.json）
    ├── password.txt.template  # 密码文件占位模板（安装时复制为 password.txt）
    └── README.md              # 打包与安装包使用说明
```

> 运行时才会生成、**不在仓库中**：`config.json`、`password.txt`、`logs\`、`tools\`（NSSM）、`python\`（CI 构建时下载的内嵌 Python 运行时，打进安装包）、`packaging\output\`。

---

## 快速开始

### 🎯 路径 0：直接下载安装程序（最省事，推荐）

打开 <https://github.com/TSS-Small-sunshine/StardustFlashLink/releases/tag/installer> → 下载 `DrcomAutoLogin-Setup-v*.exe` → 双击安装。

> 安装包**已内嵌 Python 运行时**，目标机无需预先安装 Python。

### 🪜 路径 A：一键安装（源码方式）

> 此方式需要本机已安装 Python 3（安装包方式不需要）。

1. **确认已安装 Python 3**（实测 3.14），命令行执行 `python --version` 能看到版本号
2. **以管理员身份**双击 `install.bat`（脚本会自动请求提权）
3. 脚本会自动完成：
   - 下载 NSSM 2.24 到 `tools\nssm.exe`（已存在则跳过）
   - 注册并启动 Windows 服务 `DrcomAutoLogin`
   - 在桌面和开始菜单创建「Dr.COM 配置」快捷方式
4. 打开浏览器访问 **`http://127.0.0.1:8848`**
5. 在「配置」标签页填入**账号**，选择**运营商后缀**，点「修改密码」设置密码，最后点「💾 保存配置」
6. 回到「状态」标签页点「🔄 立即登录」验证；成功后开机将自动登录

### 📦 路径 B：打包成安装程序（可选）

1. 安装 **Inno Setup 6**（`build.bat` 会检测，缺失时可自动下载安装）
2. 双击运行 `packaging\build.bat`（会自动准备 NSSM 并调用 `ISCC.exe` 编译）
3. 构建产物：`packaging\output\DrcomAutoLogin-Setup-v2.0.0.exe`
4. 把该 `.exe` 发给用户，双击即按向导安装（可勾选「创建桌面快捷方式」「安装后立即启动服务」）

---

## 自动构建（GitHub Actions）

推送代码到 `main` 后，GitHub Actions 会自动在 Windows runner 上用 Inno Setup 构建安装程序：

1. 打开 <https://github.com/TSS-Small-sunshine/StardustFlashLink/actions>
2. 构建完成后，产物在 `Actions → 对应 run → Artifacts → DrcomAutoLogin-Setup`

也可以直接从 Releases 下载（**每次推送自动覆盖更新，链接固定**）：

<https://github.com/TSS-Small-sunshine/StardustFlashLink/releases/tag/installer>

> CI 流程：安装 Inno Setup 6 → 准备中文语言文件 → 下载 NSSM 到 `tools\` → 下载并解压内嵌 Python 到 `python\` → 用 ISCC 编译 `packaging\setup.iss` → 校验产物 → 上传 artifact → 发布到 tag `installer` 的 Release。

---

## Web UI 说明

浏览器打开 `http://127.0.0.1:8848`，共 4 个标签页：

| 标签 | 用途 |
| --- | --- |
| 📊 **状态** | 查看「网关是否可达 / 是否在线 / 上次登录时间与结果 / 下次检查倒计时」；提供「🔄 立即登录」手动触发一次认证 |
| ⚙️ **配置** | 修改守护网关、账号、运营商后缀、自检开关与间隔、网络等待超时、UI 端口；点「💾 保存配置」立即生效（UI 端口需重启服务）。另有「修改密码」按钮单独改密码 |
| 📝 **日志** | 实时查看 `campus_login.log` 尾部，支持关键字过滤、自动滚动、下载完整日志 |
| ℹ️ **关于** | 显示程序版本、服务启动时间与运行时长、配置 / 密码 / 日志文件位置；提供「🔁 重启服务」与「🗑 卸载服务」提示 |

---

## 配置文件说明

`config.json`（与 `联网_service.py` 同目录；缺失或损坏时自动生成默认值）：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `host` | 字符串 | `"172.16.80.3"` | 认证网关地址（**换学校必须改**） |
| `port` | 整数 | `80` | 在线状态探测端口，取值范围 `1-65535` |
| `account` | 字符串 | `""` | 校园网账号（**纯数字**，不含运营商后缀） |
| `suffix` | 字符串 | `""` | 运营商后缀，只能是 `""` / `"@yd"` / `"@dx"` / `"@lt"` |
| `auto_check_enabled` | 布尔 | `true` | 是否启用周期自检 |
| `auto_check_interval_min` | 整数 | `30` | 自检间隔（分钟），只能是 `5` / `15` / `30` / `60` / `120` |
| `network_wait_timeout_sec` | 整数 | `60` | 每次检查前等待网关可达的最长秒数，取值范围 `10-300` |
| `ui_port` | 整数 | `8848` | Web UI 监听端口，取值范围 `1024-65535`（**修改后需重启服务**） |

> 以上字段在保存时都会经过校验，非法值会被拒绝并给出提示。

---

## 常见问题

**Q1：服务起不来 / 装了但没反应？**
看 `logs\service_stderr.log`。**使用安装包版本时不会出现该问题**（安装包已内嵌 Python 运行时，目标机无需预装 Python）。若用源码方式：最常见原因是 Python 不在 PATH，命令行执行 `python --version` 验证，或把 Python 装到 `C:\Python314\`。装完可用管理员运行 `tools\nssm.exe restart DrcomAutoLogin`。

**Q2：日志一直显示「网关不可达」？**
说明当前不在校园网内，或被分到了别的网段。确认已连上校园网 Wi-Fi / 网线，并核对 `config.json` 的 `host` 是否为所在学校的网关地址（本项目默认值是**福建农业职业技术学院**的 `172.16.80.3`）。

**Q3：提示登录失败？**
依次检查：账号是否为纯数字（后缀单独在 `suffix` 里选）；密码是否正确；运营商后缀是否选对（移动 `@yd` / 电信 `@dx` / 联通 `@lt`，校内网选空）；最后看 `logs\campus_login.log` 里的 `ERROR` 行，失败原因会写在接口返回的 `msg` 里。

**Q4：Web UI 打不开（`http://127.0.0.1:8848`）？**
先确认服务在运行（`tools\nssm.exe status DrcomAutoLogin`）。若 `8848` 被别的程序占用，改 `config.json` 的 `ui_port` 或卸载重装后改端口，然后**重启服务**。注意 Web UI 只监听本机，手机等其它设备访问不了。

**Q5：怎么改密码？**
Web UI →「配置」标签页 → 点「修改密码」→ 输入新密码保存。也可直接编辑 `password.txt`（UTF-8 单行，前后不要空格）后重启服务。

**Q6：怎么卸载？**
- **脚本方式**：管理员运行 `uninstall.bat`。它会停止并移除 Windows 服务、删除桌面与开始菜单快捷方式；随后**询问**是否一并删除 `config.json`、`password.txt` 与 `logs\`（**默认保留**，直接回车即保留）。注意 `tools\`（NSSM）不会被自动删除，需要彻底清理可手动删掉整个目录。
- **安装包方式**：走「控制面板 → 程序和功能」卸载，或开始菜单里的卸载入口。它会停止并移除服务，并清理 `logs\` 与 `tools\`，但**保留** `config.json` 与 `password.txt`。

---

## 安全说明

- **密码只在 `password.txt`**：UTF-8 编码的单行纯文本文件，与其它配置分离；源码中**没有任何硬编码凭据**
- **凭据不入库**：`password.txt` 与 `config.json` 均已写入 `.gitignore`，不会被提交到仓库
- **需自行创建 `password.txt`**：本仓库不含该文件，请在使用前于脚本目录下自行创建并写入密码
- **Web UI 只监听 `127.0.0.1`**：不对局域网 / 外网开放，其它设备无法访问
- **接口不返回密码**：状态接口只返回 `password_status`（`set` / `missing`），永不返回密码原文
- **日志脱敏**：密码不写入任何日志文件；`config.json` 也不保存密码
- **权限建议**：`password.txt` 所在目录建议只授予当前用户访问权限（服务以系统账户运行，注意共享机器的风险）

---

## 免责声明

本项目**仅供个人学习研究，以及为本人自有账号提供正常上网认证便利**。使用时请遵守所在学校的网络管理规定与相关法律法规；请勿用于批量爆破、代他人认证、绕过计费或任何破坏校园网秩序的行为。因使用本项目产生的一切后果由使用者自行承担。

---

## 📜 许可证

本项目以 **MIT 许可证**开源 — 详见 [LICENSE](LICENSE) 文件。

附加：安装包内置了 [用户协议 (EULA)](packaging/branding/EULA.rtf)，安装时会要求勾选同意。

---

## 文档索引

| 文档 | 内容 |
| --- | --- |
| 本 `README.md` | 功能、架构、协议、快速开始、常见问题 |
| [`packaging/README.md`](packaging/README.md) | 开发者构建安装包的步骤、终端用户安装 / 卸载流程、安装包目录结构、已知限制 |

---

## 版本记录

| 版本 | 说明 |
| --- | --- |
| **v1.0** | 首个公开发布版本。Windows 校园网自动登录：NSSM 服务托管、Web UI 配置、周期自检与指数退避、一键安装 / 卸载、可选 Inno Setup 打包。 |
| **v1.1** | 安装包内嵌 Python 3.12 运行时（终端用户无需预装，也不再受架构差异影响）；GitHub Actions 自动构建流水线（push 后自动产出 `.exe` 并发布到 `installer` Release）；Web UI 现代化重设计（亮 / 暗主题、KPI 卡片、分段控件、终端日志、Toast）。修复 `.panel.active` 白屏（动画未推进时永久停在 `opacity:0`，补静态兜底）。 |
| **v1.2** | Web UI 重设计为 **DeepSeek 风格**（极简、淡蓝 / 淡紫渐变背景、细腻网格底纹、大圆角、柔和阴影、大字号 KPI、pill 按钮、状态点呼吸动效、顶部细提示条）；配置页密码字段标注为「账户登录密码」（明确这是校园网认证密码，而非系统登录密码）；安装器在升级时**自动先停服务再覆盖文件**——避免旧 Python 进程持有 `联网_service.py` 句柄导致新版本装不上；统一版本号到 `1.x` 公开版本线（废弃之前并存的 `2.0` / `2.1` 内部代号）。 |
| **v1.3** | **静默自动升级**：服务后台定期检查 GitHub `/releases/latest` —— 本机版本落后则自动下载安装器、校验 SHA256、备份当前脚本、调 Inno Setup 静默安装、服务自动重启；升级全程无需操作，失败立即写日志并显示红色横幅 + 升级历史。Web UI 状态面板顶部新增升级状态横幅；配置面板「自动化」card 新增「启用自动升级」开关与「检查间隔」下拉；关于面板新增「查看升级历史」按钮（弹窗显示 `logs/upgrade.log`）。配置面板布局调整：「账户与登录密码」card（账号 + 运营商 + 密码）整体上移到顶部。 |
| **v1.3.1** | 修复 v1.3 引入的**前后端字段没收口**问题 —— `_save_config` 在校验前 merge 默认值（兜底），老 config.json 缺 `update_min_free_disk_mb` 等 v1.3 字段时不再报错；Web UI 自动升级 card 增加「下载前最小剩余磁盘」输入框。同时修复**版本比较 bug**：`_parse_version` 不识别 `-fix` / `-rc1` 等非数字后缀，`_parse_version("1.3-fix")` 与 `1.3` 比较时错误地返回 0（"已是最新"），导致 v1.3 服务无法识别并升级到 `v1.3-fix` / `v1.3.1`；重写解析逻辑，遇非数字后缀追加 sentinel `999`，使 hotfix 版本严格大于同主版本号。 |
| **v1.3.2** | 修复 v1.3.1 自动升级流程的**两个关键 bug**：(1) `_launch_installer` 启动 installer 时**未用 `DETACHED_PROCESS` flag** —— installer 进程继承父 Python 的 console handle + process group，Python 被 NSSM 杀掉时 installer 被**连带杀掉**，升级半途而废。修复：用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` 让 installer 完全脱离父进程生命周期；`_do_update_now` 步骤 10 改用 `os._exit(0)` 立即退出，不调 `subprocess.run(nssm stop)`（之前那种调用会触发恶性循环）。(2) 升级期间 `_set_nssm_appexit("Disabled")` 写了一个**NSSM 非法值**（AppExit 合法值只有 `Default | Exit | Success | Failure | Codes`），导致升级后服务无法启动（`nssm start` 报 `OpenService 0x424`，`sc start` 报 `Access is denied`）。修复：升级透明策略 —— 不再写 AppExit，保留用户原值，升级完由 `_post_upgrade_startup` 钩子做幂等恢复。 |
| **v1.3.3** | 新增**配置导入/导出**：Web UI「配置」面板新增「📤 导出配置」和「📥 导入配置」两个按钮。导出把 `config.json` + `password.txt`（如有）+ `manifest.json` 打包成 `config-export-<时间戳>.zip` 下载；导入上传 zip 后做合法性校验（manifest schema_version、config 字段校验、password 非空），通过后原子写入磁盘并返回 `{need_restart: true}`，由用户手动点「重启服务」按钮应用新配置。**纯标准库实现**（`zipfile` + `io`），不引入新依赖。`POST /api/config/import` 路由必须放在 JSON 解析**之前**分发（zip 是二进制 body 会被现有 `json.loads` 拦截）。 |
| **v1.3.4** | **日志分级**：Web UI「日志」面板顶部新增等级筛选 chip（全部 / INFO / WARN / ERROR），后端 `GET /api/log_tail` 新增可选 `level` query 参数（`info` / `warning` / `error` / `debug` / `critical`），按等级过滤返回行。响应体新增 `level_filter` 字段（向后兼容：旧客户端忽略未知字段）。无效 level 返回 400。**保持单文件日志**（不拆多个 log 文件），通过前端 chip 切换实现产品级「按等级筛选」体验。 |
| **v1.4.0** | **项目重命名为「星尘闪连 (Stardust Flash Link)」**，沿用 `DrcomAutoLogin` NSSM 服务名、`AppId` 与全部 API 路径（保证旧版可正常卸载 / 升级）。安装包新增 `branding\app.ico`（应用图标，256/128/64/48/32/24/16 多尺寸 ICO）与 `branding\wizard.bmp`（164×314 24-bit 安装器左侧品牌横幅），由 Inno Setup `SetupIconFile` / `WizardImageFile` 引入；Python 脚本头部、L37 install.bat 标题、L31 uninstall.bat 标题、build.ps1 头部与 banner 同步更新。 |
| **v1.3.5** | **GitHub 国内镜像加速** + **手动触发更新**：(1) `_check_github_latest` 和 `_download_installer` 按 `GITHUB_API_MIRRORS` 列表（`None` 主源 + `https://gh-proxy.com` / `ghfast.top` / `mirror.ghproxy.com` 三个镜像）串行 fallback；主源超时/失败时自动尝试镜像，避免国内用户机器上 `api.github.com` 不可达导致自动升级静默失效。(2) Web UI「配置」面板「自动化」card 末尾新增「🔍 立即检查更新」和「⬆️ 立即升级」两个按钮，调用 v1.3 已有的 `/api/update/check` 和 `/api/update/install` 端点（零新增 API），升级按钮带 confirm 确认对话框。 |
| **v2.0.0** | **当前版本。进入 2.0 时代**：从 v1.4 之前的用户自制 logo 改用**纯生成的蔚蓝档案（Blue Archive）经典蓝渐变背景**（`#A0D8EF` → `#3D7DC9` → `#1B3A6B`），164×314 24-bit BMP 嵌入安装器左侧 164×314 横幅；新增 **EULA 用户协议**（`packaging/branding/EULA.rtf`，ISCC `LicenseFile` 原生支持）—— 9 节完整条款（服务范围 / 许可 / 用户责任 / 免责声明 / 隐私 / 第三方组件 / 修改 / 终止 / 法律）。**主版本号 bump** 是视觉 / 法务姿态升级（视觉重做 + 协议引入），技术栈不变。 |

> **版本号说明**：本项目从 `1.x` 进入 `2.x` 公开版本线，**当前版本为 `2.0.0`**。
> - `联网_service.py` 的 `VERSION` 常量（显示在日志与「关于」页）、Inno Setup 安装包版本、安装 / 卸载脚本与构建脚本中的版本字样，**全部是同一个 `2.0.0`**，不再存在多套并存的编号；
> - 历史上曾短暂并存过 `2.0` / `2.1` 内部代号（由「命令行脚本 → Web UI 版」的迭代历史沿用而来），该套编号已废弃；
> - **GitHub Release 标签 `v1.0`** 是本项目的**首次公开发布**记录，属于历史事实，保持不变；
> - 后续公开发布在 `2.x` 线上递增（`2.0.0` → `2.0.1` → `2.1` → …）。
