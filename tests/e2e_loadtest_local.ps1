# 真实推理压测驱动：加载真模型后压代理层
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 90; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 400 }; return $false }

$home2 = "$root\.load-local"
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $home2 | Out-Null
New-Item -ItemType Junction -Path "$home2\models" -Target "$root\data\models" | Out-Null
New-Item -ItemType Junction -Path "$home2\runtime" -Target "$root\data\runtime" | Out-Null
@{ api_port = 19011; advertise = $false; api_max_concurrency = 4; parallel_slots = 2 } |
    ConvertTo-Json | Set-Content "$home2\settings.json" -Encoding UTF8

$env:AETHER_HOME = $home2
$srv = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19011" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\o.log" -RedirectStandardError "$home2\e.log"
Log "server_up=$(Up 19011)"
$base = "http://127.0.0.1:19011"

$ms = Invoke-RestMethod -Uri "$base/api/models" -TimeoutSec 20
$m = $ms.models | Select-Object -First 1
if (-not $m) { Log "没有本地模型"; Stop-Process -Id $srv.Id -Force; exit 1 }
Log "加载模型: $($m.display)"
Invoke-RestMethod -Uri "$base/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
$ready = $false
for ($i=0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    $rt = Invoke-RestMethod -Uri "$base/api/runtime" -TimeoutSec 10
    if ($rt.state -in @('ready','failed')) { $ready = ($rt.state -eq 'ready'); break }
}
Log "runtime=$($rt.state)"
if (-not $ready) { Log "加载失败: $($rt.error)"; Stop-Process -Id $srv.Id -Force; exit 1 }

$env:LOAD_BASE = $base
$env:LOAD_MODEL = $rt.model
$env:LOAD_CONCURRENCY = "4"
$env:LOAD_TOTAL = "12"
& $py "$root\tests\loadtest_local.py" 2>&1
$code = $LASTEXITCODE
Log "loadtest_local exit=$code"

Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
Remove-Item "$home2\models","$home2\runtime" -Force -ErrorAction SilentlyContinue
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
Log "DONE"
exit $code
