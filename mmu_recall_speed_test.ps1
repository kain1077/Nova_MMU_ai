<#
mmu_recall_speed_test.ps1
==========================
Re-runs the recall-latency probe set against the live MMU server and reports
read_ms (the server's own internal timing, the same field every phase's deploy
report has compared against) alongside full round-trip time.

Two probe sets, run together by default:

  LEGACY -- the exact 7 prompts used in the Phase 9 and Phase 11 deploy reports.
  Kept unchanged on purpose so today's numbers land on the same historical table:

    Phase 3 baseline    (~55 memories)   3.8 ms hot path
    Phase 9  before      (~95 memories)  42.8 - 119.3 ms
    Phase 9  after       (~95 memories)  23.9 - 61.2 ms
    Phase 11 after ingest (954 memories) ~130 ms

  NEW -- covers the parts of the graph the legacy set never touched: four of the
  five ingested physics papers (only Dimensional Relativity was in the legacy
  set; Entropic, Fractional, Geometric and Universal Relativity were added
  later and have no probe of their own until now), plus the one verified
  paraphrase probe from the Phase 11 report (Shapiro delay, phrased without any
  of its own keywords, to exercise the semantic path rather than the gate).
  This set has no historical baseline yet -- this run IS the baseline for it,
  going forward.

Results print and save with a Set column (Legacy/New) so the two stay easy to
tell apart, and summary stats are reported for each set separately as well as
combined, since only the Legacy numbers are directly comparable to the table
above.

Also runs a one-call localhost-vs-127.0.0.1 latency diagnostic before the probe
set (see -SkipLatencyDiagnostic), and gets the current memory count straight
from Neo4j if /insights' shape doesn't match what's expected -- both added
after a run where /insights.total_memories didn't exist (real field is nested
under totals.memories) and round-trip time ran ~2 seconds against a read_ms
under 155ms, worth running down since it would affect every real recall Nova
makes, not just this script.

SAFETY NOTE -- read before running:
_age_memories() bumps a use-counter on every non-recalled, non-Document memory
on EVERY recall call, and MMU_ARCHIVE_THRESH is 20. Heavy test-recall bursts
archived the user's conversational memories twice during Phase 9 and Phase 11
validation. The combined default probe set is 12 prompts (still under 20 for
one pass), and the script warns loudly if -Repeat would push the total recall
count close to or past the threshold. It also prints the conversational colour
distribution before and after so you can eyeball whether anything got archived.

USAGE
  .\mmu_recall_speed_test.ps1                       # legacy + new, one pass
  .\mmu_recall_speed_test.ps1 -LegacyOnly            # historical comparison only
  .\mmu_recall_speed_test.ps1 -NewOnly               # paper-coverage probes only
  .\mmu_recall_speed_test.ps1 -Repeat 2
  .\mmu_recall_speed_test.ps1 -Prompts "who am I married to","Star Wars"
  .\mmu_recall_speed_test.ps1 -SkipColorCheck
#>

param(
    [string]$BaseUrl = "http://127.0.0.1:8765",
    [int]$TopK = 10,
    [int]$Repeat = 1,

    [string[]]$LegacyPrompts = @(
        "Star Wars",
        "Dimensional Relativity Theory physics paper",
        "Castlevania Symphony of the Night video games",
        "what is the user's game design focus",
        "tell me about my pet",
        "who am I married to",
        "how does entropy relate to the cosmological constant"
    ),
    [string[]]$NewPrompts = @(
        "Entropic Relativity Theory paper",
        "Fractional Relativity Theory paper",
        "Geometric Relativity Theory paper",
        "Universal Relativity Theory paper",
        "why does a signal slow down when it travels close to a heavy star"
    ),

    # Overrides both default sets above entirely -- everything runs under Set="Custom".
    [string[]]$Prompts,

    [switch]$LegacyOnly,
    [switch]$NewOnly,

    [switch]$SkipColorCheck,
    [switch]$SkipLatencyDiagnostic,
    [string]$Neo4jContainer = "mmu-neo4j",
    [string]$Neo4jUser = "neo4j",
    # Read from the environment, never baked in. This defaulted to a real
    # password and was committed; a credential in a repo is a credential
    # published, whether or not the repo was public yet.
    #   PowerShell:  $env:NEO4J_PASS = "..."   (or pass -Neo4jPass)
    [string]$Neo4jPass = $(if ($env:NEO4J_PASS) { $env:NEO4J_PASS }
                           else { throw "Set NEO4J_PASS or pass -Neo4jPass." }),
    [string]$ResultsDir = "scratchpad"
)

$ErrorActionPreference = "Stop"
$ArchiveThresh = 20   # matches MMU_ARCHIVE_THRESH's default; the real value could
                       # differ if the user has overridden it -- this is a heuristic warning,
                       # not an authoritative check.

# ── Build the working probe list, tagged by Set ──
$probeList = @()
if ($Prompts) {
    foreach ($p in $Prompts) { $probeList += [PSCustomObject]@{ Prompt = $p; Set = "Custom" } }
} else {
    if (-not $NewOnly) {
        foreach ($p in $LegacyPrompts) { $probeList += [PSCustomObject]@{ Prompt = $p; Set = "Legacy" } }
    }
    if (-not $LegacyOnly) {
        foreach ($p in $NewPrompts) { $probeList += [PSCustomObject]@{ Prompt = $p; Set = "New" } }
    }
}

Write-Host "=== MMU recall speed test ===" -ForegroundColor Cyan
Write-Host "Base URL: $BaseUrl | top_k=$TopK | prompts=$($probeList.Count) | repeat=$Repeat`n"

$totalRecalls = $probeList.Count * $Repeat
Write-Host "This run will fire $totalRecalls /recall calls." -ForegroundColor Yellow
if ($totalRecalls -ge $ArchiveThresh) {
    Write-Host "WARNING: $totalRecalls >= the default MMU_ARCHIVE_THRESH ($ArchiveThresh)." -ForegroundColor Red
    Write-Host "Conversational memories not recalled in this run could get archived (turned Blue)." -ForegroundColor Red
    Write-Host "This happened twice before, during Phase 9 and Phase 11 validation. Consider -LegacyOnly, -NewOnly, or a smaller -Repeat." -ForegroundColor Red
} elseif ($totalRecalls -ge ($ArchiveThresh - 5)) {
    Write-Host "Note: getting close to the archive threshold ($ArchiveThresh). Fine for one run, watch if you repeat this." -ForegroundColor Yellow
}

# ── Localhost latency diagnostic ──
# read_ms (server-internal) and full round-trip time can differ by a lot on
# Windows for reasons that have nothing to do with the MMU code: .NET's
# HttpClient resolving "localhost" can eat 1-2+ seconds trying IPv6 before
# falling back to IPv4, and WPAD proxy auto-detection can add a similar
# delay per request. Both are well-known, both are invisible to read_ms
# because they happen before the request ever reaches the server. One quick
# comparison call against 127.0.0.1 tells you immediately whether this is in
# play -- if 127.0.0.1 is dramatically faster than localhost, that is the
# cause, and the fix is switching every MMU_BASE / LMSTUDIO_BASE (server,
# idle daemon, MCP bridge) from "localhost" to "127.0.0.1".
if (-not $SkipLatencyDiagnostic) {
    Write-Host "--- Localhost latency diagnostic (one /health call each) ---" -ForegroundColor Cyan
    $hostBase = $BaseUrl -replace "localhost", "127.0.0.1"
    try {
        $swA = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get | Out-Null
        $swA.Stop()
        $swB = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-RestMethod -Uri "$hostBase/health" -Method Get | Out-Null
        $swB.Stop()
        $msA = [math]::Round($swA.Elapsed.TotalMilliseconds, 1)
        $msB = [math]::Round($swB.Elapsed.TotalMilliseconds, 1)
        Write-Host "  $BaseUrl  ->  ${msA}ms"
        Write-Host "  $hostBase  ->  ${msB}ms"
        if ($msA -gt ($msB * 3) -and $msA -gt 500) {
            Write-Host "  '127.0.0.1' is much faster than 'localhost' -- this looks like the" -ForegroundColor Yellow
            Write-Host "  IPv6-then-fallback DNS delay. Consider switching MMU_BASE (and" -ForegroundColor Yellow
            Write-Host "  LMSTUDIO_BASE) to 127.0.0.1 everywhere -- this affects every real" -ForegroundColor Yellow
            Write-Host "  recall/remember/rate call Nova makes, not just this test script." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  Diagnostic call failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
    Write-Host ""
}

# ── Current graph size, for context on where these numbers should land ──
# /insights nests the total under `totals.memories`, not a top-level field --
# tried in order, falling back to a direct Neo4j count (always correct,
# regardless of how /insights' shape has changed since this script was written).
$totalMemories = $null
try {
    $insights = Invoke-RestMethod -Uri "$BaseUrl/insights" -Method Get
    if ($insights.totals -and $insights.totals.memories) {
        $totalMemories = $insights.totals.memories
    } elseif ($insights.total_memories) {
        $totalMemories = $insights.total_memories
    }
} catch {
    # fall through to the Neo4j fallback below
}
if (-not $totalMemories) {
    try {
        $countOut = docker exec $Neo4jContainer cypher-shell -u $Neo4jUser -p $Neo4jPass `
            "MATCH (m:Memory) RETURN count(m) AS total" 2>$null
        # cypher-shell prints a header line then the value; take the last non-empty line
        $lastLine = ($countOut | Where-Object { $_.Trim() -ne "" } | Select-Object -Last 1)
        if ($lastLine) { $totalMemories = $lastLine.Trim() }
    } catch {
        # leave as unknown
    }
}
if (-not $totalMemories) { $totalMemories = "unknown (/insights had no recognizable field and the Neo4j fallback also failed)" }

Write-Host "Current graph size: $totalMemories memories" -ForegroundColor Cyan
Write-Host "Historical reference points (Legacy set only):"
Write-Host "  Phase 3 baseline      (~55 memories)   3.8 ms hot path"
Write-Host "  Phase 9  before embed (~95 memories)   42.8 - 119.3 ms"
Write-Host "  Phase 9  after embed  (~95 memories)   23.9 - 61.2 ms"
Write-Host "  Phase 11 after ingest (954 memories)   ~130 ms`n"

# ── Colour distribution before, so we can catch accidental archiving ──
if (-not $SkipColorCheck) {
    Write-Host "--- Conversational (non-Document) colour distribution BEFORE ---" -ForegroundColor Cyan
    try {
        docker exec $Neo4jContainer cypher-shell -u $Neo4jUser -p $Neo4jPass `
            "MATCH (m:Memory) WHERE m.src_type <> 2 RETURN m.color AS color, count(*) AS n"
    } catch {
        Write-Host "Could not query Neo4j directly ($($_.Exception.Message)). Skipping colour check." -ForegroundColor Yellow
        $SkipColorCheck = $true
    }
    Write-Host ""
}

# ── Run the probe set ──
$results = @()

for ($r = 1; $r -le $Repeat; $r++) {
    foreach ($probe in $probeList) {
        $body = @{ prompt = $probe.Prompt; top_k = $TopK } | ConvertTo-Json

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $resp = Invoke-RestMethod -Uri "$BaseUrl/recall" -Method Post `
                -ContentType "application/json" -Body $body
            $sw.Stop()

            $results += [PSCustomObject]@{
                Pass          = $r
                Set           = $probe.Set
                Prompt        = $probe.Prompt
                ReadPath      = $resp.read_path
                ReadMs        = $resp.read_ms
                RoundTripMs   = [math]::Round($sw.Elapsed.TotalMilliseconds, 1)
                Count         = $resp.count
                Anticipated   = if ($resp.PSObject.Properties.Name -contains "anticipated") { $resp.anticipated.Count } else { $null }
            }
        } catch {
            $sw.Stop()
            Write-Host "FAILED on prompt '$($probe.Prompt)': $($_.Exception.Message)" -ForegroundColor Red
            $results += [PSCustomObject]@{
                Pass = $r; Set = $probe.Set; Prompt = $probe.Prompt; ReadPath = "ERROR"
                ReadMs = $null; RoundTripMs = $null; Count = $null; Anticipated = $null
            }
        }
    }
}

Write-Host "--- Results ---" -ForegroundColor Cyan
$results | Sort-Object Set, Prompt | Format-Table Pass, Set, Prompt, ReadPath, ReadMs, RoundTripMs, Count, Anticipated -AutoSize

function Show-Stats($rows, $label) {
    $vals = $rows | Where-Object { $_.ReadMs -ne $null } | Select-Object -ExpandProperty ReadMs
    if ($vals.Count -eq 0) {
        Write-Host "$label -- no successful calls to summarize." -ForegroundColor Red
        return
    }
    $min = ($vals | Measure-Object -Minimum).Minimum
    $max = ($vals | Measure-Object -Maximum).Maximum
    $avg = [math]::Round(($vals | Measure-Object -Average).Average, 1)
    Write-Host "$label -- read_ms: min=$min  avg=$avg  max=$max  (n=$($vals.Count))"
}

Write-Host "`n--- read_ms summary (server-internal timing) ---" -ForegroundColor Cyan
if (-not $NewOnly) { Show-Stats ($results | Where-Object { $_.Set -eq "Legacy" }) "Legacy (comparable to historical table above)" }
if (-not $LegacyOnly) { Show-Stats ($results | Where-Object { $_.Set -eq "New" }) "New (no prior baseline -- this run establishes one)" }
if ($Prompts) { Show-Stats ($results | Where-Object { $_.Set -eq "Custom" }) "Custom" }
Show-Stats $results "Combined"

$validRoundTrip = $results | Where-Object { $_.RoundTripMs -ne $null } | Select-Object -ExpandProperty RoundTripMs
if ($validRoundTrip.Count -gt 0) {
    $rtMin = ($validRoundTrip | Measure-Object -Minimum).Minimum
    $rtMax = ($validRoundTrip | Measure-Object -Maximum).Maximum
    $rtAvg = [math]::Round(($validRoundTrip | Measure-Object -Average).Average, 1)
    Write-Host "`nround-trip ms (includes network + PowerShell/HTTP overhead, NOT directly comparable to historical read_ms):"
    Write-Host "  min=$rtMin  avg=$rtAvg  max=$rtMax`n"
}

# ── Colour distribution after ──
if (-not $SkipColorCheck) {
    Write-Host "--- Conversational (non-Document) colour distribution AFTER ---" -ForegroundColor Cyan
    docker exec $Neo4jContainer cypher-shell -u $Neo4jUser -p $Neo4jPass `
        "MATCH (m:Memory) WHERE m.src_type <> 2 RETURN m.color AS color, count(*) AS n"
    Write-Host "`nCompare the two tables above by eye. If Blue grew and Green/Yellow shrank," -ForegroundColor Yellow
    Write-Host "this run archived conversational memories. Restore with scratchpad\restore_blue.py" -ForegroundColor Yellow
    Write-Host "(dry-runs by default) if that happened.`n" -ForegroundColor Yellow
}

# ── Save results for later comparison ──
try {
    if (-not (Test-Path $ResultsDir)) {
        New-Item -ItemType Directory -Path $ResultsDir | Out-Null
    }
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $outFile = Join-Path $ResultsDir "recall_speed_$timestamp.json"
    @{
        timestamp      = (Get-Date).ToString("o")
        base_url       = $BaseUrl
        top_k          = $TopK
        total_memories = $totalMemories
        results        = $results
    } | ConvertTo-Json -Depth 6 | Out-File -FilePath $outFile -Encoding utf8
    Write-Host "Results saved to $outFile" -ForegroundColor Green
} catch {
    Write-Host "Could not save results file: $($_.Exception.Message)" -ForegroundColor Yellow
}
