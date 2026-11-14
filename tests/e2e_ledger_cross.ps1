# 跨节点账本对账：A 提供模型（relay-only），B 经隧道调用，双方各记一笔且能按 req_id 对齐
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 500 }; return $false }

foreach ($d in @("$root\.ledger-a","$root\.ledger-b")) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Force -Path $d | Out-Null }
New-Item -ItemType Junction -Path "$root\.ledger-a\models" -Target "$root\data\models" | Out-Null
New-Item -ItemType Junction -Path "$root\.ledger-a\runtime" -Target "$root\data\runtime" | Out-Null
Copy-Item "$root\data\identity.json" "$root\.ledger-a\identity.json"
@{ api_host = "127.0.0.1"; api_port = 18910; advertise = $false; price_in = 0.15; price_out = 0.60 } |
    ConvertTo-Json | Set-Content "$root\.ledger-a\settings.json" -Encoding UTF8
@{ api_port = 18911; advertise = $false } | ConvertTo-Json | Set-Content "$root\.ledger-b\settings.json" -Encoding UTF8

$env:AETHER_HOME = "$root\.ledger-a"
$pA = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18910","--host","127.0.0.1" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.ledger-a\out.log" -RedirectStandardError "$root\.ledger-a\err.log"
$env:AETHER_HOME = "$root\.ledger-b"
$pB = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18911" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.ledger-b\out.log" -RedirectStandardError "$root\.ledger-b\err.log"
Log "A_up=$(Up 18910) B_up=$(Up 18911)"
$A = "http://127.0.0.1:18910"; $B = "http://127.0.0.1:18911"

# A 接入 B 的 relay（B 的内置 relay 作为汇聚点）
Invoke-RestMethod -Uri "$A/api/p2p/relays" -Method Post -ContentType "application/json" -TimeoutSec 15 `
    -Body '{"action":"add","url":"ws://127.0.0.1:18911/relay"}' | Out-Null

# A 加载模型并广播
$models = Invoke-RestMethod -Uri "$A/api/models" -TimeoutSec 20
$m = $models.models | Select-Object -First 1
Invoke-RestMethod -Uri "$A/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt = Invoke-RestMethod -Uri "$A/api/runtime" -TimeoutSec 10; if ($rt.state -in @('ready','failed')) { break } }
Log "A runtime=$($rt.state) model=$($rt.model)"
if ($rt.state -ne 'ready') { Log "A 加载失败: $($rt.error)"; Stop-Process -Id $pA.Id,$pB.Id -Force; exit 1 }
$adv = Invoke-RestMethod -Uri "$A/api/p2p/advertise" -Method Post -ContentType "application/json" -TimeoutSec 30 -Body '{"enabled":true}'
Log "A 广播: relays=$($adv.relays) models=$($adv.models.Count)"

# 等 B 发现 A
$peer = $null
for ($i=0; $i -lt 20; $i++) { Start-Sleep -Seconds 2; $ps = Invoke-RestMethod -Uri "$B/api/p2p/peers" -TimeoutSec 10; if ($ps.peers.Count -gt 0) { $peer = $ps.peers[0]; break } }
if (-not $peer) { Log "B 未发现节点"; Stop-Process -Id $pA.Id,$pB.Id -Force; exit 1 }
Log "B 发现: reach=$($peer.reachability) ep='$($peer.endpoint)' price=$($peer.price | ConvertTo-Json -Compress)"

# B 经隧道调用
$body = @{ model = $m.display; messages = @(@{ role='user'; content='只回答两个字：收到' }); max_tokens = 20; stream = $false } | ConvertTo-Json -Depth 5
$resp = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 200 -UseBasicParsing -SkipHttpErrorCheck
$reqId = [string]$resp.Headers['X-Aether-Req']
Log "B 请求: HTTP=$($resp.StatusCode) 路由=$($resp.Headers['X-Aether-Route']) req_id=$reqId"
$usage = ($resp.Content | ConvertFrom-Json).usage
Log "  上游 usage: $($usage.prompt_tokens)+$($usage.completion_tokens)"
Start-Sleep -Seconds 2

$bLed = Invoke-RestMethod -Uri "$B/api/ledger/entries?limit=10" -TimeoutSec 10
$aLed = Invoke-RestMethod -Uri "$A/api/ledger/entries?limit=10" -TimeoutSec 10
Log "B 账本（调用方）:"
foreach ($e in $bLed.entries) { Log ("  role={0} req={1} peer={2} in={3} out={4} amount={5} src={6}" -f $e.role, $e.req_id.Substring(0,8), $e.peer_node.Substring(0,8), $e.in_tok, $e.out_tok, $e.amount, $e.source) }
Log "A 账本（提供方）:"
foreach ($e in $aLed.entries) { Log ("  role={0} req={1} peer={2} in={3} out={4} amount={5} src={6}" -f $e.role, $e.req_id.Substring(0,8), $e.peer_node.Substring(0,8), $e.in_tok, $e.out_tok, $e.amount, $e.source) }

$bE = $bLed.entries | Where-Object { $_.source -eq 'relay' } | Select-Object -First 1
$aE = $aLed.entries | Where-Object { $_.source -eq 'relay' } | Select-Object -First 1
if ($bE -and $aE) {
    Log "对账: req_id 相同=$($bE.req_id -eq $aE.req_id) tokens 相同=$($bE.in_tok -eq $aE.in_tok -and $bE.out_tok -eq $aE.out_tok)"
    Log "  金额: B 记 $($bE.amount) / A 记 $($aE.amount)"
} else { Log "!! 缺少跨节点记录 B=$($bE -ne $null) A=$($aE -ne $null)" }

$bSum = Invoke-RestMethod -Uri "$B/api/ledger/summary" -TimeoutSec 10
$aSum = Invoke-RestMethod -Uri "$A/api/ledger/summary" -TimeoutSec 10
Log "B 汇总: consume=$($bSum.summary.consume.count) amount=$($bSum.summary.consume.amount) 信用=$($bSum.credit | ConvertTo-Json -Compress)"
Log "A 汇总: serve=$($aSum.summary.serve.count) amount=$($aSum.summary.serve.amount) 信用=$($aSum.credit | ConvertTo-Json -Compress)"

# 双方各自结算
$stB = Invoke-RestMethod -Uri "$B/api/ledger/settle" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body '{}'
$stA = Invoke-RestMethod -Uri "$A/api/ledger/settle" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body '{}'
if ($stB.created) { Log "B 结算: count=$($stB.batch.count) amount=$($stB.batch.amount) root=$($stB.batch.merkle_root.Substring(0,16))" }
if ($stA.created) { Log "A 结算: count=$($stA.batch.count) amount=$($stA.batch.amount) root=$($stA.batch.merkle_root.Substring(0,16))" }

Stop-Process -Id $pA.Id,$pB.Id -Force -ErrorAction SilentlyContinue
foreach ($d in @("$root\.ledger-a","$root\.ledger-b")) { Remove-Item "$d\models","$d\runtime" -Force -ErrorAction SilentlyContinue }
Log "DONE"
