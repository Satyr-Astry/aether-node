# 贡献度与发放验证：A 提供服务并被隧道调用，relay 侧统计带宽，B 生成发放清单
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 90; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 500 }; return $false }

foreach ($d in @('.emis-a','.emis-b')) { Remove-Item "$root\$d" -Recurse -Force -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Force -Path "$root\$d" | Out-Null }
New-Item -ItemType Junction -Path "$root\.emis-a\models" -Target "$root\data\models" | Out-Null
New-Item -ItemType Junction -Path "$root\.emis-a\runtime" -Target "$root\data\runtime" | Out-Null
Copy-Item "$root\data\identity.json" "$root\.emis-a\identity.json"
@{ api_host = "127.0.0.1"; api_port = 18930; advertise = $false } | ConvertTo-Json | Set-Content "$root\.emis-a\settings.json" -Encoding UTF8
@{ api_port = 18931; advertise = $false } | ConvertTo-Json | Set-Content "$root\.emis-b\settings.json" -Encoding UTF8

$env:AETHER_HOME = "$root\.emis-a"
$pA = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18930","--host","127.0.0.1" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.emis-a\out.log" -RedirectStandardError "$root\.emis-a\err.log"
$env:AETHER_HOME = "$root\.emis-b"
$pB = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18931" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.emis-b\out.log" -RedirectStandardError "$root\.emis-b\err.log"
Log "A=$(Up 18930) B=$(Up 18931)"
$A = "http://127.0.0.1:18930"; $B = "http://127.0.0.1:18931"

Invoke-RestMethod -Uri "$A/api/p2p/relays" -Method Post -ContentType "application/json" -TimeoutSec 15 `
    -Body '{"action":"add","url":"ws://127.0.0.1:18931/relay"}' | Out-Null

$models = Invoke-RestMethod -Uri "$A/api/models" -TimeoutSec 20
$m = $models.models | Select-Object -First 1
Invoke-RestMethod -Uri "$A/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt = Invoke-RestMethod -Uri "$A/api/runtime" -TimeoutSec 10; if ($rt.state -in @('ready','failed')) { break } }
Log "A runtime=$($rt.state)"
if ($rt.state -ne 'ready') { Log "A 加载失败"; Stop-Process -Id $pA.Id,$pB.Id -Force; exit 1 }
$adv = Invoke-RestMethod -Uri "$A/api/p2p/advertise" -Method Post -ContentType "application/json" -TimeoutSec 30 -Body '{"enabled":true}'
Log "A 广播: relays=$($adv.relays)"

for ($i=0; $i -lt 20; $i++) { Start-Sleep -Seconds 2; $ps = Invoke-RestMethod -Uri "$B/api/p2p/peers" -TimeoutSec 10; if ($ps.peers.Count -gt 0) { break } }
Log "B 发现 $($ps.peers.Count) 个节点"

# 经隧道跑三次请求，产生服务贡献与带宽贡献
for ($k=0; $k -lt 3; $k++) {
    $body = @{ model = $m.display; messages = @(@{ role='user'; content="第 $k 问：说一句话。" }); max_tokens = 30; stream = $false } | ConvertTo-Json -Depth 5
    $r = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 200 -UseBasicParsing -SkipHttpErrorCheck
    Log "  请求 $k : HTTP=$($r.StatusCode) 路由=$($r.Headers['X-Aether-Route'])"
}
Start-Sleep -Seconds 2

$sumA = Invoke-RestMethod -Uri "$A/api/ledger/summary" -TimeoutSec 10
Log "A 服务贡献: serve=$($sumA.summary.serve.count) 次, tokens=$($sumA.summary.serve.in_tok)+$($sumA.summary.serve.out_tok)"

$bw = Invoke-RestMethod -Uri "$B/api/ledger/bandwidth" -TimeoutSec 10
Log "B relay 带宽统计:"
$bw.bandwidth.PSObject.Properties | ForEach-Object {
    Log "  $($_.Name.Substring(0,8)): out=$($_.Value.forwarded_out)B in=$($_.Value.forwarded_in)B msgs=$($_.Value.messages)"
}

# A 侧：自己的服务贡献
$emA = Invoke-RestMethod -Uri "$A/api/ledger/emission/current" -TimeoutSec 15
Log "A 的发放预览: epoch=$($emA.epoch) emission=$($emA.emission) points=$($emA.total_points)"
foreach ($a in $emA.allocations) { Log "  node=$($a.node.Substring(0,8)) points=$($a.points) share=$($a.share) amount=$($a.amount) tokens=$($a.serve_tokens) bytes=$($a.relay_bytes)" }

# B 侧：作为 relay，看得到带宽贡献
$emB = Invoke-RestMethod -Uri "$B/api/ledger/emission/current" -TimeoutSec 15
Log "B 的发放预览（relay 视角）: epoch=$($emB.epoch) emission=$($emB.emission) points=$($emB.total_points)"
foreach ($a in $emB.allocations) { Log "  node=$($a.node.Substring(0,8)) points=$($a.points) bytes=$($a.relay_bytes) msgs=$($a.relayed_messages)" }

# B 落盘一份发放清单并校验
$pub = Invoke-RestMethod -Uri "$B/api/ledger/emission/publish" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body '{}'
if ($pub.created) {
    Log "B 已发布清单: epoch=$($pub.plan.epoch) root=$($pub.plan.merkle_root.Substring(0,16)) 分配数=$($pub.plan.allocations.Count)"
    $vf = Invoke-RestMethod -Uri "$B/api/ledger/emission/verify" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{ path = $pub.plan.path } | ConvertTo-Json)
    Log "  清单校验: ok=$($vf.ok) 分配数=$($vf.allocations)"
} else { Log "发布: $($pub.reason)" }

$plans = Invoke-RestMethod -Uri "$B/api/ledger/emission/plans" -TimeoutSec 10
Log "B 的清单列表: $($plans.plans.Count) 份"

Stop-Process -Id $pA.Id,$pB.Id -Force -ErrorAction SilentlyContinue
foreach ($d in @('.emis-a')) { Remove-Item "$root\$d\models","$root\$d\runtime" -Force -ErrorAction SilentlyContinue }
Log "DONE"
