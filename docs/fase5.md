# Fase 5: método de medición y celda piloto

## Alcance

Implementar benchmark_runner con Python multiproceso y comprobar una celda
antes de automatizar la matriz completa.

Celda piloto:
- Motor: PostgreSQL.
- Perfil: PROFILE_A_BASELINE.
- Consulta: Q1.
- Concurrencia: 1 cliente.
- Repeticiones: 3.

La selección de esta celda es una decisión de implementación.
Se conservan el dataset, los índices y las consultas validados en la fase 4.

## Requisitos del manual

- Perfiles A/B/C: 4/2/1 CPU y 4096/2048/1024 m de memoria.
- Concurrencias: 1, 10 y 50 clientes.
- Tres repeticiones por escenario.
- Limpieza de caché Linux antes de cada repetición.
- Calentamiento de 60 segundos.
- Medición de 300 segundos.
- Reposo de 60 segundos.
- cAdvisor, Node Exporter y Prometheus con scrape_interval de 1 segundo.
- Una fila CSV por repetición.
- Evidencias mediante logs o capturas de spill/OOM.

El entorno WSL descarta mem_swappiness=0. Se documentará esta limitación y
la configuración de swap efectivamente verificada.

## Ejecución de las consultas

Cada cliente será un proceso con una conexión persistente al motor.
Los procesos coordinarán el inicio de las ventanas de carga.

El ejecutor leerá las consultas desde queries/, sin modificarlas.
Cada operación consumirá completamente sus resultados.

Q1 utilizará la configuración predeterminada del motor.
Las variantes de Q2 serán:
- MongoDB: allowDiskUse=false y allowDiskUse=true.
- PostgreSQL: work_mem=4MB y work_mem=64MB.

Solo estará activo el motor sometido a la medición.

## Ventanas y operaciones pendientes

Durante el calentamiento se admitirán consultas durante 60 segundos.
Sus operaciones pendientes terminarán antes de comenzar la medición.

Durante la medición se admitirán consultas durante 300 segundos.
Al cerrar esa ventana no se iniciarán consultas nuevas y se esperará a
que terminen las pendientes. Después comenzarán los 60 segundos de reposo.

El tratamiento de las operaciones pendientes es una decisión del método:
el manual no lo especifica. Su duración y sus conteos se conservarán.

## Cálculo de las métricas

- throughput_qps: consultas exitosas completadas dentro de la ventana de
  medición, divididas entre 300 segundos.

- latency_p50_ms, latency_p95_ms y latency_p99_ms: percentiles del tiempo
  de ejecución y recuperación completa del resultado de las consultas
  exitosas iniciadas durante la medición. Incluyen las que terminen después
  de cerrar la ventana, identificadas por separado.

- Percentiles: interpolación lineal en la posición (N-1)*p sobre las
  latencias ordenadas. Esta elección se documentará como parte del método.

- cpu_throttled_seconds: incremento del contador acumulado de throttling
  del contenedor durante la ventana de medición, expresado en segundos.
  No utilizar su valor acumulado desde el arranque como resultado de una
  repetición.

- max_rss_mb: máximo RSS observado en las muestras del contenedor durante
  la medición. Se utilizarán MiB, dividiendo bytes entre 1048576, y se
  declarará esta convención.

- disk_spill: evidencia de uso de disco temporal durante la ejecución.
  Permitir spill no demuestra que haya ocurrido. La ausencia de evidencia
  suficiente tampoco se convertirá automáticamente en false.

- OOM: se registrará si hubo una terminación por falta de memoria.
  Un error de consulta y una terminación del kernel por OOM se distinguirán.

Se conservarán también el uso acumulado de CPU, los conteos de operaciones
y las muestras de memoria necesarios para el análisis posterior de M3.

## CSV obligatorio

timestamp_epoch,engine,profile,query_id,concurrency,run_number,throughput_qps,latency_p50_ms,latency_p95_ms,latency_p99_ms,cpu_throttled_seconds,max_rss_mb,disk_spill,status

timestamp_epoch identificará el inicio de la ventana de medición.
Los valores desconocidos permanecerán vacíos y tendrán una explicación.
No se utilizará cero para sustituir datos desconocidos.

Los estados serán:
- ok: ejecución completada, sin spill identificado y con métricas disponibles.
- spill: ejecución completada con spill acreditado.
- oom: terminación por falta de memoria acreditada.
- error: fallo de ejecución o imposibilidad de obtener un dato necesario.

## Evidencias y limitaciones

Se conservarán los resultados originales, las latencias, las muestras
de recursos, las configuraciones efectivas y las evidencias de spill/OOM.

Las pérdidas de muestras y los huecos se registrarán para interpretar
los resultados. No se interpolarán muestras ni se afirmará conocer picos
ocurridos durante los huecos.

El manual no establece un porcentaje mínimo de cobertura ni un límite
máximo de cinco segundos entre muestras. No se añadirán como condiciones
automáticas para repetir una corrida.

Los planes de la fase 4 respaldarán el análisis. Sus tiempos de EXPLAIN
no se utilizarán como latencias del benchmark.

## Cierre del piloto

Comprobar:
- Tres filas, correspondientes a las tres repeticiones.
- Encabezado CSV exacto.
- Unidades y cálculos coherentes con las evidencias.
- Estados e incidencias explicados.

Spill, OOM y errores se conservarán como resultados del experimento.
No se repetirá automáticamente una corrida para obtener un estado favorable.

La automatización completa mediante run_benchmarks.sh corresponde al
siguiente paso, después de revisar el piloto.