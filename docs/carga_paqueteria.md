# Carga y comprobación del dataset de paquetería

Este paso lleva el mismo archivo `data/envios.jsonl` a MongoDB y PostgreSQL
JSONB. No ejecuta el benchmark ni las consultas Q1/Q2 anteriores de `orders`.

## Archivos y destinos

| Archivo | Cambio |
| --- | --- |
| `scripts/load_dataset.py` | Reemplazar el cargador de orders por el de envíos |
| `queries/postgresql/setup.sql` | Tabla public.envios, PK UUID e índice de fecha_cierre |
| `queries/mongodb/indexes.json` | Índice de fecha_cierre e índice único de envio_id |
| `tests/test_load_dataset.py` | Agregar pruebas pequeñas sin conectar con servidores |
| `docs/carga_paqueteria.md` | Agregar este procedimiento |

MongoDB usa la colección `envios` de la base configurada en `MONGO_DB`.
PostgreSQL usa `public.envios` de `POSTGRES_DB`, con `id` igual a `envio_id` y
el documento completo dentro de `data JSONB`. Las fechas se conservan como
cadenas UTC canónicas en ambos motores. El índice de PostgreSQL utiliza
`data->>'fecha_cierre'`; la futura Q2 deberá usar esa misma expresión textual.

MongoDB mantiene su índice automático `_id_`. Su índice `envios_envio_id_idx`
tiene `unique=true`, que ahora sí transmite el cargador a `create_index`.
PostgreSQL asegura la identidad mediante su clave primaria. No se crean
índices sobre el retraso acumulado.

Los destinos nuevos deben estar vacíos. El cargador no elimina, trunca ni
modifica los datos de `orders`. Tampoco sobrescribe reportes existentes.

## Qué se comprueba

Antes de abrir una conexión, se comprueban el caso, la versión del modelo,
semilla, escala, formato, profundidad y periodo de Q2 del manifiesto. Después
se verifican el tamaño y el SHA-256 de los bytes de `envios.jsonl`.

Cada motor comprueba en sus datos cargados:

| Campo | Valor de este dataset |
| --- | ---: |
| records | 500000 |
| packages | 1500000 |
| q1_matching_packages | 488134 |
| q2_matching_shipments | 160953 |

Para contar Q1 basta comprobar paquete entregado y fecha_entrega mayor que
fecha_compromiso. El formato UTC fijo hace equivalente esta comparación
textual a un retraso positivo. La futura Q1 convertirá las fechas para calcular
la duración de los retrasos. Estos conteos de carga no sustituyen la validación
posterior de las sumas, promedios y resultados completos de Q1/Q2.

La identidad del archivo de este paso es:

```text
bd0e8cc1c0875bfef50907a8180710cb6e01c08154d6115af2592e01495f6920
```

El cargador toma los valores esperados del manifiesto verificado. Guarda los
conteos reales, índices, versiones, origen y fecha de comprobación en:

- `results/paqueteria/load_mongodb.json`
- `results/paqueteria/load_postgresql.json`

## 1. Preparación en PowerShell

Colocar los cinco archivos en las rutas de la tabla inicial, desde la raíz
del repositorio. No se necesita cambiar el Dockerfile, las dependencias ni
el Compose. El Dockerfile existente ya copia el cargador y `queries/`.

Usar el perfil base al arrancar los motores:

```powershell
$env:PROFILE_ID = "PROFILE_A_BASELINE"
$env:DB_CPUS = "4"
$env:DB_MEMORY = "4096m"
docker compose --profile mongo --profile postgres --profile tools config --quiet
docker compose --profile tools build data-loader
```

La construcción es necesaria para incorporar el código nuevo en la imagen
del cargador. Se conservan las capas que Docker pueda reutilizar; no se pide
`--no-cache`, actualizar imágenes ni descargar versiones distintas.

Ejecutar las pruebas pequeñas de control del cargador:

```powershell
docker compose --profile tools run --rm --no-deps --pull never --entrypoint python -v "${PWD}/tests:/work/tests:ro" data-loader -m unittest discover -s tests -p test_load_dataset.py -v
```

Estas pruebas usan datos temporales y conexiones simuladas. Comprueban la
identidad de la fuente, protección contra cargas duplicadas, transmisión de
la unicidad del índice, el uso de envio_id en COPY y los reportes. No son una
prueba de integración con MongoDB/PostgreSQL; esa comprobación se hace a
continuación en los motores del equipo. Continuar si terminan en `OK`.

## 2. Cargar MongoDB

Detener PostgreSQL si está ejecutándose y arrancar únicamente el servicio de
MongoDB, con sus datos persistentes actuales:

```powershell
docker compose --profile postgres stop postgresql
docker compose --profile mongo up -d --no-deps --pull never mongodb
docker compose --profile tools run --rm --no-deps --pull never data-loader mongodb
```

El cliente tiene un plazo de selección del servidor de diez segundos al
conectarse. Una carga correcta termina con `status: ok`, colección `envios`,
los cuatro conteos de la tabla y el índice `envios_envio_id_idx` con
`unique: true`. Su `source_sha256` debe coincidir con el manifiesto.

## 3. Cargar PostgreSQL

Tras la carga correcta de MongoDB:

```powershell
docker compose --profile mongo stop mongodb
docker compose --profile postgres up -d --no-deps --pull never postgresql
docker compose --profile postgres exec postgresql pg_isready
```

Cuando `pg_isready` indique `accepting connections`, ejecutar:

```powershell
docker compose --profile tools run --rm --no-deps --pull never data-loader postgresql
```

El cargador usa COPY dentro de una transacción, ejecuta ANALYZE y comprueba los
conteos antes de confirmar la transacción. Una excepción dentro del bloque
de conexión impide que esa carga se confirme. El resultado correcto indica
`status: ok`, tabla `public.envios`, los cuatro conteos y los índices
`envios_pkey` y `envios_fecha_cierre_idx`.

Si una carga falla, conservar la salida y revisarla antes de volver a
ejecutar el comando. MongoDB puede conservar un lote parcial; este script
se detendrá ante un destino con documentos en lugar de borrar o duplicar
datos. No se debe usar `down -v`, eliminar volúmenes ni regenerar el dataset
como parte de este procedimiento.

## 4. Revisar y registrar

```powershell
Get-Content .\results\paqueteria\load_mongodb.json
Get-Content .\results\paqueteria\load_postgresql.json
```

No se solicitan capturas para este paso. Los dos reportes conservan la
comprobación de carga y los índices. Tras comprobar ambas cargas:

```powershell
git add scripts/load_dataset.py queries/postgresql/setup.sql queries/mongodb/indexes.json tests/test_load_dataset.py docs/carga_paqueteria.md results/paqueteria/load_mongodb.json results/paqueteria/load_postgresql.json
git diff --cached --check
git diff --cached --stat
git commit -m "feat: carga y verifica paqueteria en ambos motores"
git push
```

El siguiente paso es implementar Q1/Q2 de paquetería y su referencia
independiente. El validador y el ejecutor anteriores todavía corresponden a
`orders` y no se ejecutan en este paso.

## Referencias técnicas

- [Índices únicos de MongoDB](https://www.mongodb.com/docs/manual/core/index-unique/create/).
- [COPY con Psycopg](https://www.psycopg.org/psycopg3/docs/basic/copy.html).
- [Transacciones con Psycopg](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).
