# Consultas y validación de paquetería

Este paso implementa Q1/Q2 del caso definido en `docs/caso_uso_paqueteria.md`,
calcula sus resultados esperados desde el JSONL y los comprueba en los dos
motores. Se usa el perfil base A: 4 CPU y 4096 MiB.

## Archivos que se aplican manualmente

| Archivo | Acción |
| --- | --- |
| `queries/mongodb/Q1.json` | Reemplazar la consulta de orders |
| `queries/mongodb/Q2.json` | Reemplazar la consulta de orders |
| `queries/postgresql/Q1.sql` | Reemplazar la consulta de orders |
| `queries/postgresql/Q2.sql` | Reemplazar la consulta de orders |
| `scripts/parcel_reference.py` | Agregar el cálculo independiente y los comparadores |
| `scripts/validate_queries.py` | Reemplazar el validador anterior |
| `tests/test_parcel_queries.py` | Agregar pruebas pequeñas |
| `docs/consultas_paqueteria.md` | Agregar este procedimiento |

El Dockerfile actual del validador ya copia `scripts/` y `queries/` completos.
No requiere editar Compose, dependencias ni Dockerfiles. El cargador de
paquetería del paso anterior debe estar aplicado porque se reutiliza su
verificación del archivo fuente.

## Operaciones equivalentes

| Consulta | MongoDB | PostgreSQL |
| --- | --- | --- |
| Q1 | Desanida paquetes; convierte las fechas a Date; calcula minutos con dateDiff; filtra retrasos positivos; agrupa por centro y servicio | Desanida paquetes JSONB; convierte fechas a timestamptz; calcula segundos de la diferencia y divide entre 60; filtra y agrupa con los mismos criterios |
| Q2 | Filtra finalizados y fecha de cierre; ordena el retraso acumulado descendente; limita a 1000 | Aplica los mismos filtros, ordenamiento y límite sobre JSONB |

Q1 incluye paquetes entregados de envíos todavía abiertos. Excluye entregas
anticipadas, puntuales y paquetes sin entregar. Devuelve `centro_id`,
`servicio`, `paquetes_retrasados`, `retraso_total_min` y `retraso_promedio_min`.

Todas las fechas tienen segundos 00 y usan UTC. Por ello, los límites de
minuto de dateDiff coinciden aquí con los minutos transcurridos calculados
en PostgreSQL y Python. No se extiende esa equivalencia a fechas con segundos
o fracciones: el contrato y la referencia rechazan esa entrada.

Q2 filtra `estado=finalizado` y el intervalo de cierre
`[2026-01-01T00:00:00Z, 2026-10-01T00:00:00Z)`. Devuelve `envio_id`,
`centro_id`, `servicio`, `fecha_cierre` y `retraso_acumulado_min`.
La comparación de fecha es textual en ambos motores. PostgreSQL utiliza
`data->>'fecha_cierre'`, igual que el índice existente, sin convertir esa
expresión a timestamptz en el filtro.

No se agrega orden secundario a Q2 ni se exige que todos los motores escojan
exactamente los mismos identificadores entre los empatados en el corte.

## Referencia independiente

`parcel_reference.py` lee `data/envios.jsonl` sin conectarse a las bases ni
importar el generador. Recalcula cada retraso desde las fechas; comprueba los
estados, el cierre y la suma almacenada de los envíos finalizados. Para Q1
acumula conteos y sumas por centro y servicio. Para Q2 conserva los envíos
superiores al corte y todos los empatados que podrían ocupar sus posiciones.
No guarda una copia completa del dataset.

El resultado se escribe una vez en
`results/paqueteria/query_reference.json`. Sus conteos deben coincidir con el
manifiesto: 500000 envíos, 1500000 paquetes, 488134 paquetes seleccionados por
Q1 y 160953 envíos candidatos a Q2. El valor de corte y las sumas se toman del
archivo real; no se fijan de antemano.

La comparación exige igualdad exacta de grupos, conteos, sumas de minutos y
campos de Q2. Sólo el promedio de Q1 admite una tolerancia numérica de
`max(0.000001, 0.000000000001 * abs(promedio_esperado))`, porque MongoDB y
PostgreSQL pueden representar el promedio con tipos numéricos distintos.
Esta tolerancia no se aplica al conteo ni a las sumas.

Para Q2 se comprueban 1000 identificadores únicos, valores y campos correctos,
orden descendente, inclusión de todos los envíos superiores al corte y una
selección válida entre los empatados.

## Qué se ejecuta

| Motor | Casos |
| --- | --- |
| PostgreSQL | Q1 con la configuración predeterminada; Q2 con work_mem 4MB; Q2 con work_mem 64MB |
| MongoDB | Q1 con la configuración predeterminada; Q2 con allowDiskUse false; Q2 con allowDiskUse true |

Por cada caso se ejecuta una consulta para obtener y comparar resultados.
Si la comparación es correcta, se obtiene un plan con estadísticas reales:
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` o `explain` con `executionStats`.
Obtener esas estadísticas vuelve a ejecutar la consulta. Si todos los casos
son correctos, son seis ejecuciones por motor, doce en total, sin bucles de
repetición. No se recarga ni regenera el dataset.

Este paso no produce latencias ni throughput para la matriz del benchmark.
No aplica warmup, medición 300s, cooldown ni limpieza de caché. Esas condiciones
corresponden al ejecutor de mediciones, que se adaptará después.

`status=ok` en este paso significa resultados correctos y plan guardado. El
plan puede mostrar uso de temporales; no se exige ni se garantiza spill u OOM.

## 1. Construir y probar el validador

Desde PowerShell, en la raíz del repositorio, con los ocho archivos colocados:

```powershell
$env:PROFILE_ID = "PROFILE_A_BASELINE"
$env:DB_CPUS = "4"
$env:DB_MEMORY = "4096m"
docker compose --profile mongo --profile postgres --profile tools config --quiet
docker compose --profile tools build query-validator
```

La construcción incorpora los archivos nuevos. Se reutilizan las capas
disponibles; no se pide actualizar imágenes ni construir sin caché.

Pruebas pequeñas, con carpeta de pruebas montada adicionalmente:

```powershell
docker compose --profile tools run --rm --no-deps --pull never --entrypoint python -v "${PWD}/tests:/work/tests:ro" query-validator -m unittest discover -s tests -p test_parcel_queries.py -v
```

Se esperan 21 pruebas terminadas en `OK`. Estas pruebas validan los cálculos
y el control de ejecución con ejemplos pequeños; las consultas reales de los
motores se comprueban en los pasos 3 y 4.

## 2. Calcular la referencia una vez

```powershell
docker compose --profile tools run --rm --no-deps --pull never query-validator reference
```

La salida informa `q1_groups`, `q2_candidates`, `q2_required`,
`q2_boundary_candidates` y `q2_cutoff_min`. `q2_candidates` debe ser 160953.
La suma de los grupos de Q1 debe representar los 488134 paquetes retrasados.
Con seis centros y dos servicios puede haber hasta doce grupos; la referencia
determina cuáles existen realmente.

## 3. Validar PostgreSQL y guardar sus planes

Se empieza con PostgreSQL porque fue el último motor utilizado en la carga.
Se conserva un solo motor de base de datos encendido para estas consultas:

```powershell
docker compose --profile mongo stop mongodb
docker compose --profile postgres up -d --no-deps --pull never postgresql
docker compose --profile postgres exec postgresql pg_isready
```

Cuando indique `accepting connections`:

```powershell
docker compose --profile tools run --rm --no-deps --pull never query-validator postgresql
```

Se esperan tres casos con `status: ok` y el resumen final `status: ok`.
Q1 debe devolver el número de grupos de la referencia y cada variante de Q2
debe devolver 1000 filas.

## 4. Validar MongoDB y guardar sus planes

Tras completar correctamente PostgreSQL:

```powershell
docker compose --profile postgres stop postgresql
docker compose --profile mongo up -d --no-deps --pull never mongodb
docker compose --profile tools run --rm --no-deps --pull never query-validator mongodb
```

Se esperan los mismos tres estados correctos: Q1 y las dos variantes de Q2.
Los campos y los resultados se comparan con la misma referencia. Los empates
pueden cambiar el conjunto exacto seleccionado por Q2 sin causar un error.

Si aparece un error, conservar la salida y revisar antes de repetir comandos.
El programa no sobrescribe referencias, resultados, planes ni reportes
existentes, y no reintenta automáticamente una consulta fallida.

## 5. Evidencias y commit

Los archivos de este paso se guardan en:

- `results/paqueteria/query_reference.json`.
- `results/paqueteria/validation_postgresql_PROFILE_A_BASELINE.json`.
- `results/paqueteria/validation_mongodb_PROFILE_A_BASELINE.json`.
- `results/paqueteria/query_results/PROFILE_A_BASELINE/`: seis resultados.
- `evidence/paqueteria/plans/PROFILE_A_BASELINE/`: seis planes.
- `evidence/paqueteria/errors/PROFILE_A_BASELINE/`: errores, sólo si ocurren.

No se solicitan capturas para este paso: se conservan los archivos de
resultados, planes y reportes. Cada reporte registra el hash del dataset, de
las consultas y de la referencia, además de versiones y configuración del
servidor. Los archivos anteriores de orders permanecen en sus rutas previas.

Revisar los dos reportes finales:

```powershell
Get-Content .\results\paqueteria\validation_postgresql_PROFILE_A_BASELINE.json
Get-Content .\results\paqueteria\validation_mongodb_PROFILE_A_BASELINE.json
```

Cuando ambos tengan `status=ok` y sus tres casos sean correctos:

```powershell
git add scripts/parcel_reference.py scripts/validate_queries.py queries/mongodb/Q1.json queries/mongodb/Q2.json queries/postgresql/Q1.sql queries/postgresql/Q2.sql tests/test_parcel_queries.py docs/consultas_paqueteria.md results/paqueteria/query_reference.json results/paqueteria/validation_postgresql_PROFILE_A_BASELINE.json results/paqueteria/validation_mongodb_PROFILE_A_BASELINE.json results/paqueteria/query_results/PROFILE_A_BASELINE evidence/paqueteria/plans/PROFILE_A_BASELINE
git diff --cached --check
git diff --cached --stat
git commit -m "feat: valida consultas de paqueteria y guarda sus planes"
git push
```

El siguiente paso adaptará el ejecutor de mediciones a envíos y a estas rutas.
El piloto anterior de orders no se reutiliza como resultado de paquetería.
Todavía no se ejecutan el piloto ni la matriz de rendimiento.

## Referencias técnicas

- [MongoDB dateDiff](https://www.mongodb.com/docs/manual/reference/operator/aggregation/datediff/).
- [Funciones de fecha y hora de PostgreSQL](https://www.postgresql.org/docs/18/functions-datetime.html).
- [EXPLAIN de PostgreSQL](https://www.postgresql.org/docs/18/sql-explain.html).
