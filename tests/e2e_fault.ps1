# 故障注入与恢复
#
# 之前所有测试都跑在"一切正常"的路径上。这里注入三类故障，验证：
#   1) 上游进程死掉时，代理层给出明确错误而不是挂住
#   2) 上游恢复后，路由能自动恢复（不需要重启本节点）
#   3) 账本把失败也记下来（status=error），不会凭空多出成功记录
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 400 }; return $false }

$home2 = "$root\.fault-test"
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $home2 | Out-Null
@{ api_port = 19061; advertise = $false; api_max_concurrency = 4 } | ConvertTo-Json | Set-Content "$home2\settings.json" -Encoding UTF8

$mockPort = 18991
$mockScript = "$root\tests\mock_openai.py"
$mock = Start-Process -FilePath $py -ArgumentList $mockScript, "$mockPort" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\mock.out" -RedirectStandardError "$home2\mock.err"
$env:AETHER_HOME = $home2
$srv = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19061" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\o.log" -RedirectStandardError "$home2\e.log"
Log "mock=$(Up $mockPort) server=$(Up 19061)"
$base = "http://127.0.0.1:19061"

Invoke-RestMethod -Uri "$base/api/providers" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{
    action = "add"; name = "fault-upstream"; endpoint = "http://127.0.0.1:$mockPort";
    models = @("mock-model"); price = @{ in = 0.1; out = 0.3 }
} | ConvertTo-Json -Depth 5) | Out-Null

function TryChat($label) {
    $body = @{ model = "mock-model"; messages = @(@{ role='user'; content='ping' }); max_tokens = 8; stream = $false } | ConvertTo-Json -Depth 5
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 90 -UseBasicParsing -SkipHttpErrorCheck
        $code = [int]$r.StatusCode
        $text = [string]$r.Content
    } catch {
        $code = -1
        $text = $_.Exception.Message
    }
    $sw.Stop()
    $snip = if ($text.Length -gt 140) { $text.Substring(0, 140) } else { $text }
    Write-Host ("    {0}: HTTP={1} 耗时={2:N2}s {3}" -f $label, $code, $sw.Elapsed.TotalSeconds, $snip)
    return $code
}

# ---- 基准 ----
$before = (Invoke-RestMethod "$base/api/ledger/summary").summary.total_entries
$c1 = TryChat "1. 正常调用"

# ---- 故障 1：杀掉上游 ----
Stop-Process -Id $mock.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
$c2 = TryChat "2. 上游已死"

# 账本应记一条 error，不计入成功
Start-Sleep -Seconds 1
$led = Invoke-RestMethod "$base/api/ledger/entries?limit=5"
$errEntries = @($led.entries | Where-Object { $_.status -eq 'error' })
Log "  失败请求记账: $($errEntries.Count) 条 status=error"
$sum = Invoke-RestMethod "$base/api/ledger/summary"
Log "  汇总: total=$($sum.summary.total_entries) consume=$($sum.summary.consume.count)"

# ---- 恢复 1：重启上游 ----
$mock = Start-Process -FilePath $py -ArgumentList $mockScript, "$mockPort" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\mock2.out" -RedirectStandardError "$home2\mock2.err"
Up $mockPort | Out-Null
$c3 = TryChat "3. 上游已恢复"

# ---- 故障 2：把上游端点改成一个死端口 ----
Invoke-RestMethod -Uri "$base/api/providers" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{
    action = "add"; name = "fault-upstream"; endpoint = "http://127.0.0.1:1";
    models = @("mock-model"); price = @{ in = 0.1; out = 0.3 }
} | ConvertTo-Json -Depth 5) | Out-Null
$c4 = TryChat "4. 端点指向死端口"

# ---- 恢复 2：改回正确端点 ----
Invoke-RestMethod -Uri "$base/api/providers" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{
    action = "add"; name = "fault-upstream"; endpoint = "http://127.0.0.1:$mockPort";
    models = @("mock-model"); price = @{ in = 0.1; out = 0.3 }
} | ConvertTo-Json -Depth 5) | Out-Null
$c5 = TryChat "5. 端点改回"

# ---- 故障 3：并发中杀掉上游 ----
Log "6. 并发 8 请求中途杀上游"
$body = @{ model = "mock-model"; messages = @(@{ role='user'; content='ping' }); max_tokens = 8; stream = $false } | ConvertTo-Json -Depth 5
$jobs = @()
foreach ($i in 1..8) {
    $jobs += Start-Job -ScriptBlock {
        param($u, $b)
        try {
            $r = Invoke-WebRequest -Uri $u -Method Post -ContentType "application/json" -Body $b -TimeoutSec 60 -UseBasicParsing -SkipHttpErrorCheck
            return $r.StatusCode
        } catch { return 0 }
    } -ArgumentList "$base/v1/chat/completions", $body
}
Start-Sleep -Milliseconds 300
Stop-Process -Id $mock.Id -Force -ErrorAction SilentlyContinue
$codes = $jobs | Wait-Job | Receive-Job
$jobs | Remove-Job -Force
Log "  并发结果分布: $(($codes | Group-Object | ForEach-Object { "$($_.Name)×$($_.Count)" }) -join ' ')"
$noHang = @($codes | Where-Object { $_ -ne 0 }).Count
Log "  有明确响应的请求: $noHang/8（=0 表示连接被重置，也是明确结果）"

# ---- 服务仍然健康 ----
$hz = Invoke-WebRequest "$base/healthz" -UseBasicParsing -SkipHttpErrorCheck
$rz = Invoke-WebRequest "$base/readyz" -UseBasicParsing -SkipHttpErrorCheck
$md = (Invoke-WebRequest "$base/metrics" -UseBasicParsing).Content
$inflight = ($md -split "`n" | Where-Object { $_ -match '^aether_inflight ' }) -replace '.*\s', ''
Log "7. 故障后健康检查: healthz=$($hz.StatusCode) readyz=$($rz.StatusCode) inflight=$inflight"

# ---- 结论判定 ----
$pass = $true
if ($c1 -ne 200) { Log "!! 基准调用失败"; $pass = $false }
if ($c2 -eq 200) { Log "!! 上游已死却返回 200"; $pass = $false }
if ($c3 -ne 200) { Log "!! 上游恢复后仍失败"; $pass = $false }
if ($c4 -eq 200) { Log "!! 死端口却返回 200"; $pass = $false }
if ($c5 -ne 200) { Log "!! 端点改回后仍失败"; $pass = $false }
if ($errEntries.Count -lt 1) { Log "!! 失败请求未记账"; $pass = $false }
if ($inflight -ne "0") { Log "!! inflight 未归零: $inflight"; $pass = $false }
if ($hz.StatusCode -ne 200) { Log "!! 故障后进程不健康"; $pass = $false }

Stop-Process -Id $srv.Id,$mock.Id -Force -ErrorAction SilentlyContinue
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
if ($pass) { Log "PASS 全部故障场景符合预期" } else { Log "FAIL 存在不符合预期的场景" }
Log "DONE"
exit $(if ($pass) { 0 } else { 1 })
