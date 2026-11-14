# 打包成 Windows 可发布目录
# 用法: pwsh -File build.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "找不到项目虚拟环境：$py" }

Write-Host "== 清理旧产物 =="
Remove-Item "build", "dist" -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "== PyInstaller 构建 =="
& $py -m PyInstaller --noconfirm --clean "aether-node.spec"
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

$out = Join-Path $root "dist\AetherNode"
$exe = Join-Path $out "AetherNode.exe"
if (-not (Test-Path $exe)) { throw "构建产物缺失：$exe" }

Write-Host "== 产物 =="
$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum
"{0} 个文件，合计 {1:N0} MB" -f (Get-ChildItem $out -Recurse -File).Count, ($size / 1MB)
Write-Host "可执行文件: $exe"

Write-Host "== 冒烟测试（不启动窗口，3 秒后关闭）=="
$env:AETHER_HOME = Join-Path $root "dist\smoke-data"
$proc = Start-Process -FilePath $exe -ArgumentList "--no-window", "--port", "19222" `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root "dist\smoke.out") `
    -RedirectStandardError (Join-Path $root "dist\smoke.err")
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    if (Test-NetConnection 127.0.0.1 -Port 19222 -InformationLevel Quiet -WarningAction SilentlyContinue) { $ok = $true; break }
    Start-Sleep -Milliseconds 500
}
if ($ok) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:19222/healthz" -TimeoutSec 5
        Write-Host "  /healthz -> $($h.ok)"
        $idx = (Invoke-WebRequest "http://127.0.0.1:19222/" -UseBasicParsing).StatusCode
        Write-Host "  静态界面 -> HTTP $idx"
    } catch {
        Write-Host "  请求失败: $($_.Exception.Message)"
    }
} else {
    Write-Host "  启动超时，见 dist\smoke.err"
    Get-Content (Join-Path $root "dist\smoke.err") -Tail 20 -ErrorAction SilentlyContinue
}
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root "dist\smoke-data") -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "== 完成 =="
