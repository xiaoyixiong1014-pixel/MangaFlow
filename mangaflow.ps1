#requires -Version 5.1
# =============================================================================
# 文件名: mangaflow.ps1
# 说明: MangaFlow 项目 Windows PowerShell 一键启停脚本
#
# 管理的服务:
#   - PostgreSQL 16      Windows 服务 postgresql-x64-16 (localhost:5432)
#   - backend (FastAPI)  uvicorn, 端口 8300, venv 位于 backend\.venv
#   （端口说明: 本机 8000 被其他项目长期占用, MangaFlow 后端统一使用 8300）
#   - web     (Vite)     npm run dev, 端口 5173, 目录 mangaflow-web
#   - admin   (Vite)     npm run dev, 端口 5174, 目录 mangaflow-admin-web
#
# 用法:
#   .\mangaflow.ps1 start|stop|status|restart
#     start   : 检查/启动 PostgreSQL -> 幂等初始化数据库 -> 启动三服务
#     stop    : 停止本脚本启动的 backend/web/admin (保留 PostgreSQL 运行)
#     status  : 表格显示各服务状态 (含后端 HTTP 探活)
#     restart : stop + start
#
# 运行时文件 (位于项目根 .run\ 目录, 可随时整体删除):
#   .run\<name>.pid      各服务主进程 (cmd.exe) PID
#   .run\<name>.out.log  各服务标准输出日志
#   .run\<name>.err.log  各服务标准错误日志
#
# 启动机制说明: 通过 WMI (Win32_Process.Create) 创建后台进程, 其父进程为
#   WmiPrvSE 服务, 与调用方完全解耦——因此从 IDE 任务/CI/计划任务等
#   Job Object (kill-on-close) 环境调用本脚本时, 服务进程不会被连带清理;
#   日志重定向由 cmd.exe 原生 > / 2> 完成。
#
# 兼容性: PowerShell 5.1+; 保存为 UTF-8 with BOM 以正确显示中文
# =============================================================================
param(
    [Parameter(Position = 0, HelpMessage = '要执行的命令: start/stop/status/restart')]
    [string]$Command
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

# 受管服务定义: Name=服务名; Port=监听端口; PidFile/OutLog/ErrLog=运行时文件路径
$Services = @(
    @{ Name = 'backend'; Port = 8300; PidFile = (Join-Path $RunDir 'backend.pid'); OutLog = (Join-Path $RunDir 'backend.out.log'); ErrLog = (Join-Path $RunDir 'backend.err.log') },
    @{ Name = 'web';     Port = 5173; PidFile = (Join-Path $RunDir 'web.pid');     OutLog = (Join-Path $RunDir 'web.out.log');     ErrLog = (Join-Path $RunDir 'web.err.log') },
    @{ Name = 'admin';   Port = 5174; PidFile = (Join-Path $RunDir 'admin.pid');   OutLog = (Join-Path $RunDir 'admin.out.log');   ErrLog = (Join-Path $RunDir 'admin.err.log') }
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
    Write-Host '  - PID 与日志文件位于 .run\ 目录, 可随时整体删除'
    Write-Host '  - 端口: backend=8300, web=5173, admin=5174 (8000 被其他项目占用)'
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
# 函数: Show-LogTail
# 说明: 打印某服务的 stdout/stderr 日志文件末尾各 20 行 (用于启动失败诊断)
# 参数: $Svc - 服务定义哈希表 (需含 OutLog / ErrLog 键)
# 返回: 无 (仅输出文本)
# 异常: 无 (日志文件不存在时跳过)
# =============================================================================
function Show-LogTail {
    param([Parameter(Mandatory = $true)]$Svc)

    foreach ($f in @($Svc.OutLog, $Svc.ErrLog)) {
        if (Test-Path $f) {
            Write-Host ("  ---- {0} (尾部 20 行) ----" -f (Split-Path $f -Leaf))
            Get-Content $f -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
        }
    }
}

# =============================================================================
# 函数: Start-DetachedProcess
# 说明: 通过 WMI (Win32_Process.Create) 创建与调用方完全解耦的后台进程:
#       实际启动 cmd.exe /s /c "<命令> 1>out 2>err"，由 cmd 原生重定向日志;
#       进程父级为 WmiPrvSE, 不属于调用方的进程树/Job Object, 因此在 IDE、
#       CI 等 kill-on-close 环境中运行本脚本时服务进程不会被连带清理。
# 参数: $FilePath - 要执行的程序路径或命令名 (如 python.exe / npm)
#       $ArgumentString - 传给程序的参数字符串 (如 '-m uvicorn ...')
#       $WorkDir - 工作目录 (将作为 WMI Create 的 CurrentDirectory)
#       $OutLog - 标准输出日志文件路径
#       $ErrLog - 标准错误日志文件路径
# 返回: 新建 cmd.exe 包装进程的 PID (int)
# 异常: WMI 创建失败 (ReturnValue 非 0) 或 CIM 调用异常时抛出 RuntimeException
# =============================================================================
function Start-DetachedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$ArgumentString,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][string]$OutLog,
        [Parameter(Mandatory = $true)][string]$ErrLog
    )

    # 组装 cmd /s /c 命令行: /s 保证外层引号被正确剥离, 1>/2> 为原生日志重定向
    $inner = '"{0}" {1} 1> "{2}" 2> "{3}"' -f $FilePath, $ArgumentString, $OutLog, $ErrLog
    $cmdLine = 'cmd.exe /s /c "{0}"' -f $inner

    # ShowWindow = 0 (SW_HIDE): 隐藏 cmd 控制台窗口
    $startup = New-CimInstance -CimClass (Get-CimClass Win32_ProcessStartup) -Property @{ ShowWindow = 0 } -ClientOnly
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
#       3) 逐个启动 backend/web/admin: 端口已被监听则跳过 (视为已在运行),
#          否则通过 WMI 后台启动进程、写入 PID 文件并轮询端口最多 60 秒;
#          启动失败时打印对应日志尾部 20 行并终止其半启动进程树
# 参数: 无
# 返回: $true 表示全部服务就绪; $false 表示存在失败 (已中止)
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

    # --- 步骤 3: 启动三个服务 ----------------------------------------------
    Write-Host '==> [3/3] 启动服务 (backend / web / admin)...'
    $anyFail = $false
    foreach ($s in $Services) {
        # 端口已被监听 -> 视为已在运行, 跳过 (可能是本脚本上次启动, 也可能是外部进程)
        $listener = Get-ListenPid -Port $s.Port
        if ($null -ne $listener) {
            Write-Host ("[{0}] 已在运行 (端口 {1} 已被 pid={2} 监听), 跳过启动" -f $s.Name, $s.Port, $listener)
            continue
        }

        Write-Host ("[{0}] 正在启动 (端口 {1})..." -f $s.Name, $s.Port)
        try {
            if ($s.Name -eq 'backend') {
                # backend: venv python 启动 uvicorn
                $procId = Start-DetachedProcess -FilePath (Join-Path $Root 'backend\.venv\Scripts\python.exe') `
                    -ArgumentString '-m uvicorn app.main:app --host 127.0.0.1 --port 8300' `
                    -WorkDir (Join-Path $Root 'backend') `
                    -OutLog $s.OutLog -ErrLog $s.ErrLog
            } else {
                # web/admin: Vite 是 cmd 脚本, 通过 npm run dev 启动。
                # 注意: WMI 启动的进程继承的是 WmiPrvSE 的环境 (PATH 与用户
                # 会话不同), 必须用 npm.cmd 的绝对路径, 避免解析到错误的 shim
                $npmPath = (Get-Command 'npm.cmd' -ErrorAction Stop).Source
                $workDir = if ($s.Name -eq 'web') { Join-Path $Root 'mangaflow-web' } else { Join-Path $Root 'mangaflow-admin-web' }
                $procId = Start-DetachedProcess -FilePath $npmPath `
                    -ArgumentString 'run dev' `
                    -WorkDir $workDir `
                    -OutLog $s.OutLog -ErrLog $s.ErrLog
            }
        } catch {
            Write-Host ("[{0}] [错误] 启动进程失败: {1}" -f $s.Name, $_.Exception.Message)
            Show-LogTail -Svc $s
            $anyFail = $true
            continue
        }

        # 记录 PID 文件
        Set-Content -Path $s.PidFile -Value $procId -Encoding ASCII

        # 轮询端口最多 60 秒 (每 2 秒探测一次)
        if (Wait-PortListening -Port $s.Port -TimeoutSec 60) {
            Write-Host ("[{0}] 启动成功 (pid={1}, 端口 {2} 监听中)" -f $s.Name, $procId, $s.Port)
        } else {
            Write-Host ("[{0}] [错误] 启动失败: 端口 {1} 在 60 秒内未开始监听。日志尾部:" -f $s.Name, $s.Port)
            Show-LogTail -Svc $s
            # 终止半启动的进程树, 避免残留, 并清理 PID 文件
            Stop-ProcessTree -ProcId $procId
            Remove-Item -Path $s.PidFile -ErrorAction SilentlyContinue
            $anyFail = $true
        }
    }

    if ($anyFail) { return $false }

    Write-Host ''
    Write-Host '全部服务已就绪, 访问地址:'
    Write-Host '  API    : http://127.0.0.1:8300/docs'
    Write-Host '  用户端 : http://localhost:5173'
    Write-Host '  管理端 : http://localhost:5174'
    return $true
}

# =============================================================================
# 函数: Invoke-Stop
# 说明: 停止流程:
#       1) 读取 .run\<name>.pid, 对存活进程执行整树强杀 (含 cmd/npm 派生的
#          node 子进程)
#       2) 等待端口释放最多 15 秒; 若端口仍被"本项目残留进程"占用 (PID 文件
#          失效的场景), 继续按端口定位并树杀
#       3) 若端口仍被"外部进程"占用 (非本脚本启动, 例如其他项目), 只警告不杀
#       4) 清理 PID 文件; PostgreSQL 服务保持运行不受影响
# 参数: 无
# 返回: $true 表示所有受管端口均已释放; $false 表示有端口仍被外部进程占用
# 异常: 无 (单个进程操作失败不影响整体流程)
# =============================================================================
function Invoke-Stop {
    Write-Host '==> 停止服务 (backend / web / admin)...'
    $allReleased = $true

    foreach ($s in $Services) {
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
        if (-not (Wait-PortReleased -Port $s.Port -TimeoutSec 15)) {
            $listener = Get-ListenPid -Port $s.Port
            # PID 文件失效但端口被本项目残留进程占用 -> 按端口兜底树杀
            if ($null -ne $listener -and (Test-IsProjectProcess -ProcId $listener)) {
                Write-Host ("[{0}] 端口仍被本项目残留进程 pid={1} 占用, 继续终止..." -f $s.Name, $listener)
                Stop-ProcessTree -ProcId $listener
                Wait-PortReleased -Port $s.Port -TimeoutSec 15 | Out-Null
            }
        }

        # 最终校验
        if (Test-PortListening -Port $s.Port) {
            $listener = Get-ListenPid -Port $s.Port
            $pname = (Get-Process -Id $listener -ErrorAction SilentlyContinue).ProcessName
            Write-Host ("[{0}] [警告] 端口 {1} 仍被占用 (pid={2}, 进程={3})。该进程非本脚本启动 (外部进程), 未终止。" -f $s.Name, $s.Port, $listener, $pname)
            $allReleased = $false
        } else {
            Write-Host ("[{0}] 已停止 (端口 {1} 已释放)" -f $s.Name, $s.Port)
        }

        # 清理 PID 文件 (日志保留, 便于排查)
        Remove-Item -Path $s.PidFile -ErrorAction SilentlyContinue
    }

    Write-Host '注: PostgreSQL 服务 (postgresql-x64-16) 保持运行, 本脚本不停止数据库。'
    return $allReleased
}

# =============================================================================
# 函数: Show-Status
# 说明: 以表格显示各服务状态: PostgreSQL 服务 + backend/web/admin,
#       含运行状态、PID、端口、进程名; backend 额外做 HTTP 探活
#       (GET http://127.0.0.1:8300/)。端口被外部进程占用时也会如实显示。
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

    # 三个受管服务行
    foreach ($s in $Services) {
        $listener = Get-ListenPid -Port $s.Port
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
                    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8300/' -UseBasicParsing -TimeoutSec 3
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
            '端口'   = $s.Port
            '进程名' = $procName
            'API状态' = $api
        }
    }

    $rows | Format-Table -AutoSize
    Write-Host ('运行时文件目录: {0} (PID 与日志, 可随时删除)' -f $RunDir)
}

# =============================================================================
# 主流程: 根据 param 传入的命令分发
# =============================================================================
switch ($Command) {
    'start' {
        if (Invoke-Start) { exit 0 } else { exit 1 }
    }
    'stop' {
        if (Invoke-Stop) { exit 0 } else { exit 1 }
    }
    'status' {
        Show-Status
        exit 0
    }
    'restart' {
        $stopOk = Invoke-Stop
        if ($stopOk -and (Invoke-Start)) { exit 0 } else { exit 1 }
    }
    default {
        Write-Usage
        exit 1
    }
}
