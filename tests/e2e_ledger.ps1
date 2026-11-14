# 账本端到端验证：真实请求的 token 计量、对账、结算凭证
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\aether-node"
$env:AETHER_INSECURE_TLS = "1"
$py = "E:\HERMES\hermes-agent\venv\Scripts\python.exe"
function Log($m) { "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m }
function Up($p) { for ($i=0; $i -lt 80; $i++) { if (Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue) { return $true }; Start-Sleep -Milliseconds 500 }; return $false }

# 用独立数据目录，但把模型与运行时链接过去，避免污染正式账本
$home2 = "$root\.ledger-test"
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $home2 | Out-Null
New-Item -ItemType Junction -Path "$home2\models" -Target "$root\data\models" | Out-Null
New-Item -ItemType Junction -Path "$home2\runtime" -Target "$root\data\runtime" | Out-Null
Copy-Item "$root\data\identity.json" "$home2\identity.json"
@{ api_host = "127.0.0.1"; api_port = 18900; advertise = $false; price_in = 0.15; price_out = 0.60 } |
    ConvertTo-Json | Set-Content "$home2\settings.json" -Encoding UTF8

$env:AETHER_HOME = $home2
$p = Start-Process -FilePath $py -ArgumentList "run.py","--no-window","--port","18900" -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput "$home2\out.log" -RedirectStandardError "$home2\err.log"
Log "server_up=$(Up 18900)"
$base = "http://127.0.0.1:18900"

# 加载模型
$models = Invoke-RestMethod -Uri "$base/api/models" -TimeoutSec 20
$m = $models.models | Select-Object -First 1
if (-not $m) { Log "没有模型"; Stop-Process -Id $p.Id -Force; exit 1 }
Invoke-RestMethod -Uri "$base/api/models/load" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body (@{ key = $m.key } | ConvertTo-Json) | Out-Null
for ($i=0; $i -lt 40; $i++) { Start-Sleep -Seconds 2; $rt = Invoke-RestMethod -Uri "$base/api/runtime" -TimeoutSec 10; if ($rt.state -in @('ready','failed')) { break } }
Log "runtime state=$($rt.state) model=$($rt.model)"
if ($rt.state -ne 'ready') { Log "加载失败: $($rt.error)"; Stop-Process -Id $p.Id -Force; exit 1 }

$before = (Invoke-RestMethod -Uri "$base/api/ledger/summary" -TimeoutSec 10).summary.total_entries
Log "起始账本条目: $before"

# 1) 非流式请求
$body = @{ model = $rt.model; messages = @(@{ role='user'; content='说一句话。' }); max_tokens = 24; stream = $false } | ConvertTo-Json -Depth 5
$resp = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 180 -UseBasicParsing
$j = $resp.Content | ConvertFrom-Json
Log "非流式: HTTP=$($resp.StatusCode) usage=$($j.usage.prompt_tokens)+$($j.usage.completion_tokens) req=$($resp.Headers['X-Aether-Req'])"
$nonStreamUsage = "$($j.usage.prompt_tokens)|$($j.usage.completion_tokens)"

# 2) 流式请求
$sbody = @{ model = $rt.model; messages = @(@{ role='user'; content='数到三。' }); max_tokens = 30; stream = $true } | ConvertTo-Json -Depth 5
$sresp = Invoke-WebRequest -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json" -Body $sbody -TimeoutSec 180 -UseBasicParsing
$susage = $null
foreach ($line in ([string]$sresp.Content -split "`n")) {
    $t = $line.Trim()
    if ($t.StartsWith("data:") -and $t -notmatch '\[DONE\]') {
        try { $o = $t.Substring(5).Trim() | ConvertFrom-Json; if ($o.usage) { $susage = $o.usage } } catch {}
    }
}
Log "流式: 路由=$($sresp.Headers['X-Aether-Route']) 上游usage=$($susage.prompt_tokens)+$($susage.completion_tokens)"

Start-Sleep -Seconds 2
$led = Invoke-RestMethod -Uri "$base/api/ledger/entries?limit=10" -TimeoutSec 10
Log "账本新增条目:"
foreach ($e in $led.entries) {
    Log ("  role={0} model={1} in={2} out={3} amount={4} est={5} receipt={6} src={7}" -f `
        $e.role, $e.model, $e.in_tok, $e.out_tok, $e.amount, $e.estimated, $(if($e.receipt){"yes"}else{"no"}), $e.source)
}
$sum = Invoke-RestMethod -Uri "$base/api/ledger/summary" -TimeoutSec 10
Log "汇总: total=$($sum.summary.total_entries) unsettled=$($sum.summary.unsettled) serve=$($sum.summary.serve.count) consume=$($sum.summary.consume.count)"
Log "serve 侧: in=$($sum.summary.serve.in_tok) out=$($sum.summary.serve.out_tok) amount=$($sum.summary.serve.amount) estimated=$($sum.summary.serve.estimated)"

# 3) 对账
$rec = Invoke-RestMethod -Uri "$base/api/ledger/reconcile" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body '{}'
Log "对账: checked=$($rec.checked) matched=$($rec.matched) mismatched=$($rec.mismatched.Count) missing=$($rec.missing.Count)"

# 4) 结算并独立校验
$st = Invoke-RestMethod -Uri "$base/api/ledger/settle" -Method Post -ContentType "application/json" -TimeoutSec 20 -Body '{}'
if ($st.created) {
    Log "结算批次: $($st.batch.id) count=$($st.batch.count) in=$($st.batch.total_in) out=$($st.batch.total_out) amount=$($st.batch.amount)"
    Log "  merkle_root=$($st.batch.merkle_root)"
    $vf = Invoke-RestMethod -Uri "$base/api/ledger/verify" -Method Post -ContentType "application/json" -TimeoutSec 15 -Body (@{ path = $st.batch.path } | ConvertTo-Json)
    Log "  独立校验: ok=$($vf.ok) entries=$($vf.entry_count) bad=$($vf.bad_leaves.Count)"
} else { Log "结算: $($st.reason)" }

Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Remove-Item "$home2\models","$home2\runtime" -Force -ErrorAction SilentlyContinue
Log "DONE"
