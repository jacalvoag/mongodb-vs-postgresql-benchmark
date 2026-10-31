# Fase 4: consultas, validacion y planes de ejecucion

Se conservan las consultas Q1 y Q2 del manual. Las variantes de Q2 se aplican
como opciones de ejecucion: MongoDB allowDiskUse false/true y PostgreSQL
work_mem 4MB/64MB. Q1 utiliza la configuracion predeterminada de cada motor.

La validacion se realiza con el perfil A y un motor en ejecucion a la vez.
El servicio query-validator utiliza los mismos controladores de requirements.txt.

## Referencia independiente

El modo reference verifica el SHA-256 y calcula resultados esperados desde
data/orders.jsonl, sin consultar las bases de datos. Guarda
results/query_reference.json. Q1 utiliza calculos enteros en centavos.
Q2 conserva todas las ordenes superiores al importe de corte y todos los
candidatos empatados exactamente en ese corte.

## Comparacion

Q1 exige los mismos grupos y conteos. Para sumas y promedios utiliza:

    abs(actual - esperado) <= max(0.000001, abs(esperado) * 0.000000000001)

Los resultados se alinean por pais y categoria; las consultas no imponen
un orden a los grupos. Q2 exige 1000 ordenes distintas, orden descendente,
clientes e importes correctos y todas las ordenes superiores al corte.
Se acepta cualquier seleccion valida entre los empates en el corte.
El campo automatico _id de MongoDB se omite solamente al comparar resultados.

## Ejecucion desde PowerShell

Conservar PROFILE_A_BASELINE, DB_CPUS=4 y DB_MEMORY=4096m en .env.

```powershell
docker compose --profile tools config --quiet
docker compose --profile tools build query-validator
docker compose --profile tools run --rm --no-deps query-validator reference

docker compose --profile postgres stop postgresql
docker compose --profile mongo up -d mongodb
docker compose --profile tools run --rm --no-deps query-validator mongodb

docker compose --profile mongo stop mongodb
docker compose --profile postgres up -d postgresql
docker compose --profile tools run --rm --no-deps query-validator postgresql
```

Comprobar disponibilidad de cada motor antes de ejecutar su validacion.

## Evidencia

Los resultados normalizados se guardan en results/query_results/PROFILE_A_BASELINE/.
Los planes se guardan en evidence/plans/PROFILE_A_BASELINE/: MongoDB explain
executionStats y PostgreSQL EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON).
Si ocurre un error, su detalle se guarda en evidence/errors/PROFILE_A_BASELINE/.
Los reportes results/validation_mongodb_PROFILE_A_BASELINE.json y
results/validation_postgresql_PROFILE_A_BASELINE.json incluyen versiones,
hashes de consultas, configuraciones elegidas y estado de cada caso.

Cada caso ejecuta la consulta y despues su explain, fuera de las mediciones
formales. Los tiempos de explain no se utilizan como latencias del benchmark.
El CSV de mediciones se implementara en la fase 5.