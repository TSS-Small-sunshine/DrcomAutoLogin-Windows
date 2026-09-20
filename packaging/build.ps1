<#
============================================================
  build.ps1 - Dr.COM 校园网自动登录 - Inno Setup 安装包构建脚本 (v1.3.3)

  为什么从 build.bat 改写为 PowerShell:
    1. cmd 的文件存在性判断（if-exist）在该机器某些 context 下即使文件存在也返回 false
    2. .bat 里内嵌多行 PowerShell（here-string）会被 cmd 逐行当命令执行，报
       "The string is missing the terminator" / ".Length was unexpected at this time"
    3. .bat + UTF-8 BOM + chcp 的中文回显偶发 mojibake
  PowerShell 版本: Windows PowerShell 5.1（powershell.exe，不是 pwsh）
  本文件编码: UTF-8 with BOM（PowerShell 5.1 读非 ASCII 需要 BOM）
  日志: packaging\build.log（Start-Transcript 全程记录，UTF-8）
============================================================
#>
param(
    [switch]$Elevated,
    [Parameter(ValueFromRemainingArguments = $true)]
    $Rest
)

$ErrorActionPreference = 'Stop'

# ============================================================
#   常量
# ============================================================
$AppVersionText  = 'v1.3.3'
$LogPath         = $null   # 在路径解析后赋值
$LangUrl         = 'https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl'
$LangManualUrl   = 'https://github.com/jrsoftware/issrc/tree/main/Files/Languages'
$NssmUrl         = 'https://nssm.cc/release/nssm-2.24.zip'
$InnoSetupUrl    = 'https://jrsoftware.org/isdl.php'
$NssmManualUrl   = 'https://nssm.cc/download'
# 内嵌 Python（官方 Windows embeddable package）候选下载地址，按顺序依次尝试，第一个成功的即使用
$PythonEmbedUrls = @(
    'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip',
    'https://www.python.org/ftp/python/3.12.9/python-3.12.9-embed-amd64.zip',
    'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip'
)
$PythonManualUrl = 'https://www.python.org/downloads/windows/'

# ============================================================
#   步骤 0 - 自身提权
#   注意: 提权判断必须放在 Start-Transcript 之前，
#         否则父子两个 powershell 进程会抢同一个 build.log
# ============================================================
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$ElevatedSession = $Elevated.IsPresent -or ($env:DRCOM_BUILD_ELEVATED -eq '1')
# 双击 build.bat 时 cmd 命令行形如: cmd /c ""D:\...\build.bat" "
# 在已有终端窗口里手敲 build.bat 时命令行不含 /c —— 那种情况窗口不会消失，不用 pause
$WindowWillClose = $ElevatedSession -or ($env:CMDCMDLINE -match '(?i)/c')

if (-not $IsAdmin) {
    Write-Host '[INFO] 当前不是管理员，正在通过 UAC 重新以管理员身份启动...'
    if ([string]::IsNullOrEmpty($PSCommandPath)) {
        Write-Host '[ERROR] 无法取得脚本完整路径 ($PSCommandPath 为空)'
        Write-Host '        请右键 build.bat -> 以管理员身份运行'
        Read-Host '按回车关闭'
        exit 1
    }
    $env:DRCOM_BUILD_ELEVATED = '1'
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Elevated'
        ) | Out-Null
    } catch {
        Write-Host ('[ERROR] 提权失败: ' + $_.Exception.Message)
        Write-Host '        请右键 build.bat -> 以管理员身份运行'
        Read-Host '按回车关闭'
        exit 1
    }
    Write-Host '[INFO] 已在新窗口中以管理员身份启动，本窗口关闭。'
    exit 0
}

# ============================================================
#   步骤 1 - UTF-8 控制台
# ============================================================
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
try { chcp 65001 | Out-Null } catch { }
# Invoke-WebRequest 下载 GitHub 需要 TLS 1.2（老系统默认可能只有 TLS 1.0）
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# ============================================================
#   步骤 2 - 路径解析
# ============================================================
$PackagingDir = $PSScriptRoot
if ([string]::IsNullOrEmpty($PackagingDir)) {
    $PackagingDir = Split-Path -Path $PSCommandPath -Parent
}
$ProjectDir   = Split-Path -Path $PackagingDir -Parent
$OutputDir    = Join-Path $PackagingDir 'output'
$IssPath      = Join-Path $PackagingDir 'setup.iss'
$LogPath      = Join-Path $PackagingDir 'build.log'
$NssmTarget   = Join-Path $ProjectDir 'tools\nssm.exe'
$PythonDir    = Join-Path $ProjectDir 'python'

# ============================================================
#   步骤 3 - 全程 transcript 日志
#   （已有 transcript 在运行时 Start-Transcript 会失败 -> 降级为警告，不阻塞构建）
# ============================================================
$script:TranscriptStarted = $false
try {
    Start-Transcript -Path $LogPath -Force | Out-Null
    $script:TranscriptStarted = $true
} catch {
    Write-Host ('[WARN] 无法启动 transcript 日志: ' + $_.Exception.Message)
}

# ============================================================
#   输出辅助函数
# ============================================================
function Write-Info {
    param([string]$Message)
    Write-Host ('[INFO] ' + $Message)
}
function Write-Warn {
    param([string]$Message)
    Write-Host ('[WARN] ' + $Message) -ForegroundColor Yellow
}
function Write-Err {
    param([string]$Message)
    Write-Host ('[ERROR] ' + $Message) -ForegroundColor Red
}
function Stop-Build {
    param(
        [string]$Message,
        [string[]]$Hint = @()
    )
    Write-Err $Message
    if ($Hint.Count -gt 0) {
        Write-Host ''
        foreach ($line in $Hint) { Write-Host $line }
    }
    Write-Host ''
    Write-Host ('[INFO] 完整日志: ' + $LogPath)
    $script:ExitCode = 1
    exit 1
}

$script:ExitCode = 1

try {
    # ============================================================
    #   Banner
    # ============================================================
    Write-Host '============================================================'
    Write-Host ('  Dr.COM 校园网自动登录 - 安装包构建 (' + $AppVersionText + ')')
    Write-Host '============================================================'
    Write-Info ('脚本:     ' + $PSCommandPath)
    Write-Info ('打包目录: ' + $PackagingDir)
    Write-Info ('项目目录: ' + $ProjectDir)
    Write-Info ('输出目录: ' + $OutputDir)
    Write-Info ('日志文件: ' + $LogPath)
    Write-Host ''

    # ============================================================
    #   步骤 4 - 定位 ISCC.exe（只用 Powershell 原生 Test-Path）
    # ============================================================
    Write-Info '正在查找 Inno Setup 6 (ISCC.exe) ...'
    $iscc = $null
    $checked = @()

    $candidates = @()
    if ($env:ISCC) { $candidates += $env:ISCC } else { $checked += ('环境变量 ISCC: (未设置)') }
    $candidates += 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
    $candidates += 'C:\Program Files\Inno Setup 6\ISCC.exe'
    $candidates += 'C:\Tools\InnoSetup\ISCC.exe'

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $iscc = $candidate
            break
        }
        $checked += ('路径不存在: ' + $candidate)
    }

    if (-not $iscc) {
        $regKeys = @(
            'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1'
        )
        foreach ($rk in $regKeys) {
            $loc = $null
            try {
                $loc = (Get-ItemProperty -Path $rk -Name 'InstallLocation' -ErrorAction Stop).InstallLocation
            } catch {
                $checked += ('注册表无 InstallLocation: ' + $rk)
            }
            if ($loc) {
                $regIscc = Join-Path $loc 'ISCC.exe'
                if (Test-Path -LiteralPath $regIscc -PathType Leaf) {
                    $iscc = $regIscc
                    break
                }
                $checked += ('注册表 InstallLocation 下无 ISCC.exe: ' + $regIscc)
            }
        }
    }

    if (-not $iscc) {
        $hint = @('        已检查以下位置，均未找到 ISCC.exe:')
        foreach ($line in $checked) { $hint += ('          - ' + $line) }
        $hint += ''
        $hint += '        解决办法（任选一种）:'
        $hint += '          1) 安装 Inno Setup 6: ' + $InnoSetupUrl
        $hint += '          2) 已装但路径不同 -> 设置环境变量后重跑，例如:'
        $hint += '             $env:ISCC = "D:\InnoSetup6\ISCC.exe"; .\build.ps1'
        $hint += '             （或 setx ISCC "D:\InnoSetup6\ISCC.exe" 后重开窗口）'
        Stop-Build -Message '未检测到 Inno Setup 6 (ISCC.exe)' -Hint $hint
    }
    Write-Info ('找到 ISCC: ' + $iscc)

    # ============================================================
    #   步骤 5 - 确保中文语言文件存在
    # ============================================================
    $IsccDir  = Split-Path -Path $iscc -Parent
    $LangDir  = Join-Path $IsccDir 'Languages'
    $LangFile = Join-Path $LangDir 'ChineseSimplified.isl'
    $langNeedDownload = $true

    # 中文语言文件下载镜像（按顺序依次尝试；全部失败则降级为英文界面，不中断构建）
    $LangMirrors = @(
        'https://gh-proxy.com/https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl',
        'https://ghproxy.net/https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl',
        'https://cdn.jsdelivr.net/gh/jrsoftware/issrc@main/Files/Languages/ChineseSimplified.isl',
        'https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl'
    )

    if (Test-Path -LiteralPath $LangFile -PathType Leaf) {
        $langSize = (Get-Item -LiteralPath $LangFile).Length
        if ($langSize -ge 1000) {
            Write-Info ('中文语言文件已存在: ' + $LangFile + ' (' + $langSize + ' 字节)')
            $langNeedDownload = $false
        } else {
            Write-Warn ('中文语言文件损坏（' + $langSize + ' 字节 < 1000），将重新下载: ' + $LangFile)
        }
    } else {
        Write-Warn ('缺少中文语言文件: ' + $LangFile)
    }

    if ($langNeedDownload) {
        if (-not (Test-Path -LiteralPath $LangDir -PathType Container)) {
            try {
                New-Item -ItemType Directory -Path $LangDir -Force | Out-Null
            } catch {
                Stop-Build -Message ('无法创建 Languages 目录: ' + $LangDir) -Hint @('        原因: ' + $_.Exception.Message)
            }
        }
        # 陆续尝试各镜像，成功即跳出；全部失败则走下面的英文降级分支
        $langSize        = 0
        $langFromMirror  = 0
        $langTmp         = $LangFile + '.tmp'
        $mirrorCount     = @($LangMirrors).Count

        for ($mi = 0; $mi -lt $mirrorCount; $mi++) {
            $mirrorNumber = $mi + 1
            $mirrorUrl    = $LangMirrors[$mi]
            $mirrorErr    = $null
            Write-Info ('尝试镜像 ' + $mirrorNumber + '/' + $mirrorCount + ': ' + $mirrorUrl)

            # 每次尝试前清掉上一轮的残留，避免把旧文件当成本次下载结果
            if (Test-Path -LiteralPath $langTmp -PathType Leaf) {
                Remove-Item -LiteralPath $langTmp -Force -ErrorAction SilentlyContinue
            }

            try {
                Invoke-WebRequest -Uri $mirrorUrl -OutFile $langTmp -UseBasicParsing -TimeoutSec 120
            } catch {
                $mirrorErr = $_.Exception.Message
            }

            # 校验 1: 文件存在
            if (-not $mirrorErr) {
                if (-not (Test-Path -LiteralPath $langTmp -PathType Leaf)) {
                    $mirrorErr = '下载后临时文件不存在'
                }
            }
            # 校验 2: 大小合理（.isl 约 20 KB，HTML 错误页通常很小）
            if (-not $mirrorErr) {
                $tmpSize = (Get-Item -LiteralPath $langTmp).Length
                if ($tmpSize -le 1000) {
                    $mirrorErr = ('文件只有 ' + $tmpSize + ' 字节 (<= 1000)，疑似错误页')
                }
            }
            # 校验 3: 内容像 .isl（防代理/网关把 HTML 错误页当 200 返回）
            if (-not $mirrorErr) {
                $head = ''
                try {
                    $tmpBytes = [System.IO.File]::ReadAllBytes($langTmp)
                    $takeLen  = [Math]::Min(200, $tmpBytes.Length)
                    $head     = [System.Text.Encoding]::UTF8.GetString($tmpBytes, 0, $takeLen)
                } catch {
                    $mirrorErr = ('读取临时文件失败: ' + $_.Exception.Message)
                }
                if (-not $mirrorErr) {
                    $looksLikeIsl = $false
                    if ($head -match '(?m)^\s*;')      { $looksLikeIsl = $true }
                    if ($head -match '\[LangOptions\]') { $looksLikeIsl = $true }
                    if ($head -match 'LanguageName')    { $looksLikeIsl = $true }
                    if (-not $looksLikeIsl) {
                        $mirrorErr = '内容不像 .isl（前 200 字节无 ; 注释 / [LangOptions] / LanguageName，疑似 HTML 错误页）'
                    }
                }
            }

            if (-not $mirrorErr) {
                Move-Item -LiteralPath $langTmp -Destination $LangFile -Force
                $langSize       = (Get-Item -LiteralPath $LangFile).Length
                $langFromMirror = $mirrorNumber
                break
            }

            Write-Warn ('镜像 ' + $mirrorNumber + ' 失败: ' + $mirrorErr)
            if (Test-Path -LiteralPath $langTmp -PathType Leaf) {
                Remove-Item -LiteralPath $langTmp -Force -ErrorAction SilentlyContinue
            }
        }

        if ($langFromMirror -gt 0) {
            Write-Info ('中文语言文件已就绪（来自镜像 ' + $langFromMirror + '）: ' + $LangFile + ' (' + $langSize + ' 字节)')
        } else {
            # ------------------------------------------------------------
            # 降级：所有镜像都不可达 -> 用 Inno Setup 自带的英文语言文件兜底，
            #       保证安装包照常产出（只是向导界面为英文），不中断构建
            # ------------------------------------------------------------
            $DefaultIsl = Join-Path $IsccDir 'Default.isl'
            $degradedOk = $false
            if (Test-Path -LiteralPath $DefaultIsl -PathType Leaf) {
                try {
                    Copy-Item -LiteralPath $DefaultIsl -Destination $LangFile -Force
                    if (Test-Path -LiteralPath $LangFile -PathType Leaf) {
                        $langSize   = (Get-Item -LiteralPath $LangFile).Length
                        $degradedOk = $true
                    }
                } catch {
                    $degradedOk = $false
                }
            }

            if ($degradedOk) {
                Write-Warn '============================================================'
                Write-Warn (' 中文语言文件下载失败（' + $mirrorCount + ' 个镜像全部不可达）')
                Write-Warn ' 已降级使用英文界面（复制 Default.isl）'
                Write-Warn ' 安装向导将显示英文，功能完全正常'
                Write-Warn ' 如需中文：手动下载后覆盖'
                Write-Warn ('   ' + $LangFile)
                Write-Warn ('   下载页: ' + $LangManualUrl)
                Write-Warn ('   国内可用: ' + $LangMirrors[0])
                Write-Warn '============================================================'
                Write-Warn '继续构建（不中断）...'
            } else {
                Stop-Build -Message ('中文语言文件下载失败（' + $mirrorCount + ' 个镜像全部不可达），且找不到 Inno Setup 自带的 Default.isl，无法降级') -Hint @(
                    '        请手动下载后放到下面这个位置（文件名必须是 ChineseSimplified.isl）:',
                    '          目标路径: ' + $LangFile,
                    '          下载页:   ' + $LangManualUrl,
                    '          国内可用: ' + $LangMirrors[0],
                    '          直链:     ' + $LangMirrors[$mirrorCount - 1],
                    '        确认网络可访问 github / raw.githubusercontent.com 后重跑本脚本。'
                )
            }
        }
    }

    # ============================================================
    #   步骤 6 - 确保 NSSM 存在
    # ============================================================
    $nssmNeedDownload = $true
    if (Test-Path -LiteralPath $NssmTarget -PathType Leaf) {
        $nssmSize = (Get-Item -LiteralPath $NssmTarget).Length
        if ($nssmSize -gt 100000) {
            Write-Info ('NSSM 已存在，跳过下载: ' + $NssmTarget + ' (' + $nssmSize + ' 字节)')
            $nssmNeedDownload = $false
        } else {
            Write-Warn ('NSSM 文件异常（' + $nssmSize + ' 字节 <= 100000），将重新下载: ' + $NssmTarget)
        }
    } else {
        Write-Warn ('缺少 NSSM: ' + $NssmTarget)
    }

    if ($nssmNeedDownload) {
        $nssmZip = Join-Path $env:TEMP ('nssm-2.24-' + $PID + '.zip')
        $nssmTmp = Join-Path $env:TEMP ('nssm-extract-' + $PID)
        Write-Info ('正在下载 NSSM 2.24: ' + $NssmUrl)
        try {
            Invoke-WebRequest -Uri $NssmUrl -OutFile $nssmZip -UseBasicParsing -TimeoutSec 300
        } catch {
            Stop-Build -Message ('下载 NSSM 失败: ' + $_.Exception.Message) -Hint @(
                '        请手动下载 nssm（2.24）并把 win64\nssm.exe 放到:',
                '          ' + $NssmTarget,
                '        下载页: ' + $NssmManualUrl,
                '        直链:   ' + $NssmUrl
            )
        }
        if (-not (Test-Path -LiteralPath $nssmZip -PathType Leaf)) {
            Stop-Build -Message ('下载后压缩包不存在: ' + $nssmZip)
        }
        Write-Info ('正在解压: ' + $nssmZip)
        try {
            if (-not (Test-Path -LiteralPath $nssmTmp -PathType Container)) {
                New-Item -ItemType Directory -Path $nssmTmp -Force | Out-Null
            }
            Expand-Archive -LiteralPath $nssmZip -DestinationPath $nssmTmp -Force
        } catch {
            Stop-Build -Message ('解压 NSSM 失败: ' + $_.Exception.Message) -Hint @(
                '        压缩包: ' + $nssmZip,
                '        可手动解压后把 nssm-2.24\win64\nssm.exe 复制到: ' + $NssmTarget
            )
        }
        $nssmSrc = Join-Path $nssmTmp 'nssm-2.24\win64\nssm.exe'
        if (-not (Test-Path -LiteralPath $nssmSrc -PathType Leaf)) {
            $found = @(Get-ChildItem -LiteralPath $nssmTmp -Recurse -Filter 'nssm.exe' -ErrorAction SilentlyContinue |
                       Where-Object { $_.FullName -like '*win64*' })
            if ($found.Count -gt 0) { $nssmSrc = $found[0].FullName }
        }
        if (-not (Test-Path -LiteralPath $nssmSrc -PathType Leaf)) {
            Stop-Build -Message '解压后没有找到 win64\nssm.exe' -Hint @(
                '        解压目录: ' + $nssmTmp,
                '        可手动下载后把 win64\nssm.exe 放到: ' + $NssmTarget
            )
        }
        $toolsDir = Split-Path -Path $NssmTarget -Parent
        if (-not (Test-Path -LiteralPath $toolsDir -PathType Container)) {
            New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
        }
        Copy-Item -LiteralPath $nssmSrc -Destination $NssmTarget -Force
        if (-not (Test-Path -LiteralPath $NssmTarget -PathType Leaf)) {
            Stop-Build -Message ('复制 nssm.exe 失败: ' + $NssmTarget)
        }
        Write-Info ('NSSM 已安装到: ' + $NssmTarget + ' (' + (Get-Item -LiteralPath $NssmTarget).Length + ' 字节)')
        # 清理临时文件（失败不影响构建）
        Remove-Item -LiteralPath $nssmZip -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $nssmTmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    # ============================================================
    #   步骤 7 - 准备内嵌 Python
    #   setup.iss 的 [Files] 段引用了 ..\python\*（DestDir: {app}\python），
    #   所以必须在调用 ISCC 之前把官方 embeddable 包解压到 <仓库根>\python\，
    #   否则源文件无匹配，ISCC 会直接报错。
    # ============================================================
    $pyExe              = Join-Path $PythonDir 'python.exe'
    $pythonNeedDownload = $true

    if (Test-Path -LiteralPath $pyExe -PathType Leaf) {
        Write-Info ('内嵌 Python 已存在，跳过下载: ' + $pyExe + ' (' + (Get-Item -LiteralPath $pyExe).Length + ' 字节)')
        $pythonNeedDownload = $false
    } else {
        Write-Warn ('缺少内嵌 Python: ' + $PythonDir)
    }

    if ($pythonNeedDownload) {
        $pyZip      = Join-Path $env:TEMP ('python-embed-' + $PID + '.zip')
        $pyUrlCount = @($PythonEmbedUrls).Count
        $pyFromUrl  = 0
        $pyVersion  = '未知'

        # 依次尝试各候选地址，第一个通过校验的即使用
        for ($pi = 0; $pi -lt $pyUrlCount; $pi++) {
            $pyNumber = $pi + 1
            $pyUrl    = $PythonEmbedUrls[$pi]
            $pyErr    = $null
            Write-Info ('尝试内嵌 Python 下载地址 ' + $pyNumber + '/' + $pyUrlCount + ': ' + $pyUrl)

            # 每次尝试前清掉上一轮的残留，避免把旧文件当成本次下载结果
            if (Test-Path -LiteralPath $pyZip -PathType Leaf) {
                Remove-Item -LiteralPath $pyZip -Force -ErrorAction SilentlyContinue
            }

            try {
                Invoke-WebRequest -Uri $pyUrl -OutFile $pyZip -UseBasicParsing -TimeoutSec 300
            } catch {
                $pyErr = $_.Exception.Message
            }
            # 校验 1: 文件存在
            if (-not $pyErr) {
                if (-not (Test-Path -LiteralPath $pyZip -PathType Leaf)) {
                    $pyErr = '下载后压缩包不存在'
                }
            }
            # 校验 2: 大小合理（embed-amd64 约 10 MB，代理/网关错误页远小于此）
            if (-not $pyErr) {
                $pyZipSize = (Get-Item -LiteralPath $pyZip).Length
                if ($pyZipSize -le 5000000) {
                    $pyErr = ('压缩包只有 ' + $pyZipSize + ' 字节 (<= 5000000)，疑似错误页')
                }
            }

            if (-not $pyErr) {
                if ($pyUrl -match 'python-(\d+\.\d+\.\d+)-embed') { $pyVersion = $Matches[1] }
                $pyFromUrl = $pyNumber
                break
            }

            Write-Warn ('下载地址 ' + $pyNumber + ' 失败: ' + $pyErr)
            if (Test-Path -LiteralPath $pyZip -PathType Leaf) {
                Remove-Item -LiteralPath $pyZip -Force -ErrorAction SilentlyContinue
            }
        }

        if ($pyFromUrl -eq 0) {
            $pyHint = @('        已尝试以下候选地址，均不可用:')
            foreach ($pyUrl in $PythonEmbedUrls) { $pyHint += ('          - ' + $pyUrl) }
            $pyHint += ''
            $pyHint += '        解决办法: 手动下载官方 Windows embeddable package (64-bit) 并解压，'
            $pyHint += ('                  把解压出的全部文件放到: ' + $PythonDir)
            $pyHint += '                  解压后该目录下应当能看到 python.exe'
            $pyHint += ('        下载页: ' + $PythonManualUrl)
            Stop-Build -Message ('内嵌 Python 下载失败（' + $pyUrlCount + ' 个候选地址均不可用）') -Hint $pyHint
        }

        Write-Info ('正在解压内嵌 Python 到: ' + $PythonDir)
        try {
            if (-not (Test-Path -LiteralPath $PythonDir -PathType Container)) {
                New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null
            }
            Expand-Archive -LiteralPath $pyZip -DestinationPath $PythonDir -Force
        } catch {
            Stop-Build -Message ('解压内嵌 Python 失败: ' + $_.Exception.Message) -Hint @(
                '        压缩包: ' + $pyZip,
                ('        可手动解压后把全部文件放到: ' + $PythonDir)
            )
        }

        # 校验解压结果：setup.iss 依赖 python\python.exe
        if (-not (Test-Path -LiteralPath $pyExe -PathType Leaf)) {
            Stop-Build -Message ('解压后没有找到 python.exe: ' + $pyExe) -Hint @(
                ('        解压目录: ' + $PythonDir),
                ('        可手动下载官方 embeddable 包后解压到该目录: ' + $PythonManualUrl)
            )
        }

        $pyFileCount = @(Get-ChildItem -LiteralPath $PythonDir -Recurse -File -ErrorAction SilentlyContinue).Count
        Write-Info ('内嵌 Python 版本: ' + $pyVersion + ' (' + $PythonEmbedUrls[$pyFromUrl - 1] + ')')
        Write-Info ('内嵌 Python 文件数: ' + $pyFileCount)

        # 清理临时压缩包（失败不影响构建）
        Remove-Item -LiteralPath $pyZip -Force -ErrorAction SilentlyContinue
    }

    # ============================================================
    #   步骤 8 - 准备 output 目录
    # ============================================================
    if (-not (Test-Path -LiteralPath $OutputDir -PathType Container)) {
        Write-Info ('创建输出目录: ' + $OutputDir)
        New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
    } else {
        Write-Info ('输出目录已存在: ' + $OutputDir)
    }
    if (-not (Test-Path -LiteralPath $OutputDir -PathType Container)) {
        Stop-Build -Message ('输出目录创建失败: ' + $OutputDir)
    }

    # ============================================================
    #   步骤 9 - 检查输入文件（只报告，不修改）
    # ============================================================
    if (-not (Test-Path -LiteralPath $IssPath -PathType Leaf)) {
        Stop-Build -Message ('找不到 Inno Setup 脚本: ' + $IssPath) -Hint @(
            '        期望的打包目录: ' + $PackagingDir,
            '        setup.iss 必须和 build.ps1 放在同一个目录。'
        )
    }
    Write-Info ('输入脚本: ' + $IssPath + ' (' + (Get-Item -LiteralPath $IssPath).Length + ' 字节)')

    # ============================================================
    #   步骤 10 - 调用 ISCC.exe 编译
    # ============================================================
    Write-Host ''
    Write-Info ('开始编译: ' + $IssPath)
    $rc = 0
    $prevEap = $ErrorActionPreference
    # ISCC 会把进度/警告写 stderr，临时放开 Stop 以免被当成致命错误
    $ErrorActionPreference = 'Continue'
    try {
        & $iscc $IssPath
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($null -eq $rc) { $rc = 0 }
    Write-Host ''
    Write-Info ('ISCC 退出码: ' + $rc)

    # ============================================================
    #   步骤 11 - 结果校验
    # ============================================================
    if ($rc -ne 0) {
        Stop-Build -Message ('ISCC.exe 编译失败 (exit code ' + $rc + ')') -Hint @(
            '        请看上方 ISCC 输出的具体错误行（通常是 setup.iss 语法或源文件缺失）。',
            '        本次使用的脚本: ' + $IssPath
        )
    }

    $built = @(Get-ChildItem -LiteralPath $OutputDir -Filter '*.exe' -File -ErrorAction SilentlyContinue)
    if ($built.Count -eq 0) {
        Stop-Build -Message 'ISCC 返回成功，但输出目录里没有生成任何 .exe' -Hint @(
            '        输出目录: ' + $OutputDir,
            '        请检查 setup.iss 的 OutputDir / OutputBaseFilename 设置。'
        )
    }

    Write-Host '============================================================'
    Write-Info '构建完成!'
    foreach ($f in $built) {
        if (-not (Test-Path -LiteralPath $f.FullName -PathType Leaf)) {
            Stop-Build -Message ('产物校验失败，文件不存在: ' + $f.FullName)
        }
        if ($f.Length -le 0) {
            Stop-Build -Message ('产物大小为 0 字节，构建未真正成功: ' + $f.FullName)
        }
        $mb = [math]::Round($f.Length / 1MB, 2)
        Write-Info ('安装包路径: ' + $f.FullName)
        Write-Info ('  文件大小: ' + $f.Length + ' 字节 (' + $mb + ' MB)')
    }
    Write-Host '============================================================'
    Write-Host ''
    Write-Host '双击安装包:'
    foreach ($f in $built) { Write-Host ('  ' + $f.FullName) }
    Write-Host ''
    Write-Info ('完整日志: ' + $LogPath)

    $script:ExitCode = 0
} catch {
    Write-Err ('构建过程中出现未处理的异常: ' + $_.Exception.Message)
    if ($_.ScriptStackTrace) { Write-Host $_.ScriptStackTrace }
    Write-Host ''
    Write-Host ('[INFO] 完整日志: ' + $LogPath)
    $script:ExitCode = 1
} finally {
    if ($script:TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch { }
    }
    if ($WindowWillClose) {
        Write-Host ''
        if ($script:ExitCode -eq 0) { Write-Host '[INFO] 按回车关闭窗口' } else { Write-Host '[ERROR] 构建失败，请复制上面的错误信息 / 日志文件反馈' }
        Read-Host '按回车关闭' | Out-Null
    }
}

exit $script:ExitCode
