param(
    [ValidateSet('start','stop','status')]
    [string]$Command = 'status'
)

$ErrorActionPreference = 'Stop'
$Python = 'C:\Users\Administrator\.venvs\labor-contract-ocr\Scripts\python.exe'
$Script = Join-Path $PSScriptRoot 'hr_local_bridge.py'
$Runtime = 'C:\Users\Administrator\.claude-to-im\runtime'
$PidFile = Join-Path $Runtime 'hr-contract-bridge.pid'
$StatusFile = Join-Path $Runtime 'hr-contract-bridge-status.json'

function Import-UserVariable([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name, 'User')
    if ($value) { Set-Item -Path "Env:$Name" -Value $value }
}

function Get-ListenerProcess {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $pidValue = 0
    if (-not [int]::TryParse($raw, [ref]$pidValue)) { return $null }
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction Stop
        if ($process.CommandLine -notlike '*hr_local_bridge.py*') { return $null }
        return $process
    } catch { return $null }
}

New-Item -ItemType Directory -Path $Runtime -Force | Out-Null

if ($Command -eq 'start') {
    if (Get-ListenerProcess) { Write-Output '{"running":true,"already_running":true}'; exit 0 }
    foreach ($name in @('HR_FEISHU_APP_ID','HR_FEISHU_APP_SECRET','DASHSCOPE_KEY')) { Import-UserVariable $name }
    if (-not $env:HR_FEISHU_APP_ID -or -not $env:HR_FEISHU_APP_SECRET -or -not $env:DASHSCOPE_KEY) {
        throw 'HR contract listener credentials are incomplete.'
    }
    $proc = Start-Process -FilePath $Python -ArgumentList @($Script) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $PidFile -Value $proc.Id -Encoding ascii
    Start-Sleep -Seconds 3
    if (-not (Get-ListenerProcess)) { throw 'HR contract listener failed to start.' }
    Write-Output ('{"running":true,"pid":' + $proc.Id + '}')
    exit 0
}

if ($Command -eq 'stop') {
    $process = Get-ListenerProcess
    if ($process) { Stop-Process -Id $process.ProcessId -Force }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Output '{"running":false}'
    exit 0
}

$process = Get-ListenerProcess
$status = $null
if (Test-Path -LiteralPath $StatusFile) {
    try { $status = Get-Content -LiteralPath $StatusFile -Raw | ConvertFrom-Json } catch { $status = $null }
}
$healthy = $false
if ($process -and $status) {
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$status.heartbeat_at
    $healthy = $status.state -eq 'connected' -and $age -le 90
}
[pscustomobject]@{
    running = [bool]$process
    healthy = $healthy
    pid = if ($process) { $process.ProcessId } else { $null }
    state = if ($status) { $status.state } else { 'missing' }
    error = if ($status) { $status.error } else { $null }
} | ConvertTo-Json -Compress
if ($healthy) { exit 0 } else { exit 1 }
