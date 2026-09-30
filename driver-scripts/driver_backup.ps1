# 管理者権限チェック
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Please run as Administrator." -ForegroundColor Red
    exit 1
}

# このスクリプトがあるフォルダ(USBメモリ)直下に drivers を作成
$driverBackupPath = Join-Path $PSScriptRoot "drivers"
if (-not (Test-Path $driverBackupPath)) {
    New-Item -Path $driverBackupPath -ItemType Directory -Force | Out-Null
}

Write-Host "Starting driver backup..." -ForegroundColor Cyan
Write-Host "Destination: $driverBackupPath" -ForegroundColor Cyan

# 現在使用中のサードパーティドライバをエクスポート
Export-WindowsDriver -Online -Destination $driverBackupPath

Write-Host "Backup complete. Counting exported drivers..." -ForegroundColor Green
$count = (Get-ChildItem -Path $driverBackupPath -Directory).Count
Write-Host "Exported driver folders: $count" -ForegroundColor Green
