# Implementacion del metodo de fase 5

Base revisada: commit 0fe0f91 (metodo), sobre ba6346a (fase 4).
Este documento acompaña docs/fase5.md; no cambia sus requisitos ni calculos.
No se reutilizan salidas ni se reclasifican pilotos anteriores.

## Archivos

- benchmark_runner/metrics.py: percentiles, throughput, CSV y muestreo observado.
- benchmark_runner/load.py: conexiones persistentes, procesos y ventanas.
- benchmark_runner/observe.py: lecturas Docker/cgroup, Prometheus y evidencia spill.
- benchmark_runner/__main__.py: comprobacion y una repeticion 60/300/60.
- compose.runner.yml: servicio de ejecucion e instrumentacion de logs.
- scripts/run_pilot.ps1: tres repeticiones de la celda piloto, sin reintentos.
- tests/test_runner.py: pruebas con clientes simulados y archivos temporales.

## Dependencias y almacenamiento

No se cambian requirements.txt, docker-compose.yml, monitoring/prometheus.yml,
queries/, los datos cargados ni los indices. El servicio usa el Dockerfile.loader
existente para aprovechar sus capas y las mismas versiones de controladores.
El paquete se monta desde el repositorio; no necesita otro Dockerfile.
El build puede usar la cache disponible; no se promete cache si se elimino.

No se agrega tmpfs, otra base ni otro sistema de monitorizacion. Prometheus
conserva su volumen y retencion existentes. Su configuracion original conserva
las marcas de tiempo de cAdvisor: puede haber menos muestras distintas que
recolecciones exitosas. Ambas se exportan, no se fuerza una continuidad falsa.

El adaptador Docker solo invoca GET. Las credenciales proceden del entorno
y se usan al conectar; no se exporta el campo Env de Docker en los metadatos.
El acceso al socket y los montajes de /proc y cgroup son instrumentacion local.

Los logs Docker del motor se limitan a tres archivos de 20m. Las evidencias
se guardan comprimidas, sin duplicar todos sus registros en metadata.json.
No se borran volumenes, imagenes, cache de builds ni evidencias automaticamente.
El paquete de codigo no incluye dataset, imagenes Docker ni resultados.

## Instrumentacion necesaria para spill

PostgreSQL conserva log_temp_files=0 y agrega log_min_duration_statement=0
con application_name en el prefijo. MongoDB agrega slowms=0 y muestreo 1;
el profiler permanece apagado. Esta es una decision tecnica para registrar
las operaciones, no un requisito literal del docente ni una optimizacion.
Su costo debe declararse y mantenerse constante entre las celdas de cada motor.

Cada repeticion etiqueta sus consultas. Solo los mensajes de la medicion se
usan para decidir spill. Un evento positivo acredita true. Para decidir false
se requieren logs de finalizacion de todas las operaciones exitosas. Si no hay
evidencia suficiente, queda desconocido y se explica status=error.
Una consulta con error no se oculta aunque tambien haya evidencia de spill.
Un error de limite interno de memoria no equivale automaticamente a OOM kill.

## Metricas y ventanas

Se respeta docs/fase5.md: 60s warmup, 300s medicion, drenaje y 60s reposo.
Los procesos se preparan antes de cada ventana. El limite de arranque de 120s
es una proteccion de preparacion; no limita la duracion de las consultas.
El ejecutor no fija un timeout maximo para las consultas en curso.

CPU proviene de cpu.stat de cgroup v2, leido en los limites de medicion.
usage_usec y throttled_usec se convierten a segundos. Sus tiempos reales de
lectura se conservan, junto con muestras periodicas y memory.events.
OOM se detecta por State.OOMKilled o incremento de memory.events.oom_kill.

RSS es el maximo observado de los puntos crudos de Prometheus dentro de la
ventana: se consulta un range-vector mediante la API instantanea, no se
remuestrea con query_range. Los huecos se registran sin exigir 90%, 270
muestras o un maximo de 5s. Una serie ausente no se transforma en cero.

Se conservan latencias de consultas admitidas en los 300s, incluso las que
terminan despues; QPS cuenta las exitosas terminadas dentro de la ventana.
Estos conjuntos se distinguen en metadata.json. No promediar percentiles
individuales de clientes: se calculan sobre todas las latencias exitosas.

Para Q2 se usa query_id=Q2_allowDiskUse_false, Q2_allowDiskUse_true,
Q2_work_mem_4MB o Q2_work_mem_64MB para identificar el ajuste manteniendo
las 14 columnas. Es una convencion de identificacion, no nuevas consultas.

## Artefactos por repeticion

En evidence/runs/<run_id>/:

- metadata.json: metodo, perfil, hashes, configuracion efectiva, conteos,
  CPU, errores, estado, cobertura RSS y fila CSV.
- cache_flush.json: resultado de limpieza Linux antes de esa repeticion.
- latencies.json.gz: operaciones de medicion, incluidos sus errores.
- cgroup_samples.json.gz: contadores del contenedor y observaciones OOM.
- prometheus_samples.json.gz: muestras reales de contenedor, host y scrapes.
- engine.log.gz: registros originales del motor en el intervalo de interes.

Los gzip se leen con gzip.open de Python. No son los formatos de los ZIP
anteriores de fase 5: esos quedaron descartados. Una perdida de telemetria
no se solventa imputando muestras. Las limitaciones deben constar en el reporte.

Si se produce un error durante una repeticion y se consigue registrar su fila,
el proceso termina normalmente para conservar el resto de las repeticiones:
el resultado esta en status, no en un reintento hasta conseguir ok. Un fallo
que impide preparar el motor o guardar la salida detiene la automatizacion.
Nunca declarar una repeticion inexistente como completada.

## Primer paso: comprobar sin consultas del experimento

Desde PowerShell en la raiz, validar primero los calculos con Python ya local:

    docker run --rm --pull=never --network none --mount "type=bind,source=$($PWD.Path),target=/work,readonly" --workdir /work python:3.14.10-slim python -B -m unittest discover -s tests -p test_runner.py -v

No inicia servicios, no instala paquetes, no construye imagenes y no ejecuta
Q1/Q2. Usa procesos y datos simulados con archivos temporales del contenedor.
Si la imagen no existe, --pull=never detiene el comando en lugar de descargarla.

Luego validar la union de Compose:

    docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env --env-file config/profiles/PROFILE_A_BASELINE.env --profile tools config --quiet

Preparar los servicios una vez, sin recargar datos:

    docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env --env-file config/profiles/PROFILE_A_BASELINE.env --profile tools build benchmark-runner
    docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env --env-file config/profiles/PROFILE_A_BASELINE.env up -d cadvisor node-exporter prometheus
    docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env --env-file config/profiles/PROFILE_A_BASELINE.env --profile mongo stop mongodb
    docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env --env-file config/profiles/PROFILE_A_BASELINE.env --profile postgres up -d postgresql

La ultima orden puede recrear PostgreSQL para aplicar los parametros de logs;
su volumen de datos se conserva. Comprobar despues:

    docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env --env-file config/profiles/PROFILE_A_BASELINE.env --profile tools run --rm --no-deps benchmark-runner check --engine postgresql --query Q1

check lee reportes y hashes de fase 4, identifica contenedor y limites,
consulta disponibilidad de Prometheus y realiza conexion/SHOW de configuracion.
No ejecuta Q1/Q2, COUNT sobre orders ni EXPLAIN. No crea un CSV de mediciones.
Una comprobacion correcta no demuestra por si sola la integracion bajo carga.

## Piloto: paso posterior a revisar check

El script no vuelve a construir ni recrear servicios:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_pilot.ps1

Hace exactamente tres repeticiones nuevas, con limpieza de cache Linux antes
de cada una y un CSV pilot_<UTC>.csv independiente. Tiempo nominal: 21 minutos
mas preparacion y drenaje. No correr de nuevo por ver spill, oom o error:
revisar la evidencia. El almacenamiento temporal usado por las consultas
puede producir muchas escrituras aunque los logs sean pequeños.

El piloto no sustituye run_benchmarks.sh ni el reporte final. Esos entregables
siguen pendientes de la automatizacion y analisis posteriores.

Referencias tecnicas consultadas:
https://www.mongodb.com/docs/manual/reference/command/aggregate/
https://www.postgresql.org/docs/18/runtime-config-logging.html
https://prometheus.io/docs/prometheus/latest/querying/api/
https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html
