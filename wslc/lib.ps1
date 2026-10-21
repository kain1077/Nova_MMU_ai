# Shared helpers for the wslc scripts (up.ps1, down.ps1, status.ps1).
# Dot-source this file; it does nothing on its own.
#
# Compatible with Windows PowerShell 5.1 and PowerShell 7.

$ErrorActionPreference = 'Stop'
$script:WslcDir  = $PSScriptRoot
$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:Wslc     = Join-Path $env:ProgramFiles 'WSL\wslc.exe'

# Every container, volume and network these scripts create or remove must
# start with this, so down.ps1 can never remove anything that is not MMU's.
$script:NamePrefix = 'mmu-'

# Server defaults, copied from docker-compose.yml. The server gets exactly
# this set of variables, the same set compose passes it. One default differs:
# under wslc, host.docker.internal resolves but refuses connections, so the
# embedding endpoint defaults to host.wslc.internal instead.
$script:ServerDefaults = [ordered]@{
    MMU_EMBEDDING_ENABLED       = 'true'
    MMU_EMBEDDING_BASE          = 'http://host.wslc.internal:1234/v1'
    MMU_EMBEDDING_MODEL         = 'text-embedding-nomic-embed-text-v1.5'
    MMU_EMBEDDING_API_KEY       = ''
    MMU_API_KEY                 = ''
    MMU_CORS_ORIGINS            = ''
    MMU_EMBEDDING_DIM           = '768'
    MMU_SEMANTIC_FLOOR          = '0.0'
    MMU_SEMANTIC_WEIGHT         = '0.80'
    MMU_WEB_ENABLED             = 'false'
    MMU_HOLD_THRESH             = '20'
    MMU_ARCHIVE_THRESH          = '20'
    MMU_ARCHIVE_MIN_DAYS        = '14'
    MMU_AGING_MIN_MEMORIES      = '25'
    MMU_ANTICIPATE_MAX          = '3'
    MMU_ANTICIPATE_PER_SESSION  = '5'
    MMU_ALLOW_MODEL_CRYSTALLIZE = 'false'
}

function Read-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) {
        throw "No .env at $Path. Copy .env.example to .env and set NEO4J_PASS."
    }
    $vals = [ordered]@{}
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $v = $Matches[2].Trim()
            if ($v.Length -ge 2 -and $v[0] -eq $v[-1] -and ($v[0] -eq '"' -or $v[0] -eq "'")) {
                $v = $v.Substring(1, $v.Length - 2)
            }
            $vals[$Matches[1]] = $v
        }
    }
    $vals
}

function Get-Val($vals, [string]$key, [string]$default) {
    if ($vals.Contains($key) -and $vals[$key] -ne '') { return $vals[$key] }
    return $default
}

# Relative paths resolve against the repo root, as compose resolves them
# against the folder holding docker-compose.yml.
function Resolve-RepoPath([string]$p) {
    if ([IO.Path]::IsPathRooted($p)) { return [IO.Path]::GetFullPath($p) }
    return [IO.Path]::GetFullPath((Join-Path $script:RepoRoot $p))
}

function Get-WslcConfig([string]$EnvFile) {
    $e = Read-DotEnv $EnvFile
    $prefix = Get-Val $e 'WSLC_VOLUME_PREFIX' $script:NamePrefix
    $bind = Get-Val $e 'MMU_BIND' '127.0.0.1'
    [pscustomobject]@{
        Env          = $e
        Pass         = Get-Val $e 'NEO4J_PASS' ''
        Bind         = $bind
        # Where this machine reaches the published ports.
        ProbeHost    = $(if ($bind -eq '0.0.0.0') { '127.0.0.1' } else { $bind })
        ApiKey       = Get-Val $e 'MMU_API_KEY' ''
        MmuPort      = [int](Get-Val $e 'MMU_PORT' '8765')
        HttpPort     = [int](Get-Val $e 'NEO4J_HTTP_PORT' '7474')
        BoltPort     = [int](Get-Val $e 'NEO4J_BOLT_PORT' '7687')
        Neo4jName    = Get-Val $e 'MMU_NEO4J_NAME' 'mmu-neo4j'
        ServerName   = Get-Val $e 'MMU_SERVER_NAME' 'mmu-memory-server'
        Network      = Get-Val $e 'WSLC_NETWORK' 'mmu-net'
        VolNeo4jData = "${prefix}neo4j-data"
        VolNeo4jLogs = "${prefix}neo4j-logs"
        VolMmuData   = "${prefix}mmu-data"
        ServerImage  = Get-Val $e 'WSLC_SERVER_IMAGE' 'mmu-server:wslc'
        Neo4jImage   = Get-Val $e 'WSLC_NEO4J_IMAGE' 'neo4j:5.18-community'
        BuildContext = Resolve-RepoPath (Get-Val $e 'WSLC_BUILD_CONTEXT' '.')
        DocHost      = Resolve-RepoPath (Get-Val $e 'MMU_DOC_HOST' './documents')
        ReadyTimeout = [int](Get-Val $e 'WSLC_READY_TIMEOUT' '240')
    }
}

function Assert-WslcConfig($c, [switch]$NeedPass) {
    if (-not (Test-Path $script:Wslc)) {
        throw "wslc not found at $script:Wslc. It ships with WSL 3.0.1 or newer: run 'wsl --update', then open a new terminal."
    }
    if ($NeedPass -and ($c.Pass -eq '' -or $c.Pass -eq 'change-me')) {
        throw 'NEO4J_PASS is empty or still change-me. Set it in .env.'
    }
    $ports = @($c.MmuPort, $c.HttpPort, $c.BoltPort)
    if (@($ports | Select-Object -Unique).Count -ne 3) {
        throw "MMU_PORT, NEO4J_HTTP_PORT and NEO4J_BOLT_PORT must be three different ports (got $($ports -join ', '))."
    }
    foreach ($n in @($c.Neo4jName, $c.ServerName, $c.Network, $c.VolNeo4jData, $c.VolNeo4jLogs, $c.VolMmuData)) {
        if (-not $n.StartsWith($script:NamePrefix)) {
            throw "Name $n does not start with $script:NamePrefix. Refusing, so down.ps1 can only ever remove MMU's own containers, volumes and network."
        }
    }
    # Same rule as .env.example: never expose the API beyond this machine
    # without a key.
    if ($c.Bind -ne '127.0.0.1' -and $c.ApiKey -eq '') {
        throw "MMU_BIND=$($c.Bind) with no MMU_API_KEY would expose an unauthenticated API. Set a key or bind 127.0.0.1."
    }
}

# Quote one argument the way CommandLineToArgvW / the MSVC runtime parse it.
# Done by hand because Windows PowerShell 5.1 does not escape embedded double
# quotes when it calls a native program.
function ConvertTo-WinArg([string]$a) {
    if ($a -ne '' -and $a -notmatch '[\s"]') { return $a }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $bs = 0
    foreach ($ch in $a.ToCharArray()) {
        if ($ch -eq '\') { $bs++; continue }
        if ($ch -eq '"') {
            [void]$sb.Append(('\' * (2 * $bs + 1)) + '"')
            $bs = 0
            continue
        }
        if ($bs) { [void]$sb.Append('\' * $bs); $bs = 0 }
        [void]$sb.Append($ch)
    }
    [void]$sb.Append(('\' * (2 * $bs)) + '"')
    $sb.ToString()
}

function Hide-Secrets([string]$s) {
    if ($script:Config) {
        foreach ($secret in @($script:Config.Pass, $script:Config.ApiKey,
                              (Get-Val $script:Config.Env 'MMU_EMBEDDING_API_KEY' ''))) {
            if ($secret) { $s = $s.Replace($secret, '***') }
        }
    }
    $s
}

# Run wslc with captured output. Returns Code/Out/Err; throws on a nonzero
# exit unless -AllowFail.
function Invoke-Wslc {
    param([string[]]$WArgs, [switch]$AllowFail, [switch]$Quiet)
    if (-not $Quiet) {
        Write-Host ('> wslc ' + (Hide-Secrets (($WArgs | ForEach-Object { ConvertTo-WinArg $_ }) -join ' '))) -ForegroundColor DarkCyan
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName  = $script:Wslc
    $psi.Arguments = ($WArgs | ForEach-Object { ConvertTo-WinArg $_ }) -join ' '
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.ReadToEndAsync()
    $out = $p.StandardOutput.ReadToEnd()
    $p.WaitForExit()
    $res = [pscustomobject]@{ Code = $p.ExitCode; Out = $out.TrimEnd(); Err = $errTask.Result.TrimEnd() }
    if ($res.Code -ne 0 -and -not $AllowFail) {
        throw ("wslc $($WArgs[0]) $($WArgs[1]) failed (exit $($res.Code)):`n" + (Hide-Secrets "$($res.Err)`n$($res.Out)"))
    }
    $res
}

# Run wslc with output streamed to the console (build, pull). Returns the exit code.
function Invoke-WslcLive([string[]]$WArgs) {
    Write-Host ('> wslc ' + (Hide-Secrets (($WArgs | ForEach-Object { ConvertTo-WinArg $_ }) -join ' '))) -ForegroundColor DarkCyan
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $script:Wslc @WArgs 2>&1 | ForEach-Object { Write-Host "  $_" }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
}

function Test-WslcObject([string]$name) {
    (Invoke-Wslc -WArgs @('inspect', $name) -AllowFail -Quiet).Code -eq 0
}

# State of a container, or $null if it does not exist.
function Get-ContainerState([string]$name) {
    $r = Invoke-Wslc -WArgs @('inspect', $name) -AllowFail -Quiet
    if ($r.Code -ne 0) { return $null }
    $j = $r.Out | ConvertFrom-Json
    if ($j -is [array]) { $j = $j[0] }
    if (-not $j.State) { return $null }   # same name, but not a container
    $j.State
}

function Get-PortListener([int]$port) {
    Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object LocalAddress, LocalPort, OwningProcess,
            @{ n = 'Process'; e = { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName } }
}

# Throw if any of these ports already has a listener on this machine.
function Assert-PortsFree([int[]]$ports) {
    foreach ($p in $ports) {
        $l = @(Get-PortListener $p)
        if ($l) {
            throw "Port $p is already in use by $($l[0].Process) (PID $($l[0].OwningProcess)) on $($l[0].LocalAddress). Change it in .env, or stop whatever is using it."
        }
    }
}

# Write an env-file with LF endings. A CRLF file would leave '\r' on every value.
function Write-EnvFile([string]$path, $pairs) {
    $text = (($pairs.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`n") + "`n"
    [IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# Poll a URL until it answers 2xx. Returns seconds waited, or throws.
function Wait-Http([string]$url, [int]$timeout, [string]$what) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeout) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $url
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) { return [math]::Round($sw.Elapsed.TotalSeconds, 1) }
        } catch { }
        Start-Sleep -Milliseconds 1000
    }
    throw "$what did not answer at $url within $timeout s."
}

function Format-Seconds($sw) { '{0:N1} s' -f $sw.Elapsed.TotalSeconds }
