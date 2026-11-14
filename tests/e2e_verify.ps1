# 端到端验证：下载 llama.cpp 运行时 -> 下载 GGUF 模型 -> 加载 -> 对话
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$env:AETHER_HOME = "$root\.verify"
New-Item -ItemType Directory -Force -Path "$root\.verify" | Out-Null

$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
$port = 18800
$base = "http://127.0.0.1:$port"

function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }

$proc = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","$port" `
    -WorkingDirectory $root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "$root\.verify\srv.out" -RedirectStandardError "$root\.verify\srv.err"

$up = $false
for ($i = 0; $i -lt 80; $i++) {
    if (Test-NetConnection 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue) { $up = $true; break }
    Start-Sleep -Milliseconds 500
}
Log "server_up=$up"
if (-not $up) { Log "ABORT: 服务未启动"; Get-Content "$root\.verify\srv.err" -Tail 20; exit 1 }

# ---- 1. 下载 llama.cpp 运行时 ----
try {
    Invoke-RestMethod -Uri "$base/api/runtime/install" -Method Post -TimeoutSec 20 | Out-Null
    Log "已触发 llama.cpp 运行时下载"
} catch { Log "runtime install ERR: $($_.Exception.Message)" }

# ---- 2. 下载模型 ----
$modelFile = "qwen2.5-1.5b-instruct-q2_k.gguf"
try {
    $t = Invoke-RestMethod -Uri "$base/api/models/download" -Method Post `
        -ContentType "application/json" -TimeoutSec 20 `
        -Body (@{ repo = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"; file = $modelFile } | ConvertTo-Json)
    Log "已触发模型下载: task=$($t.task.id)"
} catch { Log "model download ERR: $($_.Exception.Message)" }

# ---- 3. 轮询直到运行时与模型都就绪 ----
$deadline = (Get-Date).AddMinutes(35)
$runtimeReady = $false
$modelReady = $false
$lastLog = ""
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    try {
        $sys = Invoke-RestMethod -Uri "$base/api/system" -TimeoutSec 15
        $binary = $sys.runtime.binary
        if ($binary -and (Test-Path $binary)) { $runtimeReady = $true }
    } catch {}
    try {
        $d = Invoke-RestMethod -Uri "$base/api/downloads" -TimeoutSec 15
        $tk = $d.tasks | Where-Object { $_.file -eq $modelFile } | Select-Object -First 1
        if ($tk) {
            $line = "model {0} {1:N0}/{2:N0} MB {3}% {4} MB/s | runtime={5}" -f `
                $tk.status, ($tk.done/1MB), ($tk.total/1MB), $tk.pct, $tk.speed_mbps, $runtimeReady
            if ($line -ne $lastLog) { Log $line; $lastLog = $line }
            if ($tk.status -eq "done") { $modelReady = $true }
            if ($tk.status -eq "failed") { Log "模型下载失败: $($tk.error)"; break }
        }
    } catch {}
    if ($runtimeReady -and $modelReady) { Log "运行时与模型均就绪"; break }
}

Log "runtimeReady=$runtimeReady modelReady=$modelReady"
if (-not $modelReady) {
    Log "ABORT: 模型未下载完成"
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---- 4. 加载模型 ----
try {
    $models = Invoke-RestMethod -Uri "$base/api/models" -TimeoutSec 20
    $m = $models.models | Select-Object -First 1
    Log "库中模型: $($m.display) key=$($m.key) size=$($m.size_gb)GB layers=$($m.meta.block_count) ctxTrain=$($m.meta.context_length)"
    Invoke-RestMethod -Uri "$base/api/models/load" -Method Post -ContentType "application/json" `
        -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
    Log "已触发加载: $($m.display)"
} catch { Log "load ERR: $($_.Exception.Message)"; Stop-Process -Id $proc.Id -Force; exit 1 }

$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 5
    try {
        $rt = Invoke-RestMethod -Uri "$base/api/runtime" -TimeoutSec 15
        if ($i % 3 -eq 0) { Log ("runtime state={0} params={1}" -f $rt.state, ($rt.params | ConvertTo-Json -Compress)) }
        if ($rt.state -eq "ready") { $ready = $true; break }
        if ($rt.state -eq "failed") { Log "加载失败: $($rt.error)"; break }
    } catch {}
}
Log "model_ready=$ready"
if (-not $ready) {
    Log "--- 运行日志尾部 ---"
    (Invoke-RestMethod -Uri "$base/api/runtime" -TimeoutSec 10).log_tail | Select-Object -Last 15 | ForEach-Object { Log "  $_" }
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---- 5. 对话验证 ----
try {
    $rt = Invoke-RestMethod -Uri "$base/api/runtime" -TimeoutSec 10
    Log "模型已就绪: $($rt.model) 端口=$($rt.port) 参数=$($rt.params | ConvertTo-Json -Compress)"
    $body = @{
        model = $rt.model
        messages = @(@{ role = "user"; content = "用一句话说明你是谁。" })
        max_tokens = 60
        stream = $false
    } | ConvertTo-Json -Depth 5
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $resp = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json" `
        -Body $body -TimeoutSec 180 -UseBasicParsing
    $sw.Stop()
    $route = $resp.Headers["X-Aether-Route"]
    $node = $resp.Headers["X-Aether-Node"]
    $json = $resp.Content | ConvertFrom-Json
    Log "对话成功 路由=$route 节点=$node 耗时=$([math]::Round($sw.Elapsed.TotalSeconds,1))s"
    Log "usage: prompt=$($json.usage.prompt_tokens) completion=$($json.usage.completion_tokens)"
    Log "回答: $($json.choices[0].message.content)"
} catch { Log "chat ERR: $($_.Exception.Message)" }

# ---- 6. 广播验证（本机模型这时应出现在广告里） ----
try {
    $adv = Invoke-RestMethod -Uri "$base/api/p2p/advertise" -Method Post -ContentType "application/json" `
        -TimeoutSec 30 -Body '{"enabled":true}'
    Log "广播: published_to=$($adv.relays) relay_connected=$($adv.relay_connected) models=$($adv.models.Count)"
    if ($adv.models.Count -gt 0) { Log "  广告中的模型: $($adv.models[0].id) ctx=$($adv.models[0].ctx)" }
} catch { Log "advertise ERR: $($_.Exception.Message)" }

$cat = Invoke-RestMethod -Uri "$base/api/p2p/catalog" -TimeoutSec 10
Log "统一目录条目数: $($cat.models.Count)"
$cat.models | ForEach-Object { Log ("  {0} kind={1} loaded={2}" -f $_.id, $_.kind, $_.loaded) }

Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Log "DONE"
