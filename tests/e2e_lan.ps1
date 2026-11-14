# 非回环网络路径验证：走真实网卡 IP 而非 127.0.0.1
#
# 这不能替代跨机器验证，但能覆盖回环测不到的两件事：
#   1) 鉴权分支真的会触发——127.0.0.1 被信任，局域网 IP 不被信任
#   2) relay 的 token 校验在非本地连接上生效
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 400 }; return $false }

$lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -match '^192\.168\.|^10\.|^172\.(1[6-9]|2[0-9]|3[01])\.' } |
    Select-Object -First 1).IPAddress
if (-not $lanIp) { Log "找不到局域网 IP，跳过"; exit 2 }
Log "局域网 IP: $lanIp"

foreach ($d in @('.lan-a','.lan-b')) { Remove-Item "$root\$d" -Recurse -Force -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Force -Path "$root\$d" | Out-Null }
New-Item -ItemType Junction -Path "$root\.lan-a\models" -Target "$root\data\models" | Out-Null
New-Item -ItemType Junction -Path "$root\.lan-a\runtime" -Target "$root\data\runtime" | Out-Null
# A 开启共享：绑 0.0.0.0，自动生成 key
@{ api_port = 19051; share_enabled = $true; advertise = $false } | ConvertTo-Json | Set-Content "$root\.lan-a\settings.json" -Encoding UTF8
@{ api_port = 19052; advertise = $false } | ConvertTo-Json | Set-Content "$root\.lan-b\settings.json" -Encoding UTF8

$env:AETHER_HOME = "$root\.lan-a"
$pA = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19051" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.lan-a\o.log" -RedirectStandardError "$root\.lan-a\e.log"
$env:AETHER_HOME = "$root\.lan-b"
$pB = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19052" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.lan-b\o.log" -RedirectStandardError "$root\.lan-b\e.log"
Log "A=$(Up 19051) B=$(Up 19052)"

$localA = "http://127.0.0.1:19051"
$lanA = "http://${lanIp}:19051"
$B = "http://127.0.0.1:19052"

$cfg = Get-Content "$root\.lan-a\settings.json" -Raw | ConvertFrom-Json
$key = $cfg.api_key
Log "A 已开启共享，api_key 长度 $($key.Length)"
if (-not $key) { Log "!! 共享模式下没有生成 key"; Stop-Process -Id $pA.Id,$pB.Id -Force; exit 1 }

# 1) 本地回环免鉴权
try { $r = Invoke-WebRequest "$localA/api/models" -UseBasicParsing -SkipHttpErrorCheck; Log "回环无 key -> $($r.StatusCode)（期望 200）" } catch { Log "回环无 key 失败: $($_.Exception.Message)" }

# 2) 局域网 IP 无 key -> 401
$r = Invoke-WebRequest "$lanA/api/models" -UseBasicParsing -SkipHttpErrorCheck
Log "局域网无 key -> $($r.StatusCode)（期望 401）"
$r2 = Invoke-WebRequest "$lanA/v1/models" -UseBasicParsing -SkipHttpErrorCheck
Log "局域网 /v1 无 key -> $($r2.StatusCode)（期望 401）"

# 3) 带 key -> 200
$h = @{ "X-Api-Key" = $key }
$r3 = Invoke-WebRequest "$lanA/api/models" -UseBasicParsing -SkipHttpErrorCheck -Headers $h
Log "局域网带 key -> $($r3.StatusCode)（期望 200）"
$r4 = Invoke-WebRequest "$lanA/v1/models" -UseBasicParsing -SkipHttpErrorCheck -Headers @{ Authorization = "Bearer $key" }
Log "局域网 /v1 带 Bearer -> $($r4.StatusCode)（期望 200）"

# 4) relay 的 token 校验：不带 token 连局域网 relay 应被拒
$wstest = & $py -c @"
import asyncio, json, sys
async def main():
    import websockets
    for url in ('ws://127.0.0.1:19051/relay', 'ws://${lanIp}:19051/relay'):
        try:
            async with websockets.connect(url, open_timeout=5) as ws:
                await ws.send(json.dumps(['PING']))
                try:
                    await asyncio.wait_for(ws.recv(), timeout=3)
                    print(url, 'ACCEPTED')
                except Exception:
                    print(url, 'ACCEPTED(no-reply)')
        except Exception as e:
            print(url, 'REJECTED', type(e).__name__)
asyncio.run(main())
"@
$wstest | ForEach-Object { Log "  $_" }

# 5) B 通过局域网 IP 接入 A 的 relay（带 token）
try {
    Invoke-RestMethod -Uri "$B/api/p2p/relays" -Method Post -ContentType "application/json" -TimeoutSec 15 `
        -Body (@{ action = "add"; url = "ws://${lanIp}:19051/relay?token=$key" } | ConvertTo-Json) | Out-Null
    Log "B 已接入局域网 relay（带 token）"
} catch { Log "B 接入失败: $($_.Exception.Message)" }

# 6a) 给 B 配置对 A 的信任 key
# 共享模式下跨节点直连必须带目标节点的 key。这是刻意的设计：广告里不该放明文密钥，
# 所以要么双方预先交换 key 走直连，要么走 relay 隧道（隧道用节点签名认证，不需要共享密钥）。
$aNodeId = (Invoke-RestMethod -Uri "$localA/api/system" -TimeoutSec 10).node_id
$bCfgPath = "$root\.lan-b\settings.json"
$bCfg = Get-Content $bCfgPath -Raw | ConvertFrom-Json
$peerKeys = @{}
$peerKeys[$aNodeId] = $key
$bCfg | Add-Member -NotePropertyName peer_keys -NotePropertyValue $peerKeys -Force
$bCfg | ConvertTo-Json -Depth 8 | Set-Content $bCfgPath -Encoding UTF8
Stop-Process -Id $pB.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
$env:AETHER_HOME = "$root\.lan-b"
$pB = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19052" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$root\.lan-b\o.log" -RedirectStandardError "$root\.lan-b\e.log"
Up 19052 | Out-Null
Log "已为 B 配置对 A 的信任 key 并重启 B"
Invoke-RestMethod -Uri "$B/api/p2p/relays" -Method Post -ContentType "application/json" -TimeoutSec 15 `
    -Body (@{ action = "add"; url = "ws://${lanIp}:19051/relay?token=$key" } | ConvertTo-Json) | Out-Null

# 6) A 加载模型并广播，B 应能通过局域网 relay 发现它
$ms = Invoke-RestMethod -Uri "$localA/api/models" -TimeoutSec 20
$m = $ms.models | Select-Object -First 1
Invoke-RestMethod -Uri "$localA/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt = Invoke-RestMethod -Uri "$localA/api/runtime" -TimeoutSec 10; if ($rt.state -in @('ready','failed')) { break } }
Log "A runtime=$($rt.state)"
if ($rt.state -eq 'ready') {
    # 广播必须带 key：A 自己通过本地回环调用，免鉴权
    Invoke-RestMethod -Uri "$localA/api/p2p/advertise" -Method Post -ContentType "application/json" -TimeoutSec 30 -Body '{"enabled":true}' | Out-Null
}

$peer = $null
for ($i=0; $i -lt 20; $i++) { Start-Sleep -Seconds 2; $ps = Invoke-RestMethod -Uri "$B/api/p2p/peers" -TimeoutSec 10; if ($ps.peers.Count -gt 0) { $peer = $ps.peers[0]; break } }
if ($peer) {
    Log "B 发现节点: name=$($peer.name) reach=$($peer.reachability) ep=$($peer.endpoint)"
    Log "  端点是否局域网 IP: $($peer.endpoint -match [regex]::Escape($lanIp))"
} else { Log "!! B 未发现节点" }

# 7) B 经局域网直连调用 A（A 公布的是局域网端点，需要 key）
if ($peer -and $peer.endpoint) {
    $body = @{ model = $m.display; messages = @(@{ role='user'; content='只回答两个字：通了' }); max_tokens = 20; stream = $false } | ConvertTo-Json -Depth 5
    $r5 = Invoke-WebRequest -Uri "$B/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 200 -UseBasicParsing -SkipHttpErrorCheck
    $j = $r5.Content | ConvertFrom-Json
    Log "B 调用 A: HTTP=$($r5.StatusCode) 路由=$($r5.Headers['X-Aether-Route']) 回答=$($j.choices[0].message.content)"
    if ($r5.StatusCode -ne 200) { Log "  失败详情: $($r5.Content.Substring(0,[Math]::Min(300,$r5.Content.Length)))" }
}

Stop-Process -Id $pA.Id,$pB.Id -Force -ErrorAction SilentlyContinue
foreach ($d in @('.lan-a')) { Remove-Item "$root\$d\models","$root\$d\runtime" -Force -ErrorAction SilentlyContinue }
Remove-Item "$root\.lan-a","$root\.lan-b" -Recurse -Force -ErrorAction SilentlyContinue
Log "DONE"
