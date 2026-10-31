# Ejecutor y primera celda de medición de paquetería

Este paso adapta el ejecutor ya probado al caso de paquetería. Conserva el
método de `docs/fase5.md`: perfiles, concurrencias, ventanas, cálculos y las
14 columnas. La selección de PostgreSQL / perfil A / Q1 / un cliente como
primera celda es una decisión de implementación.

Las tres repeticiones de esta celda son mediciones de paquetería. Se guardan
sus identidades para incorporarlas a la matriz posterior sin repetir la misma
celda bajo los mismos datos, consulta y método. El piloto anterior de orders
permanece como prueba técnica y no aporta filas a este experimento.

## Archivos del paquete

| Archivo | Acción |
| --- | --- |
| `benchmark_runner/__main__.py` | Reemplazar; acredita el caso y las consultas y separa las salidas |
| `benchmark_runner/load.py` | Reemplazar; consulta la colección envios |
| `benchmark_runner/observe.py` | Reemplazar; identifica logs de envios y getMore |
| `compose.runner.yml` | Reemplazar; conserva la instrumentación y actualiza el comentario del caso |
| `scripts/run_pilot.ps1` | Reemplazar; check sin carga y primera celda de tres repeticiones |
| `tests/test_runner.py` | Reemplazar; conserva las pruebas generales y adapta sus rutas y datos simulados |
| `tests/test_parcel_runner.py` | Agregar; comprueba procedencia y separación de casos |
| `docs/ejecucion_paqueteria.md` | Agregar este procedimiento |

`metrics.py` y `__init__.py` existentes se conservan. No se agregan dependencias,
Dockerfiles, bases de datos ni configuraciones de Prometheus. Tampoco cambian
el dataset, los índices o las consultas que acabas de validar.

Si todavía no registraste el paso de consultas, usa el commit indicado en
`docs/consultas_paqueteria.md`. Sus reportes deben existir localmente para que
el ejecutor acredite la equivalencia antes de medir.

## Comprobaciones previas

El modo `check` lee el manifiesto, la carga, la referencia y la validación
de paquetería. Comprueba caso, versión del modelo, conteos, origen, variante
validada y hashes de consulta/referencia. La configuración registra también
el hash del método: los cuatro módulos del ejecutor y `compose.runner.yml`.
Los hashes canónicos normalizan exclusivamente los saltos CRLF/LF; también
se conserva el hash de los bytes originales de la consulta.

Se comprueban el motor activo, sus límites efectivos de cgroup, swap, etiqueta
de perfil, versión del servidor y disponibilidad de la monitorización. Los
targets deben estar activos con scrape de un segundo. No se exigen porcentajes
de cobertura ni huecos máximos artificiales.

`check` no ejecuta Q1, Q2, COUNT ni EXPLAIN. No crea CSV, no limpia caché y no
inicia las ventanas de carga. El parámetro `-CheckOnly` del script también
comprueba su ejecución en PowerShell y termina después de este check.

## Instrumentación y almacenamiento

Se mantiene `log_temp_files=0` y el registro de finalización de consultas en
PostgreSQL, con application_name en el prefijo. MongoDB mantiene el profiler
apagado y usa slowms=0 con muestreo completo de mensajes de consultas. Es una
decisión de instrumentación para acreditar spill; su costo forma parte del
método y debe declararse en el informe.

Cada repetición y fase usa una etiqueta. Sólo se interpretan mensajes de la
medición. En MongoDB se usan los mensajes de `envios`, incluidos sus getMore
con comentario heredado. Los logs de orders no son evidencia de paquetería.
En PostgreSQL se reconoce el comienzo de la nueva Q1, que usa un WITH.

Un evento positivo acredita spill. Para afirmar que no hubo spill se requiere
captura de las finalizaciones esperadas. Si la evidencia es insuficiente, el
dato queda desconocido y se registra la incidencia; no se inventa false.

Los logs Docker siguen limitados a tres archivos de 20m por motor. Las
evidencias se guardan comprimidas. La rotación puede limitar los registros
disponibles si una celda genera muchos mensajes; el ejecutor comprueba la
suficiencia de la captura y no oculta esa limitación.

El `.dockerignore` existente excluye data, results y evidence del contexto de
construcción. No se pide descargar imágenes nuevas, construir sin caché,
recargar datos, borrar volúmenes ni ejecutar comandos de limpieza de Docker.
El espacio temporal que las propias consultas utilicen es parte del
experimento y se registra si ocurre.

## 1. Pruebas pequeñas sin servicios ni consultas reales

Colocar los ocho archivos del paquete en sus rutas. Desde PowerShell, en la
raíz del repositorio, ejecutar:

```powershell
docker run --rm --pull=never --network none --mount "type=bind,source=$($PWD.Path),target=/work,readonly" --workdir /work python:3.14.10-slim python -B -m unittest discover -s tests -p "test_*runner*.py" -v
```

Se esperan 40 pruebas terminadas en `OK`: 24 del ejecutor general y 16 de la
adaptación. Usan procesos, datos y clientes simulados y archivos temporales
del contenedor. No consultan los motores ni escriben en el repositorio montado
en modo de sólo lectura.

## 2. Preparar el perfil y la instrumentación

En la misma sesión de PowerShell:

```powershell
$env:PROFILE_ID = "PROFILE_A_BASELINE"
$env:DB_CPUS = "4"
$env:DB_MEMORY = "4096m"
$parcelCompose = @("-f", "docker-compose.yml", "-f", "compose.runner.yml", "--env-file", ".env", "--env-file", "config/profiles/PROFILE_A_BASELINE.env")

docker compose @parcelCompose --profile tools config --quiet
docker compose @parcelCompose --profile tools build benchmark-runner
docker compose @parcelCompose up -d --no-deps --pull never cadvisor node-exporter prometheus
docker compose @parcelCompose --profile mongo stop mongodb
docker compose @parcelCompose --profile postgres up -d --no-deps --pull never postgresql
docker compose @parcelCompose --profile postgres exec postgresql pg_isready
```

Se combinan ambos archivos de Compose para aplicar los parámetros de logs.
PostgreSQL puede recrearse para incorporar su comando de arranque; su volumen
se conserva. `pg_isready` debe indicar `accepting connections`.

WSL ya descartaba mem_swappiness=0. Esa limitación se conserva y debe declararse.
El ejecutor verifica la prohibición efectiva de swap en cgroup.

## 3. Comprobar sin iniciar el piloto

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_pilot.ps1 -CheckOnly
```

Se espera `status: ok`, caso `paqueteria`, Q1, perfil A, 12 filas esperadas,
4 CPU y 4294967296 bytes de memoria efectiva. El hash de origen debe ser:

```text
bd0e8cc1c0875bfef50907a8180710cb6e01c08154d6115af2592e01495f6920
```

Si hay un error de preparación, conservar la salida y resolverlo antes de
medir. Una comprobación fallida no es una repetición del experimento ni
justifica iniciar el piloto para probar si funciona bajo carga.

## 4. Ejecutar la primera celda una sola vez

Cuando la comprobación termine correctamente:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_pilot.ps1
```

El script vuelve a comprobar la preparación, reserva
`results/paqueteria/pilot.json` y ejecuta exactamente tres repeticiones de
PostgreSQL / A / Q1 / concurrencia 1. No construye imágenes, recrea servicios,
recarga datos ni modifica consultas durante el piloto.

Antes de cada repetición se ejecuta `sync` y `drop_caches=3` en el kernel Linux
de Docker Desktop/WSL2 y se conserva su salida. No se afirma limpiar la caché
de Windows ni la del dispositivo físico.

Cada repetición admite consultas durante 60 segundos de calentamiento, espera
las pendientes, admite consultas durante 300 segundos de medición, espera las
pendientes y aplica 60 segundos de reposo. Tiempo nominal de las tres:
21 minutos, más preparación y drenaje. No hay un límite artificial para la
duración de las consultas en curso.

Conexiones persistentes y consumo completo de resultados forman parte de la
latencia. Durante la medición se comprueba el número de filas; la equivalencia
completa ya fue acreditada con la referencia antes de esta fase.

El throughput cuenta consultas exitosas terminadas dentro de los 300s,
divididas entre 300. Los percentiles se calculan sobre las latencias globales
exitosas admitidas en la ventana, incluidas las terminadas después. El drenaje
se informa por separado. CPU y RSS corresponden a la ventana de medición.
`max_rss_mb` se expresa en MiB y es el máximo observado, sin interpolación de
huecos de muestreo.

Si el registro `pilot.json` ya existe, el script no inicia otro piloto. Si se
interrumpe una ejecución, conserva su estado y las filas existentes para
revisarlas. No se debe borrar el registro para repetir hasta obtener ok.

`completed` en el registro significa tres repeticiones registradas, no tres
estados favorables. Cada fila conserva `ok`, `spill`, `oom` o `error`. Un fallo
que impida preparar o guardar una repetición interrumpe el script; no se
declara completada una repetición inexistente.

## Salidas y evidencia

- `results/paqueteria/pilot.json`: identidad y progreso de esta primera celda.
- `results/paqueteria/paqueteria_pilot_<UTC>.csv`: tres filas con el encabezado
  obligatorio, sin agregar columnas.
- `evidence/paqueteria/cache_flush/<run_id>.json`: limpieza previa de cada
  repetición.
- `evidence/paqueteria/runs/<run_id>/metadata.json`: configuración, hashes,
  protocolo, resultados, errores, estado del motor y muestreo observado.
- En esa misma carpeta: `cache_flush.json`, `latencies.json.gz`,
  `cgroup_samples.json.gz`, `prometheus_samples.json.gz` y `engine.log.gz`.

Encabezado CSV conservado:

```text
timestamp_epoch,engine,profile,query_id,concurrency,run_number,throughput_qps,latency_p50_ms,latency_p95_ms,latency_p99_ms,cpu_throttled_seconds,max_rss_mb,disk_spill,status
```

No se solicitan capturas adicionales: se conservan logs y contadores para
acreditar spill/OOM. Un error de memoria de la consulta no se transforma en
OOM kill; se comprueba State.OOMKilled o el contador oom_kill de cgroup.
Los valores desconocidos permanecen vacíos y tienen explicación en metadata.
Ningún porcentaje de cobertura o hueco de muestras provoca reintentos.

## Revisar y registrar

Después de terminar el piloto:

```powershell
$parcelPilot = Get-Content .\results\paqueteria\pilot.json -Raw | ConvertFrom-Json
Get-Content .\results\paqueteria\pilot.json
Import-Csv -LiteralPath $parcelPilot.csv | Format-List
```

Revisar tres filas, repeticiones 1/2/3, las 14 columnas, estados e incidencias.
Para compartir la evidencia basta con el CSV y los tres `metadata.json`; los
logs y muestras comprimidas se conservan y se consultarán si hace falta
explicar algún valor. No se publica el dataset.

Para registrar el código después de las pruebas y la comprobación sin carga:

```powershell
git add benchmark_runner/__main__.py benchmark_runner/load.py benchmark_runner/observe.py compose.runner.yml scripts/run_pilot.ps1 tests/test_runner.py tests/test_parcel_runner.py docs/ejecucion_paqueteria.md
git diff --cached --check
git diff --cached --stat
git commit -m "feat: adapta ejecutor y primera celda a paqueteria"
git push
```

Para registrar las tres repeticiones después de revisar sus resultados:

```powershell
$parcelPilot = Get-Content .\results\paqueteria\pilot.json -Raw | ConvertFrom-Json
git add results/paqueteria/pilot.json $parcelPilot.csv
foreach ($parcelRun in $parcelPilot.runs) {
    git add "evidence/paqueteria/runs/$($parcelRun.run_id)" "evidence/paqueteria/cache_flush/$($parcelRun.run_id).json"
}
git diff --cached --check
git diff --cached --stat
git commit -m "test: registra primera celda de mediciones de paqueteria"
git push
```

La automatización completa de `run_benchmarks.sh` será el siguiente paso,
utilizando este registro para identificar las repeticiones ya realizadas.

## Referencias técnicas

- [Aggregate de MongoDB: comentarios y usedDisk](https://www.mongodb.com/docs/manual/reference/command/aggregate/).
- [Registro de eventos de PostgreSQL](https://www.postgresql.org/docs/18/runtime-config-logging.html).
- [API de Prometheus](https://prometheus.io/docs/prometheus/latest/querying/api/).
- [cgroup v2 del kernel Linux](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).
