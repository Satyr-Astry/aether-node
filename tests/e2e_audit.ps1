# 抽样复算验证：两个提供同模型的节点 + 一个纯调用方，比对输出一致性
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 90; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 500 }; return $false }

foreach ($d in @('.audit-a','.audit-c','.audit-b')) { Remove-Item "$root\$d" -Recurse -Force -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Force -Path "$root\$d" | Out-Null }
foreach ($d in @('.audit-a','.audit-c')) {
    New-Item -ItemType Junction -Path "$root\$d\models" -Target "$root\data\models" | Out-Null
    New-Item -ItemType Junction -Path "$root\$d\runtime" -Target "$root\data\runtime" | Out-Null
}
@{ api_port = 18920; advertise = $false; price_in = 0.1; price_out = 0.4 } | ConvertTo-Json | Set-Content "$root\.audit-a\settings.json" -Encoding UTF8
@{ api_port = 18921; advertise = $false; price_in = 0.1; price_out = 0.4 } | ConvertTo-Json | Set-Content "$root\.audit-c\settings.json" -Encoding UTF8
@{ api_port = 18922; advertise = $false } | ConvertTo-Json | Set-Content "$root\.audit-b\settings.json" -Encoding UTF8

$env:AETHER_HOME = "$root\.audit-a"
$pA = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18920" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.audit-a\out.log" -RedirectStandardError "$root\.audit-a\err.log"
$env:AETHER_HOME = "$root\.audit-c"
$pC = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18921" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.audit-c\out.log" -RedirectStandardError "$root\.audit-c\err.log"
$env:AETHER_HOME = "$root\.audit-b"
$pB = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18922" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.audit-b\out.log" -RedirectStandardError "$root\.audit-b\err.log"
Log "A=$(Up 18920) C=$(Up 18921) B=$(Up 18922)"

$A = "http://127.0.0.1:18920"; $C = "http://127.0.0.1:18921"; $B = "http://127.0.0.1:18922"

# A 与 C 都接入 B 的内置 relay（B 作为汇聚点）
foreach ($n in @(@{n='A';u=$A}, @{n='C';u=$C})) {
    try {
        Invoke-RestMethod -Uri "$($n.u)/api/p2p/relays" -Method Post -ContentType "application/json" -TimeoutSec 15 `
            -Body '{"action":"add","url":"ws://127.0.0.1:18922/relay"}' | Out-Null
        Log "$($n.n) 接入汇聚 relay"
    } catch { Log "$($n.n) relay ERR: $($_.Exception.Message)" }
}

# 两个提供者各自加载模型并广播
foreach ($n in @(@{n='A';u=$A}, @{n='C';u=$C})) {
    $ms = Invoke-RestMethod -Uri "$($n.u)/api/models" -TimeoutSec 20
    $m = $ms.models | Select-Object -First 1
    Invoke-RestMethod -Uri "$($n.u)/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
}
foreach ($n in @(@{n='A';u=$A}, @{n='C';u=$C})) {
    $ready = $false
    for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt = Invoke-RestMethod -Uri "$($n.u)/api/runtime" -TimeoutSec 10; if ($rt.state -in @('ready','failed')) { $ready = ($rt.state -eq 'ready'); break } }
    Log "$($n.n) runtime=$($rt.state) model=$($rt.model)"
    if ($ready) { $adv = Invoke-RestMethod -Uri "$($n.u)/api/p2p/advertise" -Method Post -ContentType "application/json" -TimeoutSec 30 -Body '{"enabled":true}'; Log "  $($n.n) 广播: relays=$($adv.relays) models=$($adv.models.Count)" }
}

# 等 B 看到两个提供者
$peers = @()
for ($i=0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    $ps = Invoke-RestMethod -Uri "$B/api/p2p/peers" -TimeoutSec 10
    $peers = @($ps.peers | Where-Object { $_.models.Count -gt 0 })
    if ($peers.Count -ge 2) { break }
}
Log "B 看到的提供者: $($peers.Count) 个"
foreach ($p in $peers) { Log "  $($p.name) reach=$($p.reachability) models=$($p.models.Count)" }
if ($peers.Count -lt 2) { Log "!! 提供者不足 2 个，无法两两比对"; Stop-Process -Id $pA.Id,$pB.Id,$pC.Id -Force; exit 1 }

$cat = Invoke-RestMethod -Uri "$B/api/p2p/catalog" -TimeoutSec 10
Log "B 的统一目录: $($cat.models.Count) 条"

# 手动复算：向两个提供者各跑一次
try {
    $body = @{ model = ($peers[0].models[0].id); messages = @(@{ role='user'; content='1+1 等于几？只回答数字。' }); max_tokens = 20 } | ConvertTo-Json -Depth 5
    $probe = Invoke-RestMethod -Uri "$B/api/ledger/audit/probe" -Method Post -ContentType "application/json" -TimeoutSec 200 -Body $body
    Log "复算结果（阈值 $($probe.threshold)）:"
    foreach ($pr in $probe.providers) {
        if ($pr.error) { Log "  $($pr.node) [$($pr.kind)] ERROR $($pr.error)" }
        else { Log "  $($pr.node) [$($pr.kind)] usage=$($pr.usage.prompt_tokens)+$($pr.usage.completion_tokens) => $($pr.content)" }
    }
    foreach ($pair in $probe.pairs) { Log "  比对 $($pair.a) vs $($pair.b): 相似度=$($pair.similarity) 判定=$($pair.verdict)" }
} catch { Log "复算失败: $($_.Exception.Message)" }

$au = Invoke-RestMethod -Uri "$B/api/ledger/audits?limit=10" -TimeoutSec 10
Log "手动复算后审计: $($au.audits.Count) 条, 统计=$($au.stats | ConvertTo-Json -Compress)"

# 自动抽样路径：把抽样率设为 100%，发一次普通请求，看是否自动触发复算
Invoke-RestMethod -Uri "$B/api/settings" -Method Post -ContentType "application/json" -TimeoutSec 10 -Body '{"audit_rate":1.0}' | Out-Null
Log "B 抽样率已设为 1.0"
$chatBody = @{ model = ($peers[0].models[0].id); messages = @(@{ role='user'; content='2+2 等于几？只回答数字。' }); max_tokens = 20; stream = $false } | ConvertTo-Json -Depth 5
$cr = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $chatBody -TimeoutSec 200 -UseBasicParsing -SkipHttpErrorCheck
$cj = $cr.Content | ConvertFrom-Json
Log "自动抽样请求: HTTP=$($cr.StatusCode) 路由=$($cr.Headers['X-Aether-Route']) 回答=$($cj.choices[0].message.content)"
Start-Sleep -Seconds 10
$au2 = Invoke-RestMethod -Uri "$B/api/ledger/audits?limit=10" -TimeoutSec 10
Log "自动抽样后审计: $($au2.audits.Count) 条, 统计=$($au2.stats | ConvertTo-Json -Compress)"
foreach ($a in $au2.audits) {
    Log "  verdict=$($a.verdict) sim=$($a.similarity) req=$($a.req_id.Substring(0,8)) target=$($a.target_node.Substring(0,8)) verifier=$($a.verifier.Substring(0,8))"
}

Stop-Process -Id $pA.Id,$pB.Id,$pC.Id -Force -ErrorAction SilentlyContinue
foreach ($d in @('.audit-a','.audit-c')) { Remove-Item "$root\$d\models","$root\$d\runtime" -Force -ErrorAction SilentlyContinue }
Log "DONE"
