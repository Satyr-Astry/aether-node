# 长稳压测：持续流量下的资源曲线
#
# 之前只做过秒级压测，泄漏类问题在那种时间尺度上看不出来。这里跑 5 分钟持续流量，
# 每 20 秒采样一次进程内存与内部计数，看是否存在单调增长。
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 400 }; return $false }

$DURATION = 300       # 总时长（秒）
$SAMPLE = 20          # 采样间隔
$BATCH = 40           # 每轮请求数

$home2 = "$root\.soak-test"
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $home2 | Out-Null
@{ api_port = 19081; advertise = $false; api_max_concurrency = 8 } | ConvertTo-Json | Set-Content "$home2\settings.json" -Encoding UTF8

$mockPort = 18993
$mock = Start-Process -FilePath $py -ArgumentList "$root\tests\mock_openai.py", "$mockPort" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\mock.out" -RedirectStandardError "$home2\mock.err"
$env:AETHER_HOME = $home2
$srv = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19081" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\o.log" -RedirectStandardError "$home2\e.log"
Log "mock=$(Up $mockPort) server=$(Up 19081)"
$base = "http://127.0.0.1:19081"

Invoke-RestMethod -Uri "$base/api/providers" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{
    action = "add"; name = "soak-upstream"; endpoint = "http://127.0.0.1:$mockPort";
    models = @("mock-model"); price = @{ in = 0.1; out = 0.3 }
} | ConvertTo-Json -Depth 5) | Out-Null

# 采样内存时必须按命令行找真实进程：Start-Process 返回的 PID 是 launcher（约 4MB），
# 真正跑服务的是它的子进程（约 60MB）。用 $srv.Id 采样会得到一个恒定不变的小数字，
# 看起来"零增长"其实什么都没测到。
function GetServerMemMB($port) {
    $pids = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$port*" } |
        ForEach-Object { $_.ProcessId })
    $max = 0
    foreach ($procId in $pids) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc -and $proc.WorkingSet64 -gt $max) { $max = $proc.WorkingSet64 }
    }
    if ($max -eq 0) { return -1 }
    return [math]::Round($max / 1MB, 1)
}

# 预热，避开启动期的一次性分配
Start-Sleep -Seconds 3
$warm = GetServerMemMB 19081
Log ("预热后内存 {0:N1} MB（取命令行含 19081 的最大进程）" -f $warm)

$body = @{ model = "mock-model"; messages = @(@{ role='user'; content='ping' }); max_tokens = 8; stream = $false } | ConvertTo-Json -Depth 5
$samples = @()
$elapsed = 0
$errors = 0
$totalReq = 0

while ($elapsed -lt $DURATION) {
    $jobs = @()
    foreach ($i in 1..$BATCH) {
        $jobs += Start-Job -ScriptBlock {
            param($u, $b)
            try {
                $r = Invoke-WebRequest -Uri $u -Method Post -ContentType "application/json" -Body $b -TimeoutSec 60 -UseBasicParsing -SkipHttpErrorCheck
                return [int]$r.StatusCode
            } catch { return -1 }
        } -ArgumentList "$base/v1/chat/completions", $body
    }
    $codes = $jobs | Wait-Job | Receive-Job
    $jobs | Remove-Job -Force
    $totalReq += $codes.Count
    $errors += @($codes | Where-Object { $_ -ne 200 }).Count

    $ws = GetServerMemMB 19081
    $md = (Invoke-WebRequest "$base/metrics" -UseBasicParsing).Content
    $inflight = ($md -split "`n" | Where-Object { $_ -match '^aether_inflight ' }) -replace '.*\s', ''
    $sum = (Invoke-RestMethod "$base/api/ledger/summary").summary
    $st = Invoke-RestMethod "$base/api/p2p/status"
    $pending = $st.tunnel.requests_out

    $samples += [pscustomobject]@{
        t = $elapsed; mem = $ws; inflight = [int]$inflight
        ledger = $sum.total_entries; reqs = $totalReq; errs = $errors
    }
    Log ("t+{0,3}s 内存={1,6:N1}MB inflight={2} 账本={3} 累计请求={4} 错误={5}" -f $elapsed, $ws, $inflight, $sum.total_entries, $totalReq, $errors)

    $elapsed += $SAMPLE
    if ($elapsed -ge $DURATION) { break }
    Start-Sleep -Seconds $SAMPLE
}

# 分析趋势
$first = $samples | Select-Object -First 3 | Measure-Object -Property mem -Average
$last = $samples | Select-Object -Last 3 | Measure-Object -Property mem -Average
$growth = $last.Average - $first.Average
$growthPct = if ($first.Average -gt 0) { $growth / $first.Average * 100 } else { 0 }
Log ("内存: 起始均值 {0:N1}MB → 结束均值 {1:N1}MB，增长 {2:N1}MB ({3:N1}%)" -f $first.Average, $last.Average, $growth, $growthPct)
Log ("请求总计 {0}，错误 {1}，账本 {2}" -f $totalReq, $errors, ($samples[-1].ledger))

$pass = $true
if ($errors -gt ($totalReq * 0.01)) { Log "!! 错误率超过 1%"; $pass = $false }
if ($growthPct -gt 20) { Log "!! 内存增长超过 20%"; $pass = $false }
if ($samples[-1].inflight -ne 0) { Log "!! 结束时 inflight 非零: $($samples[-1].inflight)"; $pass = $false }
$expectedLedger = $totalReq - $errors
if ([Math]::Abs($samples[-1].ledger - $expectedLedger) -gt 2) {
    Log "!! 账本条目 $($samples[-1].ledger) 与成功请求 $expectedLedger 不符"
    $pass = $false
}

Stop-Process -Id $srv.Id,$mock.Id -Force -ErrorAction SilentlyContinue
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
if ($pass) { Log "PASS 长稳曲线正常" } else { Log "FAIL 存在异常趋势" }
Log "DONE"
exit $(if ($pass) { 0 } else { 1 })
