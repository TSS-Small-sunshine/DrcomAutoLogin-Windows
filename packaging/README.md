# 星尘闪连 (Stardust Flash Link) — Dr.COM 校园网自动登录 - 安装包说明

本目录用于**开发者构建 Inno Setup EXE 安装包**。

## 开发者：构建安装包

1. 双击运行 `build.bat`
2. 等待脚本完成（首次会下载 Inno Setup 6 与 NSSM）
3. 构建产物：`output\StardustFlashLink-Setup-v2.0.1.exe`

`build.bat` 步骤：
- 自提升为管理员
- 检查并安装 Inno Setup 6（缺失则静默下载）
- 检查 `..\tools\nssm.exe`（缺失则下载 NSSM 2.24 win64）
- 调用 `ISCC.exe setup.iss` 编译生成 EXE

也可以让 **GitHub Actions** 自动构建：推送代码到 `main` 即触发，产物在 Actions 的 Artifacts 中，并自动发布到 tag `installer` 的 Release（见仓库根 `README.md` 的「自动构建（GitHub Actions）」章节）。

## 终端用户：安装 / 卸载

**安装**：双击 `StardustFlashLink-Setup-v2.0.1.exe`，按向导提示操作。
- 默认安装到 `C:\Program Files\DrcomAutoLogin\`
- 自动注册 Windows 服务 `DrcomAutoLogin`
- 启动 Web UI: 访问 `http://127.0.0.1:8848`
- 配置文件：`C:\Program Files\DrcomAutoLogin\config.json`
- 密码文件：`C:\Program Files\DrcomAutoLogin\password.txt`

**卸载**：控制面板 → 程序与功能 → 星尘闪连 (Stardust Flash Link) → 卸载。
卸载会自动停止并移除 Windows 服务。

## 目录结构

```
packaging/
├── setup.iss              # Inno Setup 6 脚本
├── build.bat              # 一键构建脚本
├── LICENSE.txt            # MIT 许可证
├── config.json.template   # 默认配置（首次安装拷贝为 config.json）
├── password.txt.template  # 密码占位（首次安装拷贝为 password.txt）
└── README.md              # 本文件
```

构建完成后，`output\` 目录包含生成的安装包。

## 已知限制

- 需要 Windows 10+（依赖 `tar.exe` 解压 NSSM）
- Inno Setup 6.x 静默安装命令行参数在不同小版本可能变化
- 升级安装会保留用户已有的 `config.json` 与 `password.txt`
- 卸载会清理 `logs\` 与 `tools\`，但保留 `config.json` 与 `password.txt`
