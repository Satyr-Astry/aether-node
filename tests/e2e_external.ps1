# 自有外部 API 路由验证：mock OpenAI 端点注册为 provider，经统一入口调用并记账
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 60; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 400 }; return $false }

$home2 = "$root\.ext-test"
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $home2 | Out-Null
@{ api_port = 18980; advertise = $false } | ConvertTo-Json | Set-Content "$home2\settings.json" -Encoding UTF8

$mock = Start-Process -FilePath $py -ArgumentList "$root\tests\mock_openai.py","18999" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\mock.out" -RedirectStandardError "$home2\mock.err"
Log "mock_up=$(Up 18999)"

$env:AETHER_HOME = $home2
$srv = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18980" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\o.log" -RedirectStandardError "$home2\e.log"
Log "server_up=$(Up 18980)"
$base = "http://127.0.0.1:18980"

# 注册外部端点
$add = Invoke-RestMethod -Uri "$base/api/providers" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{
    action = "add"; name = "mock-api"; endpoint = "http://127.0.0.1:18999";
    api_key = "sk-test-0123456789abcdef"; models = @("mock-model");
    price = @{ in = 0.2; out = 0.8 }
} | ConvertTo-Json -Depth 5)
Log "注册: ok=$($add.ok) count=$($add.count)"

$list = Invoke-RestMethod -Uri "$base/api/providers" -TimeoutSec 10
foreach ($p in $list.providers) {
    Log "  provider: $($p.name) endpoint=$($p.endpoint) models=$($p.models -join ',') masked=$($p.api_key_masked) has_key=$($p.has_key)"
}

$test = Invoke-RestMethod -Uri "$base/api/providers" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body '{"action":"test","name":"mock-api"}'
Log "连通性测试: ok=$($test.ok) models=$($test.models -join ',')"

# 路由预览：应选中 external
$rp = Invoke-RestMethod -Uri "$base/api/route/preview" -Method Post -ContentType "application/json" -TimeoutSec 10 -Body '{"model":"mock-model"}'
Log "路由候选 $($rp.candidates.Count) 个:"
foreach ($c in $rp.candidates) { Log "  kind=$($c.kind) node=$($c.node_name) score=$($c.score) note=$($c.note)" }

# 实际调用
$body = @{ model = "mock-model"; messages = @(@{ role='user'; content='你好' }); max_tokens = 20; stream = $false } | ConvertTo-Json -Depth 5
$r = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 60 -UseBasicParsing -SkipHttpErrorCheck
$j = $r.Content | ConvertFrom-Json
Log "调用: HTTP=$($r.StatusCode) 路由=$($r.Headers['X-Aether-Route']) 节点=$($r.Headers['X-Aether-Node'])"
Log "  回答: $($j.choices[0].message.content)"
Log "  usage: $($j.usage.prompt_tokens)+$($j.usage.completion_tokens)"

Start-Sleep -Seconds 1
$led = Invoke-RestMethod -Uri "$base/api/ledger/entries?limit=5" -TimeoutSec 10
Log "账本记录:"
foreach ($e in $led.entries) { Log "  role=$($e.role) source=$($e.source) model=$($e.model) in=$($e.in_tok) out=$($e.out_tok) amount=$($e.amount) peer=$($e.peer_node)" }

$cr = Invoke-RestMethod -Uri "$base/api/ledger/credit" -TimeoutSec 10
Log "信用表条目数（外部端点不应产生债务）: $($cr.credit.Count)"

# 流式也应走外部端点
$sbody = @{ model = "mock-model"; messages = @(@{ role='user'; content='再来一次' }); max_tokens = 20; stream = $true } | ConvertTo-Json -Depth 5
$sr = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json" -Body $sbody -TimeoutSec 60 -UseBasicParsing -SkipHttpErrorCheck
Log "流式: HTTP=$($sr.StatusCode) 路由=$($sr.Headers['X-Aether-Route']) 内容长度=$($sr.Content.Length)"

# 移除
$rm = Invoke-RestMethod -Uri "$base/api/providers" -Method Post -ContentType "application/json" -TimeoutSec 10 -Body '{"action":"remove","name":"mock-api"}'
Log "移除: ok=$($rm.ok) count=$($rm.count)"
$rp2 = Invoke-RestMethod -Uri "$base/api/route/preview" -Method Post -ContentType "application/json" -TimeoutSec 10 -Body '{"model":"mock-model"}'
Log "移除后候选数: $($rp2.candidates.Count)"

Stop-Process -Id $srv.Id,$mock.Id -Force -ErrorAction SilentlyContinue
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
Log "DONE"
