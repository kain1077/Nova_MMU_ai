# MMU /rate 404 diagnostic + forced clean rebuild
# Run from PowerShell: .\mmu_rate_diagnostic.ps1
# This forces Docker to rebuild with NO layer cache, so stale code cannot survive,
# then checks the actual file INSIDE the running container to prove the fix took.

Set-Location C:\mmu

Write-Host "=== Docker engine check ===" -ForegroundColor Cyan
docker info | Select-String "Server Version"

Write-Host "`n=== Forcing a clean, no-cache rebuild of mmu-server ===" -ForegroundColor Cyan
docker compose -f C:\mmu\docker-compose.yml build --no-cache mmu-server
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n!!! BUILD FAILED (exit code $LASTEXITCODE) -- scroll up for the real error, fix that first !!!" -ForegroundColor Red
    exit 1
}
Write-Host "Build succeeded." -ForegroundColor Green

Write-Host "`n=== Recreating the container from the fresh image ===" -ForegroundColor Cyan
docker compose -f C:\mmu\docker-compose.yml up -d --force-recreate mmu-server

Start-Sleep -Seconds 5

Write-Host "`n=== Proving the running container actually has Phase 6.6 code ===" -ForegroundColor Cyan
Write-Host "-- /rate route:" -ForegroundColor DarkGray
docker exec mmu-memory-server grep -n '"/rate"' /app/mmu_server.py
Write-Host "-- emotion_label field:" -ForegroundColor DarkGray
docker exec mmu-memory-server grep -n "emotion_label" /app/mmu_server.py | Select-Object -First 3
Write-Host "-- image build time:" -ForegroundColor DarkGray
docker inspect mmu-memory-server --format "{{.Created}}"

Write-Host "`n=== Live health + a real /rate call to confirm the endpoint responds ===" -ForegroundColor Cyan
Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" | ConvertTo-Json -Depth 3

$testAddr = (Invoke-RestMethod -Uri "http://127.0.0.1:8765/unrated_memories").memories[0].address
Write-Host "Testing /rate against: $testAddr" -ForegroundColor DarkGray
Invoke-RestMethod -Uri "http://127.0.0.1:8765/rate" -Method Post -ContentType "application/json" `
  -Body (@{ address = $testAddr; val_type = "clear"; intensity = 0 } | ConvertTo-Json) | ConvertTo-Json -Depth 5

Write-Host "`nIf the /rate call above returned JSON (not an error), the endpoint is fixed." -ForegroundColor Green
Write-Host "Now safe to re-run: py -3.14 mmu_idle_daemon.py --once light" -ForegroundColor Green
