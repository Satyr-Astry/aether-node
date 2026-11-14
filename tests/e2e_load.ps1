# 加载已验证下载的模型并跑对话/广播（模型与运行时已在 .verify 中）
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$env:AETHER_HOME = "$root\.verify"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
$port = 18800
$base = "http://127.0.0.1:$port"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }

$proc = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","$port" `
    -WorkingDirectory $root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "$root\.verify\srv2.out" -RedirectStandardError "$root\.verify\srv2.err"
$up = $false
for ($i = 0; $i -lt 80; $i++) {
    if (Test-NetConnection 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue) { $up = $true; break }
    Start-Sleep -Milliseconds 500
}
Log "server_up=$up"
if (-not $up) { Log "ABORT"; exit 1 }

$models = Invoke-RestMethod -Uri "$base/api/models" -TimeoutSec 20
$m = $models.models | Select-Object -First 1
Log "模型: $($m.display) 大小=$($m.size_gb)GB 层数=$($m.meta.block_count) 原生ctx=$($m.meta.context_length)"

Invoke-RestMethod -Uri "$base/api/models/load" -Method Post -ContentType "application/json" `
    -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
Log "已触发加载"

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 5
    try {
        $rt = Invoke-RestMethod -Uri "$base/api/runtime" -TimeoutSec 15
        Log ("state={0}" -f $rt.state)
        if ($rt.state -eq "ready") { $ready = $true; break }
        if ($rt.state -eq "failed") {
            Log "失败: $($rt.error)"
            Log "--- 日志 ---"
            $rt.log_tail | Select-Object -Last 20 | ForEach-Object { Log "  $_" }
            break
        }
    } catch { Log "poll ERR $($_.Exception.Message)" }
}
Log "ready=$ready"
if (-not $ready) { Stop-Process -Id $proc.Id -Force; exit 1 }

$rt = Invoke-RestMethod -Uri "$base/api/runtime" -TimeoutSec 10
Log "运行时: 模型=$($rt.model) 端口=$($rt.port) 参数=$($rt.params | ConvertTo-Json -Compress)"

# 对话
$body = @{
    model = $rt.model
    messages = @(@{ role = "user"; content = "用一句话介绍你自己。" })
    max_tokens = 50
    stream = $false
} | ConvertTo-Json -Depth 5
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $resp = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json" `
        -Body $body -TimeoutSec 240 -UseBasicParsing
    $sw.Stop()
    $j = $resp.Content | ConvertFrom-Json
    Log "对话 OK 路由=$($resp.Headers['X-Aether-Route']) 节点=$($resp.Headers['X-Aether-Node']) 耗时=$([math]::Round($sw.Elapsed.TotalSeconds,1))s"
    Log "tokens: prompt=$($j.usage.prompt_tokens) completion=$($j.usage.completion_tokens)"
    Log "回答: $($j.choices[0].message.content)"
} catch { Log "对话失败: $($_.Exception.Message)" }

# /v1/models 应列出本地模型
try {
    $vm = Invoke-RestMethod -Uri "$base/v1/models" -TimeoutSec 15
    Log "/v1/models: $(($vm.data | ForEach-Object { $_.id }) -join ', ')"
} catch { Log "/v1/models ERR" }

# 广播（此时广告应带模型）
try {
    $adv = Invoke-RestMethod -Uri "$base/api/p2p/advertise" -Method Post -ContentType "application/json" `
        -TimeoutSec 30 -Body '{"enabled":true}'
    Log "广播: relays=$($adv.relays) connected=$($adv.relay_connected) models=$($adv.models.Count)"
    if ($adv.models.Count -gt 0) { Log "  广告模型: $($adv.models[0].id) ctx=$($adv.models[0].ctx) quant=$($adv.models[0].quant)" }
} catch { Log "广播 ERR: $($_.Exception.Message)" }

$cat = Invoke-RestMethod -Uri "$base/api/p2p/catalog" -TimeoutSec 10
Log "统一目录: $($cat.models.Count) 条"
$cat.models | ForEach-Object { Log ("  {0} kind={1} loaded={2}" -f $_.id, $_.kind, $_.loaded) }

# 路由预览（证明路由层可用）
try {
    $rp = Invoke-RestMethod -Uri "$base/api/route/preview" -Method Post -ContentType "application/json" `
        -TimeoutSec 10 -Body (@{ model = $rt.model } | ConvertTo-Json)
    Log "路由候选: $($rp.candidates.Count) 个，首选 kind=$($rp.chosen.kind) score=$($rp.chosen.score)"
} catch { Log "route preview ERR" }

Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Log "DONE"
