<#
.SYNOPSIS
  Stop and remove MMU's wslc containers. Keeps all data unless -RemoveData.

.DESCRIPTION
  The wslc equivalent of `docker compose down`. The server is stopped first,
  with a 30 s grace period so MMU's shutdown hook can flush its index, then
  Neo4j.

  It touches only the two containers named in .env (MMU_SERVER_NAME,
  MMU_NEO4J_NAME) and, with -RemoveData, the three volumes and the network
  named from it. Every one of those names must start with mmu- (enforced by
  lib.ps1), so nothing else in wslc can be removed by this script.

.PARAMETER RemoveData
  Also delete the volumes (all memories and the Neo4j database) and the
  network. Cannot be undone.
#>
param(
    [string]$EnvFile = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env'),
    [switch]$RemoveData
)

. (Join-Path $PSScriptRoot 'lib.ps1')
$script:Config = $c = Get-WslcConfig $EnvFile
Assert-WslcConfig $c

$total = [Diagnostics.Stopwatch]::StartNew()
foreach ($name in @($c.ServerName, $c.Neo4jName)) {
    $st = Get-ContainerState $name
    if (-not $st) { Write-Host "== ${name}: not present"; continue }
    if ($st.Running) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        Invoke-Wslc -WArgs @('stop', '-t', '30', $name) | Out-Null
        Write-Host "== $name stopped in $(Format-Seconds $sw)"
    }
    if ($name -eq $c.ServerName) {
        # Did uvicorn get SIGTERM and run its shutdown hooks, or was it killed?
        $logs = Invoke-Wslc -WArgs @('logs', '--tail', '15', $name) -AllowFail -Quiet
        $graceful = ("$($logs.Out)`n$($logs.Err)" -match 'Application shutdown complete')
        Write-Host "== MMU shut down gracefully: $graceful"
    }
    Invoke-Wslc -WArgs @('rm', $name) | Out-Null
}

if ($RemoveData) {
    foreach ($v in @($c.VolMmuData, $c.VolNeo4jData, $c.VolNeo4jLogs)) {
        if (Test-WslcObject $v) { Invoke-Wslc -WArgs @('volume', 'remove', $v) | Out-Null }
        else { Write-Host "== volume ${v}: not present" }
    }
    if (Test-WslcObject $c.Network) { Invoke-Wslc -WArgs @('network', 'remove', $c.Network) | Out-Null }
    Write-Host '== data and network removed' -ForegroundColor Yellow
} else {
    Write-Host "== data kept: $($c.VolMmuData), $($c.VolNeo4jData), $($c.VolNeo4jLogs); network $($c.Network) kept"
}
Write-Host "== down.ps1 total: $(Format-Seconds $total)" -ForegroundColor Green
