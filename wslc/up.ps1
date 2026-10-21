<#
.SYNOPSIS
  Start MMU (Neo4j + MMU server) under WSL containers (wslc), without Docker.

.DESCRIPTION
  The wslc equivalent of `docker compose up -d`, reading the same .env at the
  repo root. Safe to re-run: it builds only a missing image, creates only
  missing networks and volumes, starts stopped containers, and leaves running
  ones alone.

  Compose features with no wslc equivalent, and what this script does instead:
    depends_on: service_healthy  -> waits for Neo4j HTTP, then for a real bolt
                                    query, before starting the server
    restart: unless-stopped      -> none; run up.ps1 again after a reboot or
                                    `wslc system session terminate`
    extra_hosts host-gateway     -> none; MMU_EMBEDDING_BASE must use
                                    host.wslc.internal instead

.PARAMETER Rebuild
  Rebuild the server image even if it exists. Needed after updating MMU.
.PARAMETER RecreateServer
  Remove and re-create the server container (data is kept). Needed after
  changing any MMU_* value in .env, e.g. turning embeddings on.
#>
param(
    [string]$EnvFile = (Join-Path (Split-Path -Parent $PSScriptRoot) '.env'),
    [switch]$Rebuild,
    [switch]$RecreateServer
)

. (Join-Path $PSScriptRoot 'lib.ps1')
$script:Config = $c = Get-WslcConfig $EnvFile
Assert-WslcConfig $c -NeedPass

$total = [Diagnostics.Stopwatch]::StartNew()
$rt = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Force $rt | Out-Null
$log = Join-Path $rt ('up_{0:yyyyMMdd_HHmmss}.log' -f (Get-Date))
Start-Transcript -Path $log | Out-Null

try {
    Write-Host "== wslc: $((Invoke-Wslc -WArgs @('--version') -Quiet).Out)"

    # -- 1. Ports -----------------------------------------------------
    # A container of ours that is already running owns its own ports.
    $neoState = Get-ContainerState $c.Neo4jName
    $srvState = Get-ContainerState $c.ServerName
    $checks = @()
    if (-not ($neoState -and $neoState.Running)) { $checks += $c.HttpPort, $c.BoltPort }
    if (-not ($srvState -and $srvState.Running)) { $checks += $c.MmuPort }
    Assert-PortsFree $checks
    if ($checks) { Write-Host "== ports free: $($checks -join ', ')" }

    # -- 2. Server image ----------------------------------------------
    if ($Rebuild -or -not (Test-WslcObject $c.ServerImage)) {
        if (-not (Test-Path (Join-Path $c.BuildContext 'Dockerfile'))) {
            throw "No Dockerfile in build context $($c.BuildContext)."
        }
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $code = Invoke-WslcLive @('build', '-t', $c.ServerImage, '-f', (Join-Path $c.BuildContext 'Dockerfile'), $c.BuildContext)
        if ($code -ne 0) { throw "wslc build failed (exit $code). Output is above and in $log." }
        Write-Host "== built $($c.ServerImage) in $(Format-Seconds $sw)"
    } else {
        Write-Host "== image $($c.ServerImage) present (use -Rebuild to rebuild)"
    }

    # -- 3. Network and volumes ---------------------------------------
    if (-not (Test-WslcObject $c.Network)) {
        Invoke-Wslc -WArgs @('network', 'create', $c.Network) | Out-Null
    } else { Write-Host "== network $($c.Network) exists" }
    foreach ($v in @($c.VolNeo4jData, $c.VolNeo4jLogs, $c.VolMmuData)) {
        if (-not (Test-WslcObject $v)) {
            Invoke-Wslc -WArgs @('volume', 'create', $v) | Out-Null
        } else { Write-Host "== volume $v exists" }
    }
    if (-not (Test-Path $c.DocHost)) { New-Item -ItemType Directory -Force $c.DocHost | Out-Null }

    # -- 4. Env files (LF endings; values never pass through a shell) --
    $neoEnv = Join-Path $rt 'neo4j.env'
    Write-EnvFile $neoEnv ([ordered]@{
        NEO4J_AUTH                                  = "neo4j/$($c.Pass)"
        NEO4J_PLUGINS                               = '["apoc"]'
        NEO4J_apoc_export_file_enabled              = 'true'
        NEO4J_apoc_import_file_enabled              = 'true'
        NEO4J_dbms_security_procedures_unrestricted = 'apoc.*'
    })
    $srv = [ordered]@{
        MMU_INDEX_PATH = '/data/memory_index_v2.json'
        NEO4J_URI      = "bolt://$($c.Neo4jName):7687"
        NEO4J_USER     = 'neo4j'
        NEO4J_PASS     = $c.Pass
        NEO4J_ENABLED  = 'true'
        MMU_DOC_ROOT   = '/docs'
    }
    foreach ($k in $script:ServerDefaults.Keys) { $srv[$k] = Get-Val $c.Env $k $script:ServerDefaults[$k] }
    $srvEnv = Join-Path $rt 'server.env'
    Write-EnvFile $srvEnv $srv
    Write-Host "== server env: embeddings=$($srv.MMU_EMBEDDING_ENABLED) base=$($srv.MMU_EMBEDDING_BASE) neo4j=$($srv.NEO4J_URI)"
    if ($srv.MMU_EMBEDDING_ENABLED -eq 'true' -and $srv.MMU_EMBEDDING_BASE -match 'host\.docker\.internal') {
        Write-Host "!! MMU_EMBEDDING_BASE uses host.docker.internal, which refuses connections under wslc. Use http://host.wslc.internal:1234/v1." -ForegroundColor Yellow
    }

    # -- 5. Neo4j -----------------------------------------------------
    if ($neoState -and $neoState.Running) {
        Write-Host "== $($c.Neo4jName) already running"
    } elseif ($neoState) {
        Invoke-Wslc -WArgs @('start', $c.Neo4jName) | Out-Null
    } else {
        Invoke-Wslc -WArgs @(
            'run', '-d', '--name', $c.Neo4jName, '--network', $c.Network,
            '-p', "$($c.Bind):$($c.HttpPort):7474",
            '-p', "$($c.Bind):$($c.BoltPort):7687",
            '-v', "$($c.VolNeo4jData):/data",
            '-v', "$($c.VolNeo4jLogs):/logs",
            '--env-file', $neoEnv,
            '--health-cmd', 'wget -q --spider http://127.0.0.1:7474',
            '--health-interval', '30s', '--health-timeout', '10s',
            '--health-retries', '5', '--health-start-period', '60s',
            $c.Neo4jImage
        ) | Out-Null
    }

    # compose waited on the healthcheck. Here: HTTP first, then a real bolt
    # query, because HTTP answering does not mean bolt accepts sessions yet,
    # and MMU's startup index reconcile runs once and is skipped if it cannot
    # read the graph.
    $t = Wait-Http "http://$($c.ProbeHost):$($c.HttpPort)" $c.ReadyTimeout 'Neo4j HTTP'
    Write-Host "== Neo4j HTTP answered after $t s"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $q = Invoke-Wslc -WArgs @('exec', $c.Neo4jName, 'cypher-shell', '-u', 'neo4j', '-p', $c.Pass, 'RETURN 1') -AllowFail -Quiet
        if ($q.Code -eq 0) { break }
        if ($sw.Elapsed.TotalSeconds -gt $c.ReadyTimeout) {
            throw ("Neo4j bolt did not accept a query within $($c.ReadyTimeout) s:`n" + (Hide-Secrets $q.Err))
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "== Neo4j bolt answered a query after a further $(Format-Seconds $sw)"

    # -- 6. MMU server ------------------------------------------------
    if ($srvState -and $RecreateServer) {
        Invoke-Wslc -WArgs @('stop', '-t', '30', $c.ServerName) -AllowFail | Out-Null
        Invoke-Wslc -WArgs @('rm', $c.ServerName) | Out-Null
        $srvState = $null
        Assert-PortsFree @($c.MmuPort)
    }
    if ($srvState -and $srvState.Running) {
        Write-Host "== $($c.ServerName) already running"
    } elseif ($srvState) {
        Invoke-Wslc -WArgs @('start', $c.ServerName) | Out-Null
    } else {
        Invoke-Wslc -WArgs @(
            'run', '-d', '--name', $c.ServerName, '--network', $c.Network,
            '-p', "$($c.Bind):$($c.MmuPort):8765",
            '-v', "$($c.VolMmuData):/data",
            '--mount', "type=bind,source=$($c.DocHost),target=/docs,readonly",
            '--env-file', $srvEnv,
            '--health-cmd', "python -c `"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')`"",
            '--health-interval', '30s', '--health-timeout', '5s', '--health-retries', '3',
            $c.ServerImage
        ) | Out-Null
    }

    $base = "http://$($c.ProbeHost):$($c.MmuPort)"
    $t = Wait-Http "$base/health" $c.ReadyTimeout 'MMU /health'
    $h = Invoke-RestMethod -TimeoutSec 10 "$base/health"
    Write-Host "== MMU /health: status=$($h.status) memories=$($h.total_memories) neo4j=$($h.neo4j.status) (answered after $t s)"
    Write-Host "== MMU is at $base"
    Write-Host "== up.ps1 total: $(Format-Seconds $total)  (log: $log)" -ForegroundColor Green
}
catch {
    Write-Host "!! $(Hide-Secrets $_.Exception.Message)" -ForegroundColor Red
    Write-Host "!! up.ps1 stopped after $(Format-Seconds $total). Containers are left as they are, for diagnosis." -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 1
}
Stop-Transcript | Out-Null
