# MMU Phase 6.6 validation runbook
# Run from an elevated-not-required PowerShell window: .\mmu_phase66_validation.ps1
# Safe to re-run; nothing here deletes or destructively modifies data.

$ErrorActionPreference = "Stop"
Set-Location C:\mmu

Write-Host "=== 1. Rebuild + restart mmu-server (picks up any Phase 6.6 code not yet baked into the image) ===" -ForegroundColor Cyan
docker compose -f C:\mmu\docker-compose.yml build mmu-server
docker compose -f C:\mmu\docker-compose.yml up -d

Write-Host "=== 2. Health check ===" -ForegroundColor Cyan
Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" | ConvertTo-Json -Depth 5

Write-Host "=== 3. Baseline insights (before rating) -> C:\MMU\insights_before.json ===" -ForegroundColor Cyan
Invoke-RestMethod -Uri "http://127.0.0.1:8765/insights" | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 C:\MMU\insights_before.json

Write-Host "=== 4. Running idle daemon three times (light, medium, light) ===" -ForegroundColor Cyan
Write-Host "    Each run appends to C:\mmu\mmu_idle_daemon.log. This step calls out to LM Studio, so it may take 30-100s per pass." -ForegroundColor DarkGray
py -3.14 mmu_idle_daemon.py --once light
py -3.14 mmu_idle_daemon.py --once medium
py -3.14 mmu_idle_daemon.py --once light

Write-Host "=== 5. Insights again (after rating) -> C:\MMU\insights_after.json ===" -ForegroundColor Cyan
Invoke-RestMethod -Uri "http://127.0.0.1:8765/insights" | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 C:\MMU\insights_after.json

Write-Host "=== 6. Exporting full memories + creative outputs -> C:\MMU\memories_export.json / creative_outputs_export.json ===" -ForegroundColor Cyan
Invoke-RestMethod -Uri "http://127.0.0.1:8765/memories" | ConvertTo-Json -Depth 10 | Out-File -Encoding utf8 C:\MMU\memories_export.json
Invoke-RestMethod -Uri "http://127.0.0.1:8765/creative_outputs" | ConvertTo-Json -Depth 10 | Out-File -Encoding utf8 C:\MMU\creative_outputs_export.json

Write-Host "=== Done. Files written to C:\MMU: insights_before.json, insights_after.json, memories_export.json, creative_outputs_export.json ===" -ForegroundColor Green
Write-Host "Tell Claude in the chat once this finishes -- it will read those files directly from the connected C:\MMU folder." -ForegroundColor Green
