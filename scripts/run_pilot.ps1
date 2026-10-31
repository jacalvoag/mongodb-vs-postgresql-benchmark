param([switch]$CheckOnly)

# Primera celda de paqueteria: PostgreSQL / A / Q1 / C=1 / tres repeticiones.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$env:PROFILE_ID = "PROFILE_A_BASELINE"
$env:DB_CPUS = "4"
$env:DB_MEMORY = "4096m"
$statePath = "results/paqueteria/pilot.json"

function Invoke-RunnerCompose {
    & docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env --env-file config/profiles/PROFILE_A_BASELINE.env @args
    if ($LASTEXITCODE -ne 0) {
        throw "Fallo de preparacion o del ejecutor. Conserva la salida; no reintentes el piloto completo."
    }
}

function Save-PilotState {
    $pilot | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $statePath -Encoding UTF8
}

if (-not $CheckOnly -and (Test-Path -LiteralPath $statePath)) {
    throw "Ya existe $statePath. Se conserva el piloto registrado; no se inicia otro automaticamente."
}

# Este check solo lee archivos, configuracion, cgroup y disponibilidad.
# No ejecuta Q1, Q2, COUNT ni EXPLAIN.
$checkLines = @(Invoke-RunnerCompose --profile tools run --rm --no-deps --pull never benchmark-runner check --engine postgresql --query Q1)
$checkText = $checkLines -join "`n"
Write-Output $checkText
$checked = $checkText | ConvertFrom-Json
if ($checked.status -ne "ok" -or $checked.configuration.case -ne "paqueteria") {
    throw "La comprobacion no acredita el ejecutor de paqueteria."
}
if ($CheckOnly) { return }

$batch = "paqueteria_pilot_" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
$csvPath = "results/paqueteria/$batch.csv"
$sourceHash = $checked.configuration.source_sha256
New-Item -ItemType Directory -Force "results/paqueteria" | Out-Null
New-Item -ItemType Directory -Force "evidence/paqueteria/cache_flush" | Out-Null
# Reserva el registro sin reemplazar uno creado por otra invocacion.
New-Item -ItemType File -Path $statePath -ErrorAction Stop | Out-Null
$pilot = [ordered]@{
    case = "paqueteria"
    model_version = "paqueteria-v1"
    batch_id = $batch
    csv = $csvPath
    engine = "postgresql"
    profile = "PROFILE_A_BASELINE"
    query = "Q1"
    variant = "default"
    concurrency = 1
    source_sha256 = $sourceHash
    query_sha256 = $checked.configuration.query_sha256
    reference_sha256 = $checked.configuration.reference_sha256
    method_sha256 = $checked.configuration.method_sha256
    expected_rows = $checked.configuration.expected_rows
    planned_repetitions = 3
    protocol = [ordered]@{ warmup_seconds = 60; measurement_seconds = 300; cooldown_seconds = 60 }
    status = "running"
    created_at_utc = [DateTime]::UtcNow.ToString("o")
    completed_at_utc = $null
    runs = @()
    failure = $null
}
Save-PilotState

try {
    for ($number = 1; $number -le 3; $number++) {
        $runId = "${batch}_r${number}"
        Write-Host "Repeticion $number/3: limpieza de cache Linux"
        $cacheOutput = @(& wsl -d docker-desktop -u root -- sh -c 'sync && echo 3 > /proc/sys/vm/drop_caches && echo CACHE_LINUX_OK')
        if ($LASTEXITCODE -ne 0 -or $cacheOutput -notcontains "CACHE_LINUX_OK") {
            throw "No se pudo acreditar la limpieza de cache Linux."
        }
        $cachePath = "evidence/paqueteria/cache_flush/$runId.json"
        [ordered]@{
            case = "paqueteria"
            source_sha256 = $sourceHash
            run_id = $runId
            engine = "postgresql"
            profile = "PROFILE_A_BASELINE"
            status = "ok"
            completed_at_utc = [DateTime]::UtcNow.ToString("o")
            command = "sync; echo 3 > /proc/sys/vm/drop_caches"
            scope = "Kernel Linux de Docker Desktop/WSL2"
            output = ($cacheOutput -join "`n")
        } | ConvertTo-Json | Set-Content -LiteralPath $cachePath -Encoding UTF8

        Invoke-RunnerCompose --profile tools run --rm --no-deps --pull never benchmark-runner run --engine postgresql --query Q1 --concurrency 1 --run-number $number --run-id $runId --cache-record $cachePath --csv $csvPath

        $rows = @(Import-Csv -LiteralPath $csvPath)
        if ($rows.Count -ne $number) { throw "El CSV no contiene las repeticiones esperadas." }
        $lastRow = $rows[-1]
        if ($lastRow.engine -ne "postgresql" -or $lastRow.profile -ne "PROFILE_A_BASELINE" -or
            $lastRow.query_id -ne "Q1" -or [int]$lastRow.concurrency -ne 1 -or
            [int]$lastRow.run_number -ne $number) {
            throw "La fila registrada no corresponde a la celda piloto."
        }
        $metadataPath = "evidence/paqueteria/runs/$runId/metadata.json"
        $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
        if ($metadata.case -ne "paqueteria" -or
            $metadata.configuration.source_sha256 -ne $sourceHash -or
            $metadata.configuration.method_sha256 -ne $pilot.method_sha256 -or
            $metadata.configuration.query_sha256 -ne $pilot.query_sha256 -or
            $metadata.configuration.reference_sha256 -ne $pilot.reference_sha256) {
            throw "Las identidades del metodo o de los datos cambiaron durante el piloto."
        }
        $pilot.runs += [ordered]@{
            run_id = $runId
            run_number = $number
            status = $lastRow.status
            metadata = $metadataPath
        }
        Save-PilotState
    }
    $pilot.status = "completed"
    $pilot.completed_at_utc = [DateTime]::UtcNow.ToString("o")
    Save-PilotState
} catch {
    $pilot.status = "interrupted"
    $pilot.failure = $_.Exception.Message
    Save-PilotState
    throw
}

Write-Host "Tres repeticiones registradas en $csvPath. Revisa sus estados e incidencias."
Write-Host "Registro del piloto: $statePath"
Import-Csv -LiteralPath $csvPath | Format-Table -AutoSize
