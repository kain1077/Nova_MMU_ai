# mmu_phase8_deploy.ps1
# Deploys Phase 8 (Episodic Memory + True Session Continuity).
# Run this from a PowerShell window on the development machine, in C:\MMU.
#
# What this does:
#   1. Rebuilds the mmu-server Docker image with --no-cache (the PC-migration
#      lesson: a plain "docker compose build" can silently keep stale code).
#   2. Force-recreates the container so it actually picks up the new image.
#   3. Greps the RUNNING container for the new Phase 8 routes as proof the
#      new code, not a cached layer, is what's live.
#   4. Hits /activity to confirm the new last_session_id / last_session_closed
#      fields are present in the response.
#
# mmu_idle_daemon.py runs on the host directly (not Docker), so it just
# needs to be stopped and restarted after this to pick up the Phase 8
# changes -- there's nothing to rebuild for it.
#
# mmu_mcp_server.py is spawned fresh by LM Studio per conversation, so it
# picks up the new stable per-conversation session_id the next time you
# start a brand new conversation with Nova. No restart of anything needed
# for that one, just close/reopen the chat.

Write-Host "=== MMU Phase 8 deploy ===" -ForegroundColor Cyan

Set-Location C:\MMU

Write-Host "`n[1/4] Rebuilding mmu-server (--no-cache)..." -ForegroundColor Yellow
docker compose build --no-cache mmu-server
if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed. Stopping here." -ForegroundColor Red
    exit 1
}

Write-Host "`n[2/4] Recreating the container..." -ForegroundColor Yellow
docker compose up -d --force-recreate mmu-server
if ($LASTEXITCODE -ne 0) {
    Write-Host "Recreate failed. Stopping here." -ForegroundColor Red
    exit 1
}

Write-Host "`nWaiting 5s for the server to come up..." -ForegroundColor Yellow
Start-Sleep -Seconds 5

Write-Host "`n[3/4] Verifying Phase 8 code is actually in the running container..." -ForegroundColor Yellow
$checks = @(
    "session_close",
    "session_resume",
    "HAPPENED_IN",
    "x_mmu_session"
)
$containerName = (docker compose ps -q mmu-server)
if (-not $containerName) {
    Write-Host "Could not find the mmu-server container. Check 'docker compose ps'." -ForegroundColor Red
    exit 1
}

$allGood = $true
foreach ($term in $checks) {
    $found = docker exec $containerName grep -l $term /app/mmu_server.py 2>$null
    if ($found) {
        Write-Host "  OK  : found '$term' in running mmu_server.py" -ForegroundColor Green
    } else {
        Write-Host "  MISS: '$term' NOT found in running mmu_server.py" -ForegroundColor Red
        $allGood = $false
    }
}

if (-not $allGood) {
    Write-Host "`nOne or more checks failed. The container may still be running old code." -ForegroundColor Red
    Write-Host "Try: docker compose logs mmu-server   and re-run this script." -ForegroundColor Red
    exit 1
}

Write-Host "`n[4/4] Live test call to /activity..." -ForegroundColor Yellow
try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8765/activity" -Method Get
    Write-Host ($resp | ConvertTo-Json -Depth 5)
    if ($resp.PSObject.Properties.Name -contains "last_session_id") {
        Write-Host "`nlast_session_id field present. Phase 8 server code is live." -ForegroundColor Green
    } else {
        Write-Host "`nlast_session_id field missing from /activity response. Something is off." -ForegroundColor Red
    }
} catch {
    Write-Host "Could not reach http://127.0.0.1:8765/activity : $_" -ForegroundColor Red
}

Write-Host "`n=== Server-side deploy done. ===" -ForegroundColor Cyan
Write-Host "Next steps (do these yourself):" -ForegroundColor Cyan
Write-Host "  1. Stop and restart mmu_idle_daemon.py so it picks up the new code."
Write-Host "  2. Close any open Nova chat in LM Studio and start a brand new one,"
Write-Host "     so mmu_mcp_server.py respawns with the new stable session_id."
Write-Host "  3. Talk to Nova for a bit, then let her sit idle past MMU_SESSION_CLOSE_SEC"
Write-Host "     (15 min by default) and watch the idle daemon log for a 'session ..."
Write-Host "     looks over' line."
