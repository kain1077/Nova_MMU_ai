<#
.SYNOPSIS
  Show MMU's wslc containers, volumes, listening sockets, and MMU health.
  Read-only. Contacts only the ports named in .env.
#>
param([string]$EnvFile = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env'))

. (Join-Path $PSScriptRoot 'lib.ps1')
$script:Config = $c = Get-WslcConfig $EnvFile
Assert-WslcConfig $c

Write-Host '== containers'
foreach ($name in @($c.Neo4jName, $c.ServerName)) {
    $st = Get-ContainerState $name
    if (-not $st) { Write-Host ("  {0,-18} not present" -f $name); continue }
    $health = if ($st.Health) { $st.Health.Status } else { 'n/a' }
    Write-Host ("  {0,-18} {1,-9} health={2,-10} started={3}" -f $name, $st.Status, $health, $st.StartedAt)
}

Write-Host '== volumes and network'
foreach ($n in @($c.VolNeo4jData, $c.VolNeo4jLogs, $c.VolMmuData, $c.Network)) {
    Write-Host ("  {0,-22} {1}" -f $n, $(if (Test-WslcObject $n) { 'present' } else { 'missing' }))
}

Write-Host "== listening sockets (should be $($c.Bind) only)"
foreach ($p in @($c.MmuPort, $c.HttpPort, $c.BoltPort)) {
    $l = @(Get-PortListener $p)
    if (-not $l) { Write-Host "  :$p  not listening"; continue }
    foreach ($x in $l) { Write-Host ("  {0}:{1}  {2} (PID {3})" -f $x.LocalAddress, $x.LocalPort, $x.Process, $x.OwningProcess) }
}

$base = "http://$($c.ProbeHost):$($c.MmuPort)"
$headers = @{}
if ($c.ApiKey) { $headers['X-MMU-Key'] = $c.ApiKey }
foreach ($ep in @('/health', '/embedding_status')) {
    Write-Host "== GET $ep"
    try {
        $r = Invoke-RestMethod -TimeoutSec 10 -Headers $headers "$base$ep"
        Write-Host (Hide-Secrets ($r | ConvertTo-Json -Depth 6))
    } catch {
        Write-Host "  unreachable: $($_.Exception.Message)"
    }
}
