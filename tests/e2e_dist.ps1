# 打包产物可用性验证
#
# 之前只用打包出来的 exe 探过 /healthz 和静态界面，那只能证明"能启动"。
# 这里验证它能不能真的跑完一条完整链路：加载模型 → 推理 → 记账。
# 这一步过了，"干净机器安装"才谈得上有意义。
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$exe = "$root\dist\AetherNode\AetherNode.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 90; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 400 }; return $false }

if (-not (Test-Path $exe)) { Log "找不到打包产物，先跑 build.ps1"; exit 2 }
Log ("产物 {0:N1} MB" -f ((Get-Item $exe).Length / 1MB))

$port = 19111
$procName = "AetherNode"
Get-Process $procName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# 复用仓库里已有的模型与运行时（打包产物自己不带这两样）
$env:AETHER_HOME = "$root\data"
$env:AETHER_INSECURE_TLS = "1"
$logDir = "$root\data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$proc = Start-Process -FilePath $exe -ArgumentList "--no-window", "--port", "$port" `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "$logDir\exe.out" -RedirectStandardError "$logDir\exe.err"
Log "已启动打包产物，等待就绪…"
$up = Up $port
Log "port_up=$up"
if (-not $up) {
    Log "启动失败，日志尾部："
    Get-Content "$logDir\exe.err" -Tail 25 -ErrorAction SilentlyContinue
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

$base = "http://127.0.0.1:$port"
$pass = $true

# 1) 基本端点
$sys = Invoke-RestMethod "$base/api/system" -TimeoutSec 20
Log "版本=$($sys.version) 模型数=$($sys.runtime.state)"
if ($sys.version -ne "0.2.0") { Log "!! 版本不符"; $pass = $false }

$idx = (Invoke-WebRequest "$base/" -UseBasicParsing).StatusCode
Log "静态界面 -> $idx"
if ($idx -ne 200) { Log "!! 界面不可用"; $pass = $false }

# 2) 加载模型
$ms = Invoke-RestMethod "$base/api/models" -TimeoutSec 20
$m = $ms.models | Select-Object -First 1
if (-not $m) {
    Log "!! 没有本地模型，无法验证推理链路"
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    exit 1
}
Log "加载模型: $($m.display)"
Invoke-RestMethod -Uri "$base/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null

$ready = $false
for ($i=0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    $rt = Invoke-RestMethod "$base/api/runtime" -TimeoutSec 10
    if ($rt.state -in @('ready','failed')) { $ready = ($rt.state -eq 'ready'); break }
}
Log "runtime=$($rt.state) 参数=$($rt.params | ConvertTo-Json -Compress)"
if (-not $ready) {
    Log "!! 打包产物加载模型失败: $($rt.error)"
    Get-Content "$logDir\exe.err" -Tail 15 -ErrorAction SilentlyContinue
    $pass = $false
}

# 3) 真实推理
if ($ready) {
    $body = @{ model = $rt.model; messages = @(@{ role='user'; content='只回答两个字：可用' }); max_tokens = 16; stream = $false } | ConvertTo-Json -Depth 5
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 180 -UseBasicParsing -SkipHttpErrorCheck
    $sw.Stop()
    if ($r.StatusCode -eq 200) {
        $j = $r.Content | ConvertFrom-Json
        Log ("推理成功: {0:N1}s usage={1}+{2} 回答={3}" -f $sw.Elapsed.TotalSeconds, $j.usage.prompt_tokens, $j.usage.completion_tokens, $j.choices[0].message.content)
    } else {
        Log "!! 推理失败 HTTP=$($r.StatusCode): $(([string]$r.Content).Substring(0,[Math]::Min(200,([string]$r.Content).Length)))"
        $pass = $false
    }

    # 4) 账本
    $sum = Invoke-RestMethod "$base/api/ledger/summary" -TimeoutSec 10
    Log "账本条目=$($sum.summary.total_entries) 版本=$($sum.schema_version)"
    if ($sum.summary.total_entries -lt 1) { Log "!! 推理没有记账"; $pass = $false }
}

# 5) 优雅停止
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

if ($pass) { Log "PASS 打包产物可完成完整推理链路" } else { Log "FAIL 打包产物存在缺陷" }
Log "DONE"
exit $(if ($pass) { 0 } else { 1 })
