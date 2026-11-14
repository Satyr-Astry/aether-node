# 跨节点路由验证：A 提供模型，B 无本地模型但能路由到 A
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 500 }; return $false }

# B 用独立身份，否则会把自己的广告当成回声过滤掉
Remove-Item "$root\.verify-b" -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$root\.verify-b" | Out-Null

$env:AETHER_HOME = "$root\.verify"
$pA = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18800" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.verify\a.out" -RedirectStandardError "$root\.verify\a.err"
$env:AETHER_HOME = "$root\.verify-b"
$pB = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18801" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.verify-b\b.out" -RedirectStandardError "$root\.verify-b\b.err"
Log "A_up=$(Up 18800) B_up=$(Up 18801)"

$A = "http://127.0.0.1:18800"
$B = "http://127.0.0.1:18801"

# A 加载模型并广播
$models = Invoke-RestMethod -Uri "$A/api/models" -TimeoutSec 20
$m = $models.models | Select-Object -First 1
Invoke-RestMethod -Uri "$A/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt = Invoke-RestMethod -Uri "$A/api/runtime" -TimeoutSec 10; if ($rt.state -eq 'ready' -or $rt.state -eq 'failed') { break } }
Log "A 运行时: state=$($rt.state) model=$($rt.model)"
if ($rt.state -ne 'ready') { Log "A 加载失败: $($rt.error)"; Stop-Process -Id $pA.Id,$pB.Id -Force; exit 1 }

$adv = Invoke-RestMethod -Uri "$A/api/p2p/advertise" -Method Post -ContentType "application/json" -TimeoutSec 30 -Body '{"enabled":true}'
Log "A 广播: relays=$($adv.relays) models=$($adv.models.Count)"

# B 连接 A 的 relay
try {
    Invoke-RestMethod -Uri "$B/api/p2p/relays" -Method Post -ContentType "application/json" -TimeoutSec 15 `
        -Body '{"action":"add","url":"ws://127.0.0.1:18800/relay"}' | Out-Null
    Log "B 已连接 A 的 relay"
} catch { Log "B relay ERR: $($_.Exception.Message)" }

# 等 B 发现 A
$found = $null
for ($i=0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    $peers = Invoke-RestMethod -Uri "$B/api/p2p/peers" -TimeoutSec 10
    if ($peers.peers.Count -gt 0) { $found = $peers.peers[0]; break }
}
if ($found) {
    Log "B 发现节点: $($found.name) reach=$($found.reachability) endpoint=$($found.endpoint) models=$($found.models.Count)"
    Log "  该节点提供: $(($found.models | ForEach-Object { $_.id + '(ctx ' + $_.ctx + ')' }) -join ', ')"
} else {
    Log "B 未发现节点"
    Stop-Process -Id $pA.Id,$pB.Id -Force
    exit 1
}

# B 的 /v1/models 应包含 A 的模型
$bm = Invoke-RestMethod -Uri "$B/v1/models" -TimeoutSec 15
Log "B /v1/models: $(($bm.data | ForEach-Object { $_.id + '[' + $_.owned_by + ']' }) -join ', ')"

# B 自己没有任何本地模型
$brt = Invoke-RestMethod -Uri "$B/api/runtime" -TimeoutSec 10
Log "B 本地运行时: state=$($brt.state) model=$($brt.model)"

# 关键：B 发对话，请求应被路由到 A
$body = @{
    model = $m.display
    messages = @(@{ role = "user"; content = "只回答两个字：收到。" })
    max_tokens = 20
    stream = $false
} | ConvertTo-Json -Depth 5
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $resp = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 240 -UseBasicParsing
    $sw.Stop()
    $j = $resp.Content | ConvertFrom-Json
    Log "跨节点对话 OK"
    Log "  路由=$($resp.Headers['X-Aether-Route']) 节点=$($resp.Headers['X-Aether-Node']) 模型=$($resp.Headers['X-Aether-Model']) 分数=$($resp.Headers['X-Aether-Score'])"
    Log "  耗时=$([math]::Round($sw.Elapsed.TotalSeconds,1))s tokens=$($j.usage.prompt_tokens)+$($j.usage.completion_tokens)"
    Log "  回答: $($j.choices[0].message.content)"
} catch { Log "跨节点对话失败: $($_.Exception.Message)" }

# 路由预览
try {
    $rp = Invoke-RestMethod -Uri "$B/api/route/preview" -Method Post -ContentType "application/json" -TimeoutSec 10 -Body (@{ model = $m.display } | ConvertTo-Json)
    Log "B 路由候选 $($rp.candidates.Count) 个:"
    $rp.candidates | ForEach-Object { Log ("  kind={0} node={1} score={2} note={3}" -f $_.kind, $_.node_name, $_.score, $_.note) }
} catch { Log "route preview ERR" }

# 流式也验一下
try {
    $sbody = @{ model = $m.display; messages = @(@{ role='user'; content='数到三。' }); max_tokens = 40; stream = $true } | ConvertTo-Json -Depth 5
    $sr = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $sbody -TimeoutSec 240 -UseBasicParsing
    $deltas = ([regex]::Matches($sr.Content, '"content":"([^"]*)"') | ForEach-Object { $_.Groups[1].Value }) -join ''
    Log "B 流式对话 OK 路由=$($sr.Headers['X-Aether-Route']) 片段数=$(([regex]::Matches($sr.Content,'data: ')).Count)"
    Log "  拼接: $deltas"
} catch { Log "流式失败: $($_.Exception.Message)" }

Stop-Process -Id $pA.Id,$pB.Id -Force -ErrorAction SilentlyContinue
Log "DONE"
