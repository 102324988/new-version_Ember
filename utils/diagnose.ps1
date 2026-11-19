param(
    [ValidateSet('Auto', 'Service', 'Local')][string]$Mode = 'Auto',
    [string]$ConfigFile = '',
    [ValidatePattern('^[a-z0-9][a-z0-9_-]*$')][string]$ProjectName = 'ember-service',
    [switch]$Network
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$rows = New-Object System.Collections.ArrayList
$stamp = (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$destination = Join-Path $root ('output/diagnostics/' + $stamp)
New-Item -ItemType Directory -Path $destination -Force | Out-Null

function Add-Check($Status, $Name, $Detail, $Fix = '') {
    [void]$rows.Add([pscustomobject]@{ status = $Status; check = $Name; detail = $Detail; fix = $Fix })
    $color = switch ($Status) { 'FAIL' { 'Red' }; 'WARN' { 'Yellow' }; 'PASS' { 'Green' }; default { 'Gray' } }
    Write-Host ("[$Status] $Name - $Detail") -ForegroundColor $color
    if ($Fix -and $Status -in @('FAIL', 'WARN')) { Write-Host ('  建议：' + $Fix) }
}

function Quote-Native([string]$Value) {
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\*)"', '$1$1\"'), '(\+)$', '$1$1') + '"'
}

function Invoke-Bounded([string]$Program, [string[]]$Arguments, [int]$Seconds = 15, [string]$InputText = '') {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Program
    $info.Arguments = (($Arguments | ForEach-Object { Quote-Native $_ }) -join ' ')
    $info.WorkingDirectory = $root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($InputText) {
            $write = $process.StandardInput.WriteAsync($InputText)
            if (-not $write.Wait(5000)) {
                try { $process.Kill() } catch { }
                return @{ code = -1; output = ''; error = ''; timedOut = $true }
            }
        }
        $process.StandardInput.Close()
        if (-not $process.WaitForExit($Seconds * 1000)) {
            try { $process.Kill() } catch { }
            [void]$process.WaitForExit(2000)
            return @{ code = -1; output = ''; error = ''; timedOut = $true }
        }
        return @{ code = $process.ExitCode; output = $stdout.Result; error = $stderr.Result; timedOut = $false }
    } catch {
        return @{ code = -2; output = ''; error = ''; timedOut = $false }
    } finally { $process.Dispose() }
}

function Read-Config([string]$Path) {
    $values = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
            if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
                $name, $value = $Matches[1], $Matches[2].Trim()
                if ($value -match '^"([^"\r\n]*)"\s*(?:#.*)?$' -or $value -match "^'([^'\r\n]*)'\s*(?:#.*)?$") { $value = $Matches[1] }
                else { $value = $value -replace '\s+#.*$', '' }
                $values[$name] = $value
            }
        }
    }
    return $values
}

function Test-Http([string]$Url, [string]$Name) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -MaximumRedirection 0
        Add-Check 'PASS' $Name ('HTTP ' + [int]$response.StatusCode)
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        if ($status -eq 503) { Add-Check 'FAIL' $Name 'HTTP 503：服务未就绪。' '结合数据库、向量扩展、配置和日志分类结果排查；刚启动时可稍等后重试。' }
        elseif ($status -eq 401 -or $status -eq 403) { Add-Check 'WARN' $Name '请求被认证或来源规则拒绝。' '检查当前访问地址与服务配置；诊断不会尝试登录或修改会话。' }
        else { Add-Check 'FAIL' $Name '地址无法访问或响应异常。' '确认应用已启动，端口和绑定地址正确，且没有其他程序占用。' }
    }
}

Write-Host 'Ember 一键诊断（只读，不启动/停止业务服务，不调用生成模型）' -ForegroundColor Cyan
try {
    if ($Mode -eq 'Auto') { $Mode = if (Test-Path -LiteralPath (Join-Path $root 'deploy/service.env')) { 'Service' } else { 'Local' } }
    if (-not $ConfigFile) { $ConfigFile = if ($Mode -eq 'Service') { 'deploy/service.env' } else { '.env' } }
    if (-not [IO.Path]::IsPathRooted($ConfigFile)) { $ConfigFile = Join-Path $root $ConfigFile }
    $config = Read-Config $ConfigFile
    Add-Check 'INFO' 'mode' $Mode
    if (Test-Path -LiteralPath $ConfigFile) { Add-Check 'PASS' 'config-file' '配置文件存在；不会打印配置内容。' }
    else { Add-Check 'FAIL' 'config-file' '找不到所选模式配置。' '首次使用请按 README 配置；已有数据时恢复原配置，不要重新生成数据库密码。' }
    $required = @('LARGE_LLM_API_KEY','LARGE_LLM_BASE_URL','LARGE_LLM_MODEL','SMALL_LLM_API_KEY','SMALL_LLM_BASE_URL','SMALL_LLM_MODEL','EMBEDDING_API_KEY','EMBEDDING_BASE_URL','EMBEDDING_MODEL','PG_PASSWORD')
    if ($Mode -eq 'Service') { $required += 'EMBER_ACCESS_CODE_HASH' }
    $missing = @($required | Where-Object { -not $config[$_] -or $config[$_] -match '^(your_|replace_|changeme)' })
    if ($missing.Count) { Add-Check 'FAIL' 'config-fields' ('未填写：' + ($missing -join ', ')) '补齐实际使用的配置，再重启相应后端。' }
    else { Add-Check 'PASS' 'config-fields' '必需字段已填写；不代表密钥权限或额度有效。' }
    $probe = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'diagnostic_probe.py'), [Text.Encoding]::UTF8)
    # Windows PowerShell 5 may use a legacy code page for child stdin. Send an
    # ASCII envelope so Chinese probe literals arrive intact inside Linux.
    $encodedProbe = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($probe))
    $probe = "import base64; exec(compile(base64.b64decode('$encodedProbe'), '<ember-diagnostic>', 'exec'))"
    $probeResult = $null
    $port = if ($Mode -eq 'Service') { 8080 } else { 8000 }
    if ($Mode -eq 'Service') {
        $parsedPort = 0
        if ($config['EMBER_SERVICE_PORT']) {
            if ([int]::TryParse($config['EMBER_SERVICE_PORT'], [ref]$parsedPort) -and $parsedPort -ge 1 -and $parsedPort -le 65535) { $port = $parsedPort }
            else { Add-Check 'FAIL' 'service-port' 'EMBER_SERVICE_PORT 无效。' '设置为 1 到 65535 的端口。' }
        }
        $docker = Get-Command docker -ErrorAction SilentlyContinue
        if (-not $docker) { Add-Check 'FAIL' 'docker' '未安装 Docker 或不在 PATH 中。' '容器模式需要 Docker Desktop；本机模式可用 -Mode Local。' }
        else {
            $engine = Invoke-Bounded $docker.Source @('info', '--format', '{{.ServerVersion}}')
            if ($engine.code -ne 0) { Add-Check 'FAIL' 'docker-engine' 'Docker 引擎不可用或响应超时。' '手动打开 Docker Desktop，等待引擎就绪后重试；脚本不会替你重启服务。' }
            else {
                Add-Check 'PASS' 'docker-engine' 'Docker 引擎可访问。'
                $compose = Invoke-Bounded $docker.Source @('compose', 'version', '--short')
                Add-Check $(if ($compose.code -eq 0) { 'PASS' } else { 'FAIL' }) 'compose' $(if ($compose.code -eq 0) { 'Compose 可用。' } else { 'Compose 不可用，请修复 Docker Desktop。' })
                if (Test-Path -LiteralPath $ConfigFile) {
                    $syntax = Invoke-Bounded $docker.Source @('compose', '--env-file', $ConfigFile, '-f', (Join-Path $root 'compose.service.yml'), '-p', $ProjectName, 'config', '--quiet')
                    Add-Check $(if ($syntax.code -eq 0) { 'PASS' } else { 'FAIL' }) 'compose-config' $(if ($syntax.code -eq 0) { 'Compose 配置可解析。' } else { 'Compose 配置无法解析；检查配置缺项与 compose.service.yml。' })
                }
                foreach ($component in @('postgres', 'ember')) {
                    $container = $ProjectName + '-' + $component + '-1'
                    $inspection = Invoke-Bounded $docker.Source @('inspect', '--format', '{{json .State}}', $container)
                    $running = $false
                    if ($inspection.code -eq 0) {
                        try { $state = $inspection.output | ConvertFrom-Json; $running = $state.Running -eq $true } catch { }
                    }
                    if ($running) { Add-Check 'PASS' ('container:' + $component) '容器运行中。' }
                    else { Add-Check 'FAIL' ('container:' + $component) '容器不存在、已退出或无法检查。' '运行 start_service.bat；使用自定义项目名时传入 -ProjectName。' }
                    if ($component -eq 'ember' -and $running) {
                        $arguments = @('exec', '-i', $container, 'python', '-')
                        if ($Network) { $arguments += '--network' }
                        $probeResult = Invoke-Bounded $docker.Source $arguments 90 $probe
                        $logs = Invoke-Bounded $docker.Source @('logs', '--tail', '200', $container) 10
                        $categories = @(
                            @('log:db-auth', 'password authentication failed|28P01', '日志发现数据库认证错误。', '核对实际数据库用户密码，保留原数据卷。'),
                            @('log:vector', 'extension.*vector.*(?:available|exist)|向量.*初始化失败', '日志发现向量初始化错误。', '检查 pgvector 镜像、扩展和目标数据库。'),
                            @('log:model-network', 'APITimeoutError|ConnectTimeout|ReadTimeout|APIConnectionError', '日志发现接口连接或超时错误。', '检查网络、代理和模型端点，可额外运行 -Network。'),
                            @('log:tts', '403.*(?:speech|tts)|(?:tts|语音).*(?:失败|error|forbidden)', '日志发现语音服务错误。', '检查 edge-tts 版本与网络；手动重播正常而自动无声时检查网页声音权限。'),
                            @('log:error', 'Traceback|ERROR', '最近日志存在异常记录，可能包含已恢复的历史故障。', '结合当前 FAIL 项定位；脚本只导出分类，不复制聊天和原始日志。'))
                        foreach ($category in $categories) {
                            if (($logs.output + $logs.error) -match $category[1]) { Add-Check 'WARN' $category[0] $category[2] $category[3] }
                        }
                        $logs = $null
                    }
                }
            }
        }
    } else {
        $candidates = @()
        if ($env:EMBER_PYTHON) { $candidates += $env:EMBER_PYTHON }
        $candidates += (Join-Path $root '.venv/Scripts/python.exe')
        $python = Get-Command python -ErrorAction SilentlyContinue
        if ($python) { $candidates += $python.Source }
        $selected = $null
        foreach ($candidate in $candidates | Select-Object -Unique) {
            $version = Invoke-Bounded $candidate @('--version') 5
            if ($version.code -eq 0) { $selected = $candidate; break }
        }
        if (-not $selected) { Add-Check 'FAIL' 'host-python' '未找到可运行的 Python。' '安装 Python 3.11+，或把 EMBER_PYTHON 指向实际 python.exe。' }
        else {
            # Use a separate process only; dotenv never modifies this PowerShell session.
            $arguments = @((Join-Path $PSScriptRoot 'diagnostic_probe.py'), '--local', '--config', $ConfigFile)
            if ($Network) { $arguments += '--network' }
            $probeResult = Invoke-Bounded $selected $arguments 90
        }
    }
    if ($probeResult) {
        try {
            if ($probeResult.code -ne 0 -or $probeResult.timedOut -or -not $probeResult.output.Trim()) { throw 'probe' }
            $items = ConvertFrom-Json -InputObject $probeResult.output
            if ($null -eq $items -or -not @($items).Count) { throw 'probe' }
            foreach ($item in $items) {
                if ($item.status -notin @('PASS','WARN','FAIL','INFO') -or -not $item.check) { throw 'probe' }
                Add-Check $item.status $item.check $item.detail $item.fix
            }
        } catch { Add-Check 'FAIL' 'runtime-probe' '运行环境探针未返回有效结果或超过90秒。' '检查应用容器/Python；可先根据前面的环境与配置失败项处理。' }
    } else { Add-Check 'WARN' 'runtime-probe' '未进入运行环境，数据库及依赖检查未执行。' '先修复前面的 Docker 或 Python 问题，再运行诊断。' }
    Test-Http ("http://127.0.0.1:$port/health/live") 'http:live'
    Test-Http ("http://127.0.0.1:$port/health/ready") 'http:ready'
    Add-Check 'INFO' 'browser-audio' '浏览器自动播放权限无法由命令行确认。' '开关已开、手动重播正常但自动无声时，先点击页面并检查站点声音权限。'
    Add-Check 'INFO' 'model-scope' '没有调用生成/向量/语音接口；不验证模型密钥、额度或具体模型权限。' '需要人工核对供应商错误码；报告中的端点可达不代表业务调用一定成功。'
} catch {
    Add-Check 'FAIL' 'diagnostic-runner' '诊断脚本意外中止，已保留前面完成的检查。' '检查脚本文件是否完整及报告目录是否可写；不会导出原始异常文本。'
} finally {
    $failures = @($rows | Where-Object { $_.status -eq 'FAIL' }).Count
    $warnings = @($rows | Where-Object { $_.status -eq 'WARN' }).Count
    $report = [ordered]@{ version = 1; mode = $Mode; generated_at = (Get-Date -Format 'o'); failures = $failures; warnings = $warnings; checks = @($rows) }
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $destination 'report.json'), ($report | ConvertTo-Json -Depth 8), $utf8)
    $lines = @('Ember 诊断报告', ('失败：' + $failures + '；提示：' + $warnings), '只读检查，不包含配置值、聊天正文或原始日志。', '')
    foreach ($row in $rows) {
        $lines += ('[' + $row.status + '] ' + $row.check + ': ' + $row.detail)
        if ($row.fix) { $lines += ('  建议：' + $row.fix) }
    }
    [IO.File]::WriteAllText((Join-Path $destination 'report.txt'), ($lines -join [Environment]::NewLine), $utf8)
    Write-Host ("`n完成：$failures 项失败，$warnings 项提示。") -ForegroundColor Cyan
    Write-Host ('报告：' + (Join-Path $destination 'report.txt'))
    Write-Host '修复建议见报告。不会自动清库、重启容器、安装依赖或修改配置。'
}
if ($failures -gt 0) { exit 1 }
exit 0
