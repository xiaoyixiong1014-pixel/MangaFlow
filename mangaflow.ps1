#requires -Version 5.1
# =============================================================================
# 文件名: mangaflow.ps1
# 说明: MangaFlow 项目 Windows PowerShell 一键启停脚本
#
# 管理的服务:
#   - PostgreSQL 16      Windows 服务 postgresql-x64-16 (localhost:5432)
#   - backend (FastAPI)  uvicorn, 首选端口 8300, venv 位于 backend\.venv
#   （端口说明: 本机 8000 被其他项目长期占用, MangaFlow 后端默认使用 8300）
#   - web     (Vite)     npm run dev, 首选端口 5173, 目录 mangaflow-web
#   - admin   (Vite)     npm run dev, 首选端口 5174, 目录 mangaflow-admin-web
#
# 端口自动回退: 启动时若首选端口被"外部进程"占用, 自动向后扫描第一个空闲
#   端口启动 (最多 +20); 实际端口写入 .run\<name>.port, 并通过环境变量
#   MANGAFLOW_API_PORT 注入前端 (两个 vite.config.ts 的 /api 代理读取该变量,
#   未设置时回退 8300), 保证前后端联通。首选端口被"本项目进程"占用时视为
#   已在运行, 幂等跳过。
#
# 用法:
#   直接右键"使用 PowerShell 运行"(不带参数): 默认执行 start, 启动完成后
#   暂停等待按 Enter 才关闭窗口, 便于查看启动结果 (不会一闪而过)。
#   .\mangaflow.ps1 start|stop|status|restart
#     start   : 检查/启动 PostgreSQL -> 幂等初始化数据库 -> 启动三服务
#               (为 backend/web/admin 各弹出一个独立控制台窗口, 实时显示输出)
#     stop    : 停止本脚本启动的 backend/web/admin (保留 PostgreSQL 运行)
#     status  : 表格显示各服务状态 (含后端 HTTP 探活)
#     restart : stop + start
#
# 运行时文件 (位于项目根 .run\ 目录, 可随时整体删除):
#   .run\<name>.pid      各服务窗口宿主进程 (powershell.exe) PID
#   .run\<name>.port     各服务实际使用的端口 (端口漂移时与首选端口不同)
#   (旧版 *.out.log / *.err.log 已不再写入, 服务输出直接显示在各自窗口中,
#    历史日志文件可手动删除)
#
# 启动机制说明: 通过 WMI (Win32_Process.Create) 为每个服务创建一个独立可见
#   的控制台窗口 (powershell.exe -NoExit), 其父进程为 WmiPrvSE 服务, 与调用方
#   完全解耦——因此从 IDE 任务/CI/计划任务等 Job Object (kill-on-close) 环境
#   调用本脚本时, 服务窗口不会被连带清理; 在窗口内 Ctrl+C 或直接关闭窗口
#   即可停止对应服务, 也可用本脚本 stop 统一停止。
#
# 兼容性: PowerShell 5.1+; 保存为 UTF-8 with BOM 以正确显示中文
# =============================================================================
param(
    [Parameter(Position = 0, HelpMessage = '要执行的命令: start/stop/status/restart (缺省为 start)')]
    [string]$Command = 'start'
)

# --- 全局设置 ---------------------------------------------------------------
# 让控制台正确输出中文 (无控制台句柄时忽略失败)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 项目根目录 (脚本所在目录)
$Root = $PSScriptRoot
# 运行时文件目录 (PID 与日志)
$RunDir = Join-Path $Root '.run'
# 确保运行时目录存在
if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir | Out-Null }

# 受管服务定义: Name=服务名; Port=首选端口; TitleFormat=弹出窗口标题模板;
#               PidFile=PID 文件路径; PortFile=实际端口记录文件路径
$Services = @(
    @{ Name = 'backend'; Port = 8300; TitleFormat = 'MangaFlow backend ({0})'; PidFile = (Join-Path $RunDir 'backend.pid'); PortFile = (Join-Path $RunDir 'backend.port') },
    @{ Name = 'web';     Port = 5173; TitleFormat = 'MangaFlow web ({0})';     PidFile = (Join-Path $RunDir 'web.pid');     PortFile = (Join-Path $RunDir 'web.port') },
    @{ Name = 'admin';   Port = 5174; TitleFormat = 'MangaFlow admin ({0})';   PidFile = (Join-Path $RunDir 'admin.pid');   PortFile = (Join-Path $RunDir 'admin.port') }
)

# =============================================================================
# 函数: Write-Usage
# 说明: 打印脚本用法说明
# 参数: 无
# 返回: 无 (仅输出文本)
# 异常: 无
# =============================================================================
function Write-Usage {
    Write-Host '用法: .\mangaflow.ps1 <command>'
    Write-Host ''
    Write-Host '命令:'
    Write-Host '  start    检查/启动 PostgreSQL -> 幂等初始化数据库 -> 启动 backend/web/admin'
    Write-Host '  stop     停止本脚本启动的 backend/web/admin (保留 PostgreSQL)'
    Write-Host '  status   查看各服务运行状态'
    Write-Host '  restart  重启 (等同 stop + start)'
    Write-Host ''
    Write-Host '说明:'
    Write-Host '  - start 会为 backend/web/admin 各弹出一个独立窗口, 实时显示服务输出'
    Write-Host '  - 首选端口被外部进程占用时自动切换到下一个空闲端口 (前后端自动联通)'
    Write-Host '  - PID/端口文件位于 .run\ 目录, 可随时整体删除'
    Write-Host '  - 不带参数运行时默认执行 start (适配右键"使用 PowerShell 运行")'
    Write-Host '  - 首选端口: backend=8300, web=5173, admin=5174 (8000 被其他项目占用)'
}

# =============================================================================
# 函数: Get-ListenPid
# 说明: 获取指定端口上处于 Listen 状态的进程 PID
# 参数: $Port - 要查询的本地端口号
# 返回: 监听该端口的进程 PID (int); 无监听时返回 $null
# 异常: Get-NetTCPConnection 失败已被 SilentlyContinue 抑制, 返回 $null
# =============================================================================
function Get-ListenPid {
    param([Parameter(Mandatory = $true)][int]$Port)

    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

# =============================================================================
# 函数: Test-PortListening
# 说明: 判断指定端口当前是否处于监听状态
# 参数: $Port - 要检查的本地端口号
# 返回: $true 表示已有进程监听; $false 表示无监听
# 异常: 无 (内部查询失败视为无监听)
# =============================================================================
function Test-PortListening {
    param([Parameter(Mandatory = $true)][int]$Port)

    return ($null -ne (Get-ListenPid -Port $Port))
}

# =============================================================================
# 函数: Wait-PortListening
# 说明: 在超时时间内周期性探测端口, 直到其开始监听
# 参数: $Port - 要探测的端口号
#       $TimeoutSec - 最长等待秒数 (默认 60)
# 返回: $true 表示端口已监听; $false 表示超时仍未监听
# 异常: 无
# =============================================================================
function Wait-PortListening {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSec = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -Port $Port) { return $true }
        Start-Sleep -Seconds 2
    }
    return (Test-PortListening -Port $Port)
}

# =============================================================================
# 函数: Wait-PortReleased
# 说明: 在超时时间内周期性探测端口, 直到其释放 (不再监听)
# 参数: $Port - 要探测的端口号
#       $TimeoutSec - 最长等待秒数 (默认 15)
# 返回: $true 表示端口已释放; $false 表示超时仍被监听
# 异常: 无
# =============================================================================
function Wait-PortReleased {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSec = 15
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-PortListening -Port $Port)) { return $true }
        Start-Sleep -Seconds 1
    }
    return (-not (Test-PortListening -Port $Port))
}

# =============================================================================
# 函数: Get-RecordedPort
# 说明: 读取 .run\<name>.port 中记录的服务实际端口 (端口漂移时与首选端口不同)
# 参数: $Svc - 服务定义哈希表 (需含 PortFile / Port 键)
# 返回: 记录的端口号 (int); 文件不存在或内容非法时返回 $null (调用方回退首选端口)
# 异常: 无 (读取失败视为无记录)
# =============================================================================
function Get-RecordedPort {
    param([Parameter(Mandatory = $true)]$Svc)

    if (Test-Path $Svc.PortFile) {
        $raw = Get-Content $Svc.PortFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($raw -match '^\d+$') { return [int]$Matches[0] }
    }
    return $null
}

# =============================================================================
# 函数: Set-RecordedPort
# 说明: 将服务实际使用的端口写入 .run\<name>.port (供 status/stop 与前端联通使用)
# 参数: $Svc - 服务定义哈希表 (需含 PortFile 键)
#       $Port - 要记录的端口号
# 返回: 无
# 异常: 写文件失败时抛出 (由调用方捕获处理)
# =============================================================================
function Set-RecordedPort {
    param(
        [Parameter(Mandatory = $true)]$Svc,
        [Parameter(Mandatory = $true)][int]$Port
    )

    Set-Content -Path $Svc.PortFile -Value $Port -Encoding ASCII
}

# =============================================================================
# 函数: Remove-RecordedPort
# 说明: 删除 .run\<name>.port 端口记录文件 (stop 清理时调用; 不存在时静默忽略)
# 参数: $Svc - 服务定义哈希表 (需含 PortFile 键)
# 返回: 无
# 异常: 无
# =============================================================================
function Remove-RecordedPort {
    param([Parameter(Mandatory = $true)]$Svc)

    Remove-Item -Path $Svc.PortFile -ErrorAction SilentlyContinue
}

# =============================================================================
# 函数: Find-FreePort
# 说明: 从 $Start 端口起向后扫描第一个可用端口: 未被任何进程监听, 且不在
#       $Reserved 集合中 (避免与本次启动的其他服务端口冲突)
# 参数: $Start - 扫描起始端口
#       $Reserved - 要避开(保留)的端口数组 (如全部首选端口与已选定的端口)
#       $MaxTries - 最多向后尝试的端口个数 (默认 20)
# 返回: 第一个可用端口号 (int); 全部被占用时返回 $null
# 异常: 无
# =============================================================================
function Find-FreePort {
    param(
        [Parameter(Mandatory = $true)][int]$Start,
        [int[]]$Reserved = @(),
        [int]$MaxTries = 20
    )

    $p = $Start
    for ($i = 0; $i -lt $MaxTries; $i++) {
        if ($Reserved -notcontains $p -and -not (Test-PortListening -Port $p)) { return $p }
        $p++
    }
    return $null
}

# =============================================================================
# 函数: Stop-ProcessTree
# 说明: 强制终止指定进程及其全部子进程 (递归后序遍历进程树)
# 参数: $ProcId - 要终止的根进程 PID
# 返回: 无
# 异常: 目标进程已退出时静默忽略; Stop-Process 其他失败同样被捕获忽略
# =============================================================================
function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcId)

    # 先递归终止所有子进程 (npm/cmd 等会派生子进程, 需整树清理)
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcId" -ErrorAction SilentlyContinue)
    foreach ($c in $children) {
        Stop-ProcessTree -ProcId ([int]$c.ProcessId)
    }
    try { Stop-Process -Id $ProcId -Force -ErrorAction Stop } catch { }
}

# =============================================================================
# 函数: Test-IsProjectProcess
# 说明: 判断指定进程是否属于本项目 (命令行或可执行路径包含项目根目录)
#       用于 stop 时保护其他项目/任务的进程, 避免误杀
# 参数: $ProcId - 要检查的进程 PID
# 返回: $true 表示属于本项目; $false 表示不属于或进程已不存在
# 异常: 无 (进程查询失败视为不属于)
# =============================================================================
function Test-IsProjectProcess {
    param([Parameter(Mandatory = $true)][int]$ProcId)

    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    $text = "$($p.CommandLine) $($p.ExecutablePath)"
    # -like 大小写不敏感, 匹配项目根路径
    return ($text -like "*$Root*")
}

# =============================================================================
# 函数: Start-ServiceWindow
# 说明: 通过 WMI (Win32_Process.Create) 为服务创建一个独立可见的控制台窗口:
#       启动 powershell.exe -NoExit, 在其中设置窗口标题/编码/工作目录后执行
#       服务命令, 服务输出实时显示在该窗口中; 进程父级为 WmiPrvSE, 与调用方
#       完全解耦, 不属于调用方的进程树/Job Object, 因此在 IDE、CI 等
#       kill-on-close 环境中运行本脚本时, 服务窗口不会被连带清理。
# 参数: $Title - 新窗口标题 (便于区分 backend/web/admin)
#       $WorkDir - 服务工作目录 (Set-Location 目标, 同时作为 WMI CurrentDirectory)
#       $ExePath - 要执行的可执行文件绝对路径 (python.exe / npm.cmd)
#       $ArgumentString - 传给可执行文件的参数字符串
#       $EnvSettings - 需注入子进程的环境变量数组, 形如 "KEY=VALUE"
#                      (在新窗口内通过 $env:KEY='VALUE' 设置, 由服务进程继承)
# 返回: 新建窗口宿主 powershell.exe 的 PID (int)
# 异常: WMI 创建失败 (ReturnValue 非 0) 或 CIM 调用异常时抛出 RuntimeException
# =============================================================================
function Start-ServiceWindow {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][string]$ExePath,
        [Parameter(Mandatory = $true)][string]$ArgumentString,
        [string[]]$EnvSettings = @()
    )

    # 环境变量注入片段: 在新窗口内设置 $env:KEY, 服务进程 (uvicorn/vite) 继承。
    # 不使用 WMI ProcessStartup.Environment, 因为它会完全替换继承的环境
    # (PATH 等会丢失); 通过窗口内 PowerShell 设置可自然叠加在父环境之上。
    $envPart = ''
    foreach ($e in $EnvSettings) {
        if ($e -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $envPart += ('$env:{0}=''{1}''; ' -f $Matches[1], $Matches[2])
        }
    }

    # 新窗口内执行的 PowerShell 片段: 单引号模板 + -f 填充, 避免 $host 等
    # 变量被当前进程提前展开; 片段内只使用单引号, 保证可安全包进外层双引号
    $tpl = '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; ' +
        '$host.UI.RawUI.WindowTitle=''{0}''; ' +
        'Set-Location -LiteralPath ''{1}''; ' +
        'Write-Host ''>> 服务运行中, 本窗口实时显示输出 (Ctrl+C 停止服务)...''; ' +
        '& ''{2}'' {3}'
    # 进程退出后提示退出码 (由新窗口内的 PowerShell 求值 $LASTEXITCODE)
    $exitNote = ' ; Write-Host (''> 进程已退出 (退出码: '' + $LASTEXITCODE + ''). 可直接关闭本窗口。'')'
    $snippet = $envPart + ($tpl -f $Title, $WorkDir, $ExePath, $ArgumentString) + $exitNote

    # 用系统自带 powershell.exe 绝对路径 (WMI 子进程继承 WmiPrvSE 的 PATH,
    # 与用户会话不同, 不能依赖命令名解析)
    $psExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $cmdLine = '"{0}" -NoProfile -ExecutionPolicy Bypass -NoExit -Command "{1}"' -f $psExe, $snippet

    # ShowWindow = 1 (SW_SHOWNORMAL): 显示控制台窗口
    $startup = New-CimInstance -CimClass (Get-CimClass Win32_ProcessStartup) -Property @{ ShowWindow = 1 } -ClientOnly
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine               = $cmdLine
        CurrentDirectory          = $WorkDir
        ProcessStartupInformation = $startup
    }
    if ($result.ReturnValue -ne 0) {
        throw ("WMI Win32_Process.Create 失败, ReturnValue={0}" -f $result.ReturnValue)
    }
    return [int]$result.ProcessId
}

# =============================================================================
# 函数: Invoke-Start
# 说明: 启动流程:
#       1) 检查并按需启动 PostgreSQL 服务 (失败则中止)
#       2) 幂等初始化数据库 init_db (失败则中止)
#       3) 逐个启动 backend/web/admin: 首选端口被本项目进程占用则视为已在
#          运行并跳过; 被外部进程占用则自动向后扫描空闲端口 (最多 +20);
#          通过 WMI 弹出独立控制台窗口启动、写入 PID/端口文件并轮询端口最多
#          60 秒。后端实际端口通过 MANGAFLOW_API_PORT 环境变量注入前端窗口,
#          两个 vite.config.ts 的 /api 代理读取该变量, 保证前后端联通。
#          启动失败时提示用户查看对应服务窗口中的报错信息 (不强行终止窗口
#          进程, 便于排查), 可用 stop 命令统一清理
# 参数: 无
# 返回: $true 表示全部服务就绪; $false 表示存在失败
# 异常: Start-Service / 进程创建失败均被捕获并转为返回 $false
# =============================================================================
function Invoke-Start {
    # --- 步骤 1: PostgreSQL 服务 -------------------------------------------
    Write-Host '==> [1/3] 检查 PostgreSQL 服务 (postgresql-x64-16)...'
    $pgSvc = Get-Service -Name 'postgresql-x64-16' -ErrorAction SilentlyContinue
    if (-not $pgSvc) {
        Write-Host '[错误] 未找到 Windows 服务 postgresql-x64-16, 请先安装 PostgreSQL 16。'
        return $false
    }
    if ($pgSvc.Status -ne 'Running') {
        Write-Host 'PostgreSQL 未运行, 正在尝试启动...'
        try {
            Start-Service -Name 'postgresql-x64-16' -ErrorAction Stop
        } catch {
            Write-Host "[错误] Start-Service 失败: $($_.Exception.Message)"
            Write-Host '       请手动在 services.msc 中启动 postgresql-x64-16 后重试。'
            return $false
        }
        $pgSvc.Refresh()
        if ($pgSvc.Status -ne 'Running') {
            Write-Host '[错误] PostgreSQL 服务启动后仍未进入 Running 状态, 中止。'
            return $false
        }
    }
    Write-Host 'PostgreSQL: 运行中'

    # --- 步骤 2: 幂等初始化数据库 ------------------------------------------
    Write-Host '==> [2/3] 初始化数据库 (init_db, 幂等)...'
    $initOk = $false
    Push-Location (Join-Path $Root 'backend')
    try {
        $initOutput = & '.venv\Scripts\python.exe' -m app.core.init_db 2>&1
        $initOk = ($LASTEXITCODE -eq 0)
        if ($initOutput) { $initOutput | ForEach-Object { Write-Host "  $_" } }
    } finally {
        Pop-Location
    }
    if (-not $initOk) {
        Write-Host '[错误] 数据库初始化失败 (init_db 退出码非 0), 中止启动。'
        return $false
    }
    Write-Host '数据库初始化: 完成'

    # --- 步骤 3: 启动三个服务 (各弹出一个独立控制台窗口) --------------------
    Write-Host '==> [3/3] 启动服务 (backend / web / admin, 各弹出独立窗口)...'
    $anyFail = $false
    # 端口保留集合: 全部首选端口 + 本次已选定的端口, 避免服务间互相抢占
    $reserved = @($Services | ForEach-Object { [int]$_.Port })
    # 后端实际端口 (在 backend 分支确定后供 web/admin 代理联通使用)
    $apiPort = $null
    # 各服务最终使用的端口 (含"已在运行跳过"的), 用于结尾输出访问地址
    $chosen = @{}

    foreach ($s in $Services) {
        # --- 端口决策 --------------------------------------------------------
        # (a) .port 文件记录的端口正被本项目进程监听 -> 上次启动的实例仍在, 跳过
        $recorded = Get-RecordedPort -Svc $s
        if ($null -ne $recorded) {
            $listener = Get-ListenPid -Port $recorded
            if ($null -ne $listener -and (Test-IsProjectProcess -ProcId $listener)) {
                Write-Host ("[{0}] 已在运行 (pid={1}, 端口 {2}), 跳过启动" -f $s.Name, $listener, $recorded)
                $chosen[$s.Name] = $recorded
                if ($s.Name -eq 'backend') { $apiPort = $recorded }
                continue
            }
        }

        # (b) 首选端口
        $port = $null
        $listener = Get-ListenPid -Port $s.Port
        if ($null -ne $listener) {
            if (Test-IsProjectProcess -ProcId $listener) {
                # 首选端口被本项目进程占用 (如用户手动启动) -> 视为已在运行, 幂等跳过
                Write-Host ("[{0}] 已在运行 (pid={1}, 端口 {2}), 跳过启动" -f $s.Name, $listener, $s.Port)
                Set-RecordedPort -Svc $s -Port $s.Port
                $chosen[$s.Name] = $s.Port
                if ($s.Name -eq 'backend') { $apiPort = $s.Port }
                continue
            }
            # 首选端口被外部进程占用 -> 自动向后扫描第一个空闲端口
            $port = Find-FreePort -Start ($s.Port + 1) -Reserved $reserved
            if ($null -eq $port) {
                $pname = (Get-Process -Id $listener -ErrorAction SilentlyContinue).ProcessName
                Write-Host ("[{0}] [错误] 端口 {1} 被外部进程 (pid={2}, 进程={3}) 占用, 且后续 20 个端口均无空闲, 跳过启动。" -f $s.Name, $s.Port, $listener, $pname)
                $anyFail = $true
                continue
            }
            $pname = (Get-Process -Id $listener -ErrorAction SilentlyContinue).ProcessName
            Write-Host ("[{0}] 端口 {1} 被外部进程 (pid={2}, 进程={3}) 占用, 自动改用端口 {4}" -f $s.Name, $s.Port, $listener, $pname, $port)
        } else {
            $port = $s.Port
        }
        $reserved += $port

        # --- 启动进程 --------------------------------------------------------
        Write-Host ("[{0}] 正在启动 (端口 {1})..." -f $s.Name, $port)
        $title = $s.TitleFormat -f $port
        try {
            if ($s.Name -eq 'backend') {
                # backend: 在新窗口中用 venv python 启动 uvicorn
                $procId = Start-ServiceWindow -Title $title `
                    -WorkDir (Join-Path $Root 'backend') `
                    -ExePath (Join-Path $Root 'backend\.venv\Scripts\python.exe') `
                    -ArgumentString ("-m uvicorn app.main:app --host 127.0.0.1 --port {0}" -f $port)
                $apiPort = $port
            } else {
                # web/admin: Vite 通过 npm run dev 启动; --port 指定脚本选定的端口,
                # --strictPort 保证被占时报错退出而非静默漂移 (漂移会让探活/stop 失准)。
                # 注意: WMI 启动的进程继承的是 WmiPrvSE 的环境 (PATH 与用户
                # 会话不同), 必须用 npm.cmd 的绝对路径, 避免解析到错误的 shim
                $npmPath = (Get-Command 'npm.cmd' -ErrorAction Stop).Source
                $workDir = if ($s.Name -eq 'web') { Join-Path $Root 'mangaflow-web' } else { Join-Path $Root 'mangaflow-admin-web' }
                # 后端实际端口注入前端: vite.config.ts 的 /api 代理读取该变量,
                # 后端端口漂移时前端仍能联通 (backend 先于 web/admin 处理)
                if (-not $apiPort) { $apiPort = $Services[0].Port }
                $procId = Start-ServiceWindow -Title $title `
                    -WorkDir $workDir `
                    -ExePath $npmPath `
                    -ArgumentString ("run dev -- --port {0} --strictPort" -f $port) `
                    -EnvSettings @("MANGAFLOW_API_PORT=$apiPort")
            }
        } catch {
            Write-Host ("[{0}] [错误] 启动进程失败: {1}" -f $s.Name, $_.Exception.Message)
            $anyFail = $true
            continue
        }

        # 记录 PID 文件 (窗口宿主 powershell.exe) 与实际端口文件
        Set-Content -Path $s.PidFile -Value $procId -Encoding ASCII
        Set-RecordedPort -Svc $s -Port $port
        $chosen[$s.Name] = $port

        # 轮询端口最多 60 秒 (每 2 秒探测一次)
        if (Wait-PortListening -Port $port -TimeoutSec 60) {
            Write-Host ("[{0}] 启动成功 (pid={1}, 端口 {2} 监听中)" -f $s.Name, $procId, $port)
        } else {
            # 不强行终止窗口进程: 窗口保持打开便于查看报错; 可用 stop 统一清理
            Write-Host ("[{0}] [警告] 端口 {1} 在 60 秒内未开始监听, 请查看弹出的服务窗口中的报错信息。" -f $s.Name, $port)
            Write-Host '       (确认失败后可运行 .\mangaflow.ps1 stop 清理再重试)'
            $anyFail = $true
        }
    }

    if ($anyFail) { return $false }

    Write-Host ''
    Write-Host '全部服务已就绪, 访问地址:'
    Write-Host ("  API    : http://127.0.0.1:{0}/docs" -f $chosen['backend'])
    Write-Host ("  用户端 : http://localhost:{0}" -f $chosen['web'])
    Write-Host ("  管理端 : http://localhost:{0}" -f $chosen['admin'])
    $drifted = $chosen.Keys | Where-Object { $chosen[$_] -ne ($Services | Where-Object Name -eq $_).Port }
    if ($drifted) {
        Write-Host ("端口漂移: {0} (首选端口被占用, 已自动切换, 前端代理已同步)" -f (($drifted | ForEach-Object { "$_=$($chosen[$_])" }) -join ', '))
    }
    return $true
}

# =============================================================================
# 函数: Invoke-Stop
# 说明: 停止流程:
#       1) 读取 .run\<name>.pid, 对存活进程执行整树强杀 (含 cmd/npm 派生的
#          node 子进程)
#       2) 等待实际端口 (.run\<name>.port 记录端口, 无记录则用首选端口) 释放
#          最多 15 秒; 若端口仍被"本项目残留进程"占用 (PID 文件失效的场景),
#          继续按端口定位并树杀
#       3) 若端口仍被"外部进程"占用 (非本脚本启动, 例如其他项目), 只警告不杀
#       4) 清理 PID/端口文件; PostgreSQL 服务保持运行不受影响
# 参数: 无
# 返回: $true 表示所有受管端口均已释放; $false 表示有端口仍被外部进程占用
# 异常: 无 (单个进程操作失败不影响整体流程)
# =============================================================================
function Invoke-Stop {
    Write-Host '==> 停止服务 (backend / web / admin)...'
    $allReleased = $true

    foreach ($s in $Services) {
        # 实际端口: 优先 .port 记录 (端口漂移时与首选端口不同), 无记录则用首选端口
        $port = Get-RecordedPort -Svc $s
        if ($null -eq $port) { $port = [int]$s.Port }

        # 读取 PID 文件 (不存在或内容非法则视为无记录)
        $recordedPid = $null
        if (Test-Path $s.PidFile) {
            $raw = Get-Content $s.PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($raw -match '^\d+$') { $recordedPid = [int]$Matches[0] }
        }

        # 树杀 PID 文件记录的存活进程
        if ($null -ne $recordedPid -and (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)) {
            Write-Host ("[{0}] 正在停止 pid={1} (含子进程树)..." -f $s.Name, $recordedPid)
            Stop-ProcessTree -ProcId $recordedPid
        }

        # 等待端口释放 (最多 15 秒)
        if (-not (Wait-PortReleased -Port $port -TimeoutSec 15)) {
            $listener = Get-ListenPid -Port $port
            # PID 文件失效但端口被本项目残留进程占用 -> 按端口兜底树杀
            if ($null -ne $listener -and (Test-IsProjectProcess -ProcId $listener)) {
                Write-Host ("[{0}] 端口仍被本项目残留进程 pid={1} 占用, 继续终止..." -f $s.Name, $listener)
                Stop-ProcessTree -ProcId $listener
                Wait-PortReleased -Port $port -TimeoutSec 15 | Out-Null
            }
        }

        # 最终校验
        if (Test-PortListening -Port $port) {
            $listener = Get-ListenPid -Port $port
            $pname = (Get-Process -Id $listener -ErrorAction SilentlyContinue).ProcessName
            Write-Host ("[{0}] [警告] 端口 {1} 仍被占用 (pid={2}, 进程={3})。该进程非本脚本启动 (外部进程), 未终止。" -f $s.Name, $port, $listener, $pname)
            $allReleased = $false
        } else {
            Write-Host ("[{0}] 已停止 (端口 {1} 已释放)" -f $s.Name, $port)
        }

        # 清理 PID/端口文件 (旧日志文件保留, 便于排查)
        Remove-Item -Path $s.PidFile -ErrorAction SilentlyContinue
        Remove-RecordedPort -Svc $s
    }

    Write-Host '注: PostgreSQL 服务 (postgresql-x64-16) 保持运行, 本脚本不停止数据库。'
    return $allReleased
}

# =============================================================================
# 函数: Show-Status
# 说明: 以表格显示各服务状态: PostgreSQL 服务 + backend/web/admin,
#       含运行状态、PID、端口、进程名; 端口优先显示 .port 文件记录的实际
#       端口 (无记录则显示首选端口); backend 额外对实际端口做 HTTP 探活
#       (GET http://127.0.0.1:<port>/)。端口被外部进程占用时也会如实显示。
# 参数: 无
# 返回: 无 (仅输出表格)
# 异常: 无 (探活失败显示"无响应")
# =============================================================================
function Show-Status {
    $rows = @()

    # PostgreSQL 服务行
    $pgSvc = Get-Service -Name 'postgresql-x64-16' -ErrorAction SilentlyContinue
    $pgState = if ($pgSvc) { $pgSvc.Status } else { '未安装' }
    $pgPid = $null
    if ($pgSvc) {
        $pgWinSvc = Get-CimInstance Win32_Service -Filter "Name='postgresql-x64-16'" -ErrorAction SilentlyContinue
        if ($pgWinSvc -and $pgWinSvc.ProcessId -gt 0) { $pgPid = [int]$pgWinSvc.ProcessId }
    }
    $rows += [PSCustomObject]@{
        '服务'   = 'postgresql-x64-16'
        '状态'   = $pgState
        'PID'    = $pgPid
        '端口'   = 5432
        '进程名' = 'postgres'
        'API状态' = '-'
    }

    # 三个受管服务行 (端口优先取 .port 记录的实际端口, 无记录则用首选端口)
    foreach ($s in $Services) {
        $port = Get-RecordedPort -Svc $s
        if ($null -eq $port) { $port = [int]$s.Port }
        $listener = Get-ListenPid -Port $port
        $procName = '-'
        if ($null -ne $listener) {
            $p = Get-Process -Id $listener -ErrorAction SilentlyContinue
            $procName = if ($p) { $p.ProcessName } else { '未知' }
        }
        $state = if ($null -ne $listener) { '运行中' } else { '已停止' }

        $api = '-'
        if ($s.Name -eq 'backend') {
            if ($null -ne $listener) {
                try {
                    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/" -f $port) -UseBasicParsing -TimeoutSec 3
                    $api = "HTTP $($r.StatusCode)"
                } catch {
                    # 有 HTTP 响应但状态码 >= 400 也视为服务存活
                    if ($_.Exception.Response) {
                        $api = "HTTP $([int]$_.Exception.Response.StatusCode)"
                    } else {
                        $api = '无响应'
                    }
                }
            } else {
                $api = '未监听'
            }
        }

        $rows += [PSCustomObject]@{
            '服务'   = $s.Name
            '状态'   = $state
            'PID'    = $listener
            '端口'   = $port
            '进程名' = $procName
            'API状态' = $api
        }
    }

    $rows | Format-Table -AutoSize
    Write-Host ('运行时文件目录: {0} (PID/端口文件, 可随时删除)' -f $RunDir)
}

# =============================================================================
# 主流程: 根据 param 传入的命令分发
# 说明: 未显式传参 (右键"使用 PowerShell 运行"/双击) 时, 默认执行 start 并在
#       结束后暂停等待 Enter, 避免控制台一闪而过; 显式传参 (终端/IDE/CI 调用)
#       时保持原有行为, 通过退出码反馈结果, 不阻塞。
# =============================================================================
# 是否为"无参数直接运行": 通过 PSBoundParameters 判断, 而非依赖 $Command 默认值
$interactive = (-not $PSBoundParameters.ContainsKey('Command'))
$ok = $false

switch ($Command) {
    'start' {
        $ok = Invoke-Start
    }
    'stop' {
        $ok = Invoke-Stop
    }
    'status' {
        Show-Status
        $ok = $true
    }
    'restart' {
        $stopOk = Invoke-Stop
        $ok = ($stopOk -and (Invoke-Start))
    }
    default {
        Write-Usage
        $ok = $false
    }
}

# 交互场景: 暂停等待用户按 Enter, 避免启动结果/报错一闪而过
if ($interactive) {
    Write-Host ''
    Read-Host '按 Enter 关闭本窗口' | Out-Null
}
if ($ok) { exit 0 } else { exit 1 }
