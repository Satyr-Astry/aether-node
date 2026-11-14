# relay 断线与重连
#
# 验证 NostrClient 的状态机：relay 消失时状态要变成 error 而不是假装还连着，
# relay 回来后要能自动重连并重新注册身份，中间不需要重启本节点。
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 400 }; return $false }

foreach ($d in @('.rc-a','.rc-b')) { Remove-Item "$root\$d" -Recurse -Force -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Force -Path "$root\$d" | Out-Null }
New-Item -ItemType Junction -Path "$root\.rc-a\models" -Target "$root\data\models" | Out-Null
New-Item -ItemType Junction -Path "$root\.rc-a\runtime" -Target "$root\data\runtime" | Out-Null
@{ api_port = 19071; advertise = $false } | ConvertTo-Json | Set-Content "$root\.rc-a\settings.json" -Encoding UTF8
@{ api_port = 19072; advertise = $false } | ConvertTo-Json | Set-Content "$root\.rc-b\settings.json" -Encoding UTF8

$env:AETHER_HOME = "$root\.rc-a"
$pA = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19071" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.rc-a\o.log" -RedirectStandardError "$root\.rc-a\e.log"
$env:AETHER_HOME = "$root\.rc-b"
$pB = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19072" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.rc-b\o.log" -RedirectStandardError "$root\.rc-b\e.log"
Log "A=$(Up 19071) B=$(Up 19072)"
$A = "http://127.0.0.1:19071"
$B = "http://127.0.0.1:19072"

# B 连 A 的 relay
Invoke-RestMethod -Uri "$B/api/p2p/relays" -Method Post -ContentType "application/json" -TimeoutSec 15 `
    -Body '{"action":"add","url":"ws://127.0.0.1:19071/relay"}' | Out-Null

# A 加载模型并广播，让 B 能发现
$ms = Invoke-RestMethod -Uri "$A/api/models" -TimeoutSec 20
$m = $ms.models | Select-Object -First 1
Invoke-RestMethod -Uri "$A/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt = Invoke-RestMethod -Uri "$A/api/runtime" -TimeoutSec 10; if ($rt.state -in @('ready','failed')) { break } }
if ($rt.state -eq 'ready') {
    Invoke-RestMethod -Uri "$A/api/p2p/advertise" -Method Post -ContentType "application/json" -TimeoutSec 30 -Body '{"enabled":true}' | Out-Null
}

function BRelayState {
    $st = Invoke-RestMethod -Uri "$B/api/p2p/status" -TimeoutSec 10
    $relay = $st.relays | Where-Object { $_.url -like '*19071*' } | Select-Object -First 1
    return @{ status = $relay.status; attempts = $relay.attempts; peers = $st.peers.Count; connected = $st.relay_connected }
}

# ---- 阶段 1：正常 ----
# 广告传播需要 1-2 秒，这里轮询等而不是立即取
$s1 = $null
for ($i=0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    $s1 = BRelayState
    if ($s1.peers -ge 1) { break }
}
Log "阶段1 正常: relay=$($s1.status) peers=$($s1.peers)"
$peers1 = $s1.peers

# ---- 阶段 2：杀 A ----
Stop-Process -Id $pA.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 6
$s2 = BRelayState
Log "阶段2 A已停: relay=$($s2.status) attempts=$($s2.attempts) peers=$($s2.peers)"

# 隧道调用应给出明确失败，而不是挂住
$body = @{ model = $m.display; messages = @(@{ role='user'; content='ping' }); max_tokens = 8; stream = $false } | ConvertTo-Json -Depth 5
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$r = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 120 -UseBasicParsing -SkipHttpErrorCheck
$sw.Stop()
$snip = if (([string]$r.Content).Length -gt 120) { ([string]$r.Content).Substring(0,120) } else { [string]$r.Content }
Log "  隧道调用: HTTP=$($r.StatusCode) 耗时=$($sw.Elapsed.TotalSeconds.ToString('N1'))s $snip"

# ---- 阶段 3：重启 A ----
$env:AETHER_HOME = "$root\.rc-a"
$pA = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19071" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.rc-a\o2.log" -RedirectStandardError "$root\.rc-a\e2.log"
Up 19071 | Out-Null
Log "阶段3 A已重启，等待 B 自动重连…"

$reconnected = $false
$final = $null
for ($i=0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 2
    $st = BRelayState
    if ($st.status -eq 'connected') { $reconnected = $true; $final = $st; break }
    if ($i % 3 -eq 0) { Log "  等待中: relay=$($st.status) attempts=$($st.attempts) (t+$(($i+1)*2)s)" }
}
if ($reconnected) { Log "阶段3 重连成功: peers=$($final.peers)" } else { Log "阶段3 重连超时" }

# ---- 阶段 4：重连后能否真的调用 ----
# 注意：A 是全新进程，模型与广播状态都不会自动恢复，必须先重新加载再广播。
# 这本身就是个值得记录的运维事实——重启不是"什么都回到原样"。
if ($reconnected) {
    Log "阶段4 A 重新加载模型并广播"
    Invoke-RestMethod -Uri "$A/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
    for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt2 = Invoke-RestMethod -Uri "$A/api/runtime" -TimeoutSec 10; if ($rt2.state -in @('ready','failed')) { break } }
    Log "  A runtime=$($rt2.state)"
    if ($rt2.state -eq 'ready') {
        Invoke-RestMethod -Uri "$A/api/p2p/advertise" -Method Post -ContentType "application/json" -TimeoutSec 30 -Body '{"enabled":true}' | Out-Null
        # A 重启后要重新连自己的 relay 并发 HELLO，给足时间；顺便打出注册表状态便于判断
        for ($i=0; $i -lt 12; $i++) {
            Start-Sleep -Seconds 2
            $sysA = Invoke-RestMethod -Uri "$A/api/system" -TimeoutSec 10
            $nodes = $sysA.relay.nodes_registered
            if ($i % 2 -eq 0) { Log "  等待 A 注册到 relay: nodes_registered=$nodes" }
            if ($nodes -ge 2) { break }
        }
        Start-Sleep -Seconds 2
        $sw2 = [System.Diagnostics.Stopwatch]::StartNew()
        $r2 = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 180 -UseBasicParsing -SkipHttpErrorCheck
        $sw2.Stop()
        Log "阶段4 重连后调用: HTTP=$($r2.StatusCode) 路由=$($r2.Headers['X-Aether-Route']) 耗时=$($sw2.Elapsed.TotalSeconds.ToString('N1'))s"
        if ($r2.StatusCode -ne 200) {
            $snip2 = if (([string]$r2.Content).Length -gt 200) { ([string]$r2.Content).Substring(0,200) } else { [string]$r2.Content }
            Log "  失败详情: $snip2"
        }
    }
}

# ---- 判定 ----
$pass = $true
if ($peers1 -lt 1) { Log "!! 阶段1 未发现节点"; $pass = $false }
if ($s2.status -eq 'connected') { Log "!! 阶段2 relay 仍显示 connected"; $pass = $false }
if ($s2.attempts -lt 1) { Log "!! 阶段2 未记录重连尝试"; $pass = $false }
if ($r.StatusCode -eq 200) { Log "!! 阶段2 A 已停却返回 200"; $pass = $false }
if ($sw.Elapsed.TotalSeconds -gt 60) { Log "!! 阶段2 调用挂得太久"; $pass = $false }
if (-not $reconnected) { Log "!! 阶段3 未自动重连"; $pass = $false }
if ($reconnected -and $r2.StatusCode -ne 200) { Log "!! 阶段4 重连后仍调用失败"; $pass = $false }

Stop-Process -Id $pA.Id,$pB.Id -Force -ErrorAction SilentlyContinue
Remove-Item "$root\.rc-a\models","$root\.rc-a\runtime" -Force -ErrorAction SilentlyContinue
Remove-Item "$root\.rc-a","$root\.rc-b" -Recurse -Force -ErrorAction SilentlyContinue
if ($pass) { Log "PASS 断线与重连符合预期" } else { Log "FAIL 存在不符合预期的场景" }
Log "DONE"
exit $(if ($pass) { 0 } else { 1 })
