# 压测驱动：起 mock 上游 + aether 节点，跑并发与长稳场景
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 400 }; return $false }

$home2 = "$root\.load-test"
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $home2 | Out-Null
@{ api_port = 19001; advertise = $false; api_max_concurrency = 4 } | ConvertTo-Json | Set-Content "$home2\settings.json" -Encoding UTF8

$mock = Start-Process -FilePath $py -ArgumentList "$root\tests\mock_openai.py","18999" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\mock.out" -RedirectStandardError "$home2\mock.err"
Log "mock_up=$(Up 18999)"

$env:AETHER_HOME = $home2
$srv = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","19001" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\o.log" -RedirectStandardError "$home2\e.log"
Log "server_up=$(Up 19001)"

$base = "http://127.0.0.1:19001"
Invoke-RestMethod -Uri "$base/api/providers" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{
    action = "add"; name = "load-upstream"; endpoint = "http://127.0.0.1:18999";
    models = @("mock-model"); price = @{ in = 0.1; out = 0.3 }
} | ConvertTo-Json -Depth 5) | Out-Null
Log "已配置 mock 上游"

$env:LOAD_BASE = $base
$env:LOAD_MODEL = "mock-model"
& $py "$root\tests\loadtest.py" 2>&1
$code = $LASTEXITCODE
Log "loadtest exit=$code"

Log "--- 服务端日志尾部 ---"
Get-Content "$home2\e.log" -Tail 8 -ErrorAction SilentlyContinue

Stop-Process -Id $srv.Id,$mock.Id -Force -ErrorAction SilentlyContinue
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
Log "DONE"
exit $code
