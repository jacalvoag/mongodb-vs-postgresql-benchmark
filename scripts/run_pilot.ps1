# Ejecutar solo despues de preparar y comprobar el servicio benchmark-runner.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function Invoke-RunnerCompose {
    & docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env --env-file config/profiles/PROFILE_A_BASELINE.env @args
    if ($LASTEXITCODE -ne 0) { throw "Fallo de preparacion o del ejecutor. Conserva la salida; no reintentes el piloto completo." }
}

Invoke-RunnerCompose --profile tools run --rm --no-deps benchmark-runner check --engine postgresql --query Q1
$batch = "pilot_" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
$csvPath = "results/$batch.csv"
New-Item -ItemType Directory -Force "evidence/cache_flush" | Out-Null
for ($number = 1; $number -le 3; $number++) {
    $runId = "${batch}_r${number}"
    Write-Host "Repeticion $number/3: limpieza de cache Linux"
    $cacheOutput = & wsl -d docker-desktop -u root -- sh -c 'sync && echo 3 > /proc/sys/vm/drop_caches && echo CACHE_LINUX_OK'
    if ($LASTEXITCODE -ne 0 -or $cacheOutput -notcontains "CACHE_LINUX_OK") { throw "No se pudo acreditar la limpieza de cache." }
    $cachePath = "evidence/cache_flush/$runId.json"
    [ordered]@{
        run_id = $runId
        engine = "postgresql"
        profile = "PROFILE_A_BASELINE"
        status = "ok"
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
        command = "sync; echo 3 > /proc/sys/vm/drop_caches"
        scope = "Kernel Linux de Docker Desktop/WSL2"
        output = ($cacheOutput -join "`n")
    } | ConvertTo-Json | Set-Content -LiteralPath $cachePath -Encoding UTF8
    Invoke-RunnerCompose --profile tools run --rm --no-deps benchmark-runner run --engine postgresql --query Q1 --concurrency 1 --run-number $number --run-id $runId --cache-record $cachePath --csv $csvPath
}
Write-Host "Tres repeticiones registradas. Revisa todos los estados en $csvPath"
Import-Csv $csvPath | Format-Table -AutoSize
