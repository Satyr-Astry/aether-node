# 中继隧道验证：A 只监听回环（模拟 NAT 后不可直达），B 无本地模型，经 relay 隧道调用 A
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 500 }; return $false }

Remove-Item "$root\.tunnel-b" -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$root\.tunnel-b" | Out-Null

# A：复用 data（已有模型与运行时），只监听 127.0.0.1 => 广告里 reach=relay-only 且不公布端点
$env:AETHER_HOME = "$root\data"
$pA = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18800","--host","127.0.0.1" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\data\logs\tA.out" -RedirectStandardError "$root\data\logs\tA.err"
# B：独立身份。这里 B 的 relay 扮演"公共汇聚点"角色，A 和 B 都是它的客户端
$env:AETHER_HOME = "$root\.tunnel-b"
$pB = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18801" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.tunnel-b\out.log" -RedirectStandardError "$root\.tunnel-b\err.log"
Log "A_up=$(Up 18800) B_up=$(Up 18801)"

$A = "http://127.0.0.1:18800"
$B = "http://127.0.0.1:18801"

# A 接入 B 的 relay（模拟两者都连同一个汇聚 relay）
try {
    Invoke-RestMethod -Uri "$A/api/p2p/relays" -Method Post -ContentType "application/json" -TimeoutSec 15 `
        -Body '{"action":"add","url":"ws://127.0.0.1:18801/relay"}' | Out-Null
    Log "A 已接入汇聚 relay（B 的内置 relay）"
} catch { Log "A relay ERR: $($_.Exception.Message)" }

# A 加载模型
$models = Invoke-RestMethod -Uri "$A/api/models" -TimeoutSec 20
$m = $models.models | Select-Object -First 1
Log "A 模型: $($m.display)"
Invoke-RestMethod -Uri "$A/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt = Invoke-RestMethod -Uri "$A/api/runtime" -TimeoutSec 10; if ($rt.state -in @('ready','failed')) { break } }
Log "A 运行时 state=$($rt.state)"
if ($rt.state -ne 'ready') { Log "A 加载失败: $($rt.error)"; Stop-Process -Id $pA.Id,$pB.Id -Force; exit 1 }

# A 广播
$adv = Invoke-RestMethod -Uri "$A/api/p2p/advertise" -Method Post -ContentType "application/json" -TimeoutSec 30 -Body '{"enabled":true}'
Log "A 广播: relays=$($adv.relays) models=$($adv.models.Count)"

# B 无需再加 relay：它默认就连自己的内置 relay（即汇聚点）

# 等 B 发现 A，并确认它是 relay-only 且没有端点
$peer = $null
for ($i=0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    $ps = Invoke-RestMethod -Uri "$B/api/p2p/peers" -TimeoutSec 10
    if ($ps.peers.Count -gt 0) { $peer = $ps.peers[0]; break }
}
if (-not $peer) { Log "B 未发现节点"; Stop-Process -Id $pA.Id,$pB.Id -Force; exit 1 }
Log "B 发现: name=$($peer.name) reach=$($peer.reachability) endpoint='$($peer.endpoint)' models=$($peer.models.Count)"
if ($peer.reachability -ne 'relay-only') { Log "!! 期望 relay-only，实际 $($peer.reachability)" }

# B 的模型目录应含 A 的模型
$bm = Invoke-RestMethod -Uri "$B/v1/models" -TimeoutSec 15
Log "B /v1/models: $(($bm.data | ForEach-Object { $_.id + '[' + $_.owned_by + ']' }) -join ', ')"
$bcat = Invoke-RestMethod -Uri "$B/api/p2p/catalog" -TimeoutSec 15
$bcat.models | ForEach-Object { Log "  catalog: $($_.id) note=$($_.note)" }

# 路由候选应为 relay 类型
$rp = Invoke-RestMethod -Uri "$B/api/route/preview" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{ model = $m.display } | ConvertTo-Json)
Log "B 路由候选 $($rp.candidates.Count):"
$rp.candidates | ForEach-Object { Log ("  kind={0} node={1} score={2} note={3}" -f $_.kind, $_.node_name, $_.score, $_.note) }

# 关键：经隧道调用
$body = @{ model = $m.display; messages = @(@{ role='user'; content='只回答两个字：通了。' }); max_tokens = 20; stream = $false } | ConvertTo-Json -Depth 5
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $resp = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 200 -UseBasicParsing -SkipHttpErrorCheck
    $sw.Stop()
    Log "隧道对话 HTTP=$($resp.StatusCode) 路由=$($resp.Headers['X-Aether-Route']) 节点=$($resp.Headers['X-Aether-Node']) 耗时=$([math]::Round($sw.Elapsed.TotalSeconds,1))s"
    $c = [string]$resp.Content
    if ($resp.StatusCode -eq 200) {
        $j = $c | ConvertFrom-Json
        Log "  tokens=$($j.usage.prompt_tokens)+$($j.usage.completion_tokens)"
        Log "  回答: $($j.choices[0].message.content)"
    } else {
        Log "  失败详情: $($c.Substring(0, [Math]::Min(500, $c.Length)))"
    }
} catch { Log "隧道对话异常: $($_.Exception.Message)" }

# 流式也要过隧道
try {
    $sbody = @{ model = $m.display; messages = @(@{ role='user'; content='数到二。' }); max_tokens = 30; stream = $true } | ConvertTo-Json -Depth 5
    $sr = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $sbody -TimeoutSec 200 -UseBasicParsing
    $deltas = ([regex]::Matches($sr.Content, '"content":"([^"]*)"') | ForEach-Object { $_.Groups[1].Value }) -join ''
    Log "隧道流式: 路由=$($sr.Headers['X-Aether-Route']) 片段=$(([regex]::Matches($sr.Content,'data: ')).Count) 拼接=$deltas"
} catch { Log "隧道流式失败: $($_.Exception.Message)" }

# relay 侧的统计应显示转发
$sysA = Invoke-RestMethod -Uri "$A/api/system" -TimeoutSec 10
Log "A relay: nodes_registered=$($sysA.relay.nodes_registered) tunnel_relayed=$($sysA.relay.tunnel_relayed) denied=$($sysA.relay.tunnel_denied)"
$stB = Invoke-RestMethod -Uri "$B/api/p2p/status" -TimeoutSec 10
Log "B tunnel: requests_out=$($stB.tunnel.requests_out) errors=$($stB.tunnel.errors)"
$stA = Invoke-RestMethod -Uri "$A/api/p2p/status" -TimeoutSec 10
Log "A tunnel: served=$($stA.tunnel.served) forwarded_in=$($stA.tunnel.forwarded_in) errors=$($stA.tunnel.errors) last_status=$($stA.tunnel.last_status) body=$($stA.tunnel.last_body)"

# relay 侧是否真的收到过 A 的身份注册
try {
    $hist = Invoke-RestMethod -Uri "$A/api/events/history?limit=300" -TimeoutSec 10
    $hellos = $hist.events | Where-Object { $_.kind -in @('relay.hello','relay.hello_fail','tunnel.error','relay.error','relay.ws_closed') }
    if ($hellos) {
        foreach ($h in $hellos) { Log "  A event: kind=$($h.kind) payload=$($h.payload | ConvertTo-Json -Compress)" }
    } else {
        Log "  A 无 relay.hello / tunnel.error 事件（说明 A 自己从未发出或从未收到 HELLO）"
    }
} catch { Log "history ERR: $($_.Exception.Message)" }

Stop-Process -Id $pA.Id,$pB.Id -Force -ErrorAction SilentlyContinue
Log "DONE"
