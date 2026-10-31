# Matriz de mediciones de paquetería

Este paso automatiza los escenarios definidos mediante `run_benchmarks.sh`.
Conserva el dataset, índices, consultas, ejecutor y método de medición del
piloto. No modifica ninguno de los cinco archivos que definen su hash de
método. Los cambios se aplican manualmente al repositorio.

## Archivos

| Archivo | Acción |
| --- | --- |
| `run_benchmarks.sh` | Agregar el controlador Bash |
| `scripts/matrix.py` | Agregar planificación, acreditación y registro de avance |
| `tests/test_matrix.py` | Agregar pruebas sin motores ni consultas reales |
| `docs/matriz_paqueteria.md` | Agregar este procedimiento |
| `README.md` | Reemplazar la descripción antigua de orders por la de paquetería |

Se utiliza la imagen Python 3.14.10-slim ya disponible y el servicio
benchmark-runner ya preparado. No se agregan dependencias ni imágenes.

## Escenarios y duración

Se combinan dos motores, tres perfiles y concurrencias 1/10/50, con tres
repeticiones. Q1 tiene una configuración por motor; Q2 conserva sus dos
variantes: allowDiskUse false/true o work_mem 4MB/64MB.

| Parte de la matriz | Ejecuciones |
| --- | ---: |
| Q1: 2 motores × 3 perfiles × 3 concurrencias × 3 repeticiones | 54 |
| Q2: lo anterior × 2 variantes | 108 |
| Total con las variantes separadas | 162 |
| Piloto de paquetería ya registrado | 3 |
| Pendientes al empezar | 159 |

Las 36 combinaciones base de motor/perfil/consulta/concurrencia se desglosan
en 54 celdas al separar las variantes de Q2. Con tres repeticiones son 162
ejecuciones. Este desglose identifica los ajustes ya definidos; no agrega
consultas, perfiles ni concurrencias.

Cada repetición conserva 60s de calentamiento, 300s de medición y 60s de
reposo, más el drenaje de consultas pendientes. Las 159 pendientes suman
18 horas y 33 minutos nominales, más preparación y drenaje. Se puede trabajar
por bloques sin duplicar las repeticiones completadas.

## Cómo incorpora el piloto

El controlador lee `results/paqueteria/pilot.json`. No tiene un identificador
de piloto escrito directamente en el código. Comprueba caso, modelo, celda,
protocolo, hashes y las tres repeticiones registradas.

Antes de incorporarlas compara el CSV con sus metadatos y recalcula los
resúmenes desde las latencias, RSS desde las muestras, throttling desde los
contadores y la decisión de spill desde los logs. Se conservan los estados
originales, incluidos error y OOM si los hubiera. Un dato desconocido permanece
vacío, sin sustituirlo por cero.

Los archivos originales del piloto no se modifican. Sus tres filas se copian
al CSV consolidado y esas repeticiones quedan registradas, por lo que no se
programan otra vez.

## Planificación, ejecución y reanudación

Sin `--run`, el script sólo muestra el plan. El auxiliar se ejecuta en un
contenedor Python sin red y con el repositorio montado en modo de sólo
lectura. No usa Compose, conecta a las bases, arranca servicios, limpia caché,
crea el registro de matriz ni ejecuta consultas del experimento.

Con `--run`:

1. Acredita fuentes, consultas, método, versiones y perfiles.
2. Reserva un directorio que impide dos controladores simultáneos.
3. Crea o recupera el manifiesto y el CSV consolidado.
4. Programa únicamente repeticiones pendientes del bloque elegido.
5. Prepara la instrumentación y mantiene un solo motor de base de datos activo.
6. Aplica explícitamente el perfil A/B/C, incluso si PowerShell heredó valores
   del perfil A. Usa las versiones de imagen de `.env` y el nombre del proyecto
   existente para conservar sus volúmenes.
7. Comprueba disponibilidad, limpia caché Linux y ejecuta cada repetición.
8. Verifica y registra sus archivos originales. Al terminar el bloque audita
   las huellas de las evidencias registradas.

El controlador comprueba que la imagen local del ejecutor exista antes de
arrancar servicios; si falta, se detiene para evitar la construcción automática
que Compose podría realizar. No construye imágenes, descarga imágenes, recarga datos ni
elimina volúmenes. Se conservan las imágenes y la instrumentación del piloto.
Arrancar un motor con otro perfil puede recrear su contenedor; se conserva el
volumen de datos. Un motor detenido por OOM puede arrancarse para la siguiente
repetición pendiente, conservando la fila OOM ya registrada.

Las comprobaciones de disponibilidad pueden repetirse hasta cinco veces,
separadas por dos segundos. Son comprobaciones sin Q1/Q2, no reintentos de
mediciones. Una vez registrada una fila, su estado no provoca otra ejecución.

Los códigos, consultas, perfiles y versiones quedan identificados en el
manifiesto. Si cambian, la reanudación se detiene para evitar mezclar métodos.
Se normalizan exclusivamente los saltos CRLF/LF al calcular huellas de texto.
El manifiesto sólo registra las versiones de imagen de `.env`, no contraseñas.

Una repetición terminada cuyo registro no alcanzó a incorporarse al CSV se
recupera desde su metadata. Si existe una carpeta incompleta, el controlador
se detiene para revisarla: no borra archivos ni vuelve a ejecutar esa repetición
automáticamente. El CSV consolidado es derivado de las evidencias y puede
reconstruirse sin modificar las originales.

## 1. Aplicar y probar desde PowerShell

Colocar los cinco archivos del paquete en sus rutas. En la raíz del repositorio:

```powershell
docker run --rm --pull=never --network none --mount "type=bind,source=$($PWD.Path),target=/work,readonly" --workdir /work python:3.14.10-slim python -B -m unittest discover -s tests -p test_matrix.py -v
```

Se esperan 20 pruebas terminadas en `OK`. Las llamadas Docker y WSL de las
pruebas se simulan; no se ejecutan mediciones ni se limpia caché real. Si la
imagen no contiene Bash, esas pruebas de Bash se omiten y se informa; la
comprobación real de sintaxis se hace también en el paso siguiente.

## 2. Abrir Git Bash y revisar el plan

En VS Code, seleccionar Git Bash como perfil de una terminal nueva y situarse
en la raíz del repositorio. Los comandos siguientes son de Bash. En este
equipo se usa Git Bash de Windows con Docker Desktop y WSL2; no se necesita
instalar otra distribución Linux ni ejecutar este archivo con PowerShell.

```bash
bash -n run_benchmarks.sh
bash run_benchmarks.sh --plan
```

La primera orden comprueba la sintaxis sin ejecutar el script. Al empezar,
el plan debe informar:

```json
{
  "total_matrix": 162,
  "selected": 162,
  "registered": 3,
  "pending": 159,
  "reused_pilot": 3,
  "statuses": {"spill": 3},
  "nominal_pending_seconds": 66780
}
```

El JSON también indica las rutas del CSV y del manifiesto. Los valores cambian
a medida que se registran bloques. El plan puede consultarse nuevamente sin
iniciar mediciones.

## 3. Primera ejecución por bloque

Para empezar por PostgreSQL / A / Q1 / concurrencia 10:

```bash
bash run_benchmarks.sh --plan --engine postgresql --profile PROFILE_A_BASELINE --query Q1 --concurrency 10
bash run_benchmarks.sh --run --engine postgresql --profile PROFILE_A_BASELINE --query Q1 --concurrency 10
```

El bloque tiene tres repeticiones y 21 minutos nominales más preparación y
drenaje. El piloto de concurrencia 1 ya está registrado y no se repite.

También se pueden seleccionar bloques más amplios, por ejemplo todo
PostgreSQL del perfil A:

```bash
bash run_benchmarks.sh --plan --engine postgresql --profile PROFILE_A_BASELINE
bash run_benchmarks.sh --run --engine postgresql --profile PROFILE_A_BASELINE
```

Al comenzar desde el piloto, ese bloque tiene 27 ejecuciones, tres registradas
y 24 pendientes: 2 horas y 48 minutos nominales. Después de ejecutar otro
bloque, el plan mostrará únicamente lo que todavía falte.

Para ejecutar todas las pendientes:

```bash
bash run_benchmarks.sh --run
```

Para continuar posteriormente se usa el mismo comando o un filtro diferente.
No se vuelven a medir las filas ya registradas, incluidas spill, OOM y error.
Los filtros admitidos son engine, profile, query, concurrency y variant. Si se
filtra una variante, usar los nombres exactos de las consultas validadas.

Para pausar conviene terminar un bloque. Si una interrupción deja una carpeta
incompleta o un directorio `evidence/paqueteria/matrix_lock`, conservar su
contenido y revisar el estado antes de continuar. No se debe borrar una
evidencia para convertir una repetición ejecutada en pendiente.

## Salidas

- `results/paqueteria/matrix_v1/manifest.json`: escenarios, avance, origen del
  piloto, identidades y rutas de evidencia.
- `results/paqueteria/matrix_v1/measurements.csv`: CSV consolidado con las
  mismas 14 columnas. Al terminar toda la matriz tendrá 162 filas de datos.
- `results/paqueteria/matrix_v1/pending.tsv` y `readiness.log`: archivos de
  trabajo del controlador, no necesarios para el commit de resultados.
- `evidence/paqueteria/runs/<run_id>/`: metadatos, latencias, muestras y logs
  comprimidos del ejecutor existente.
- `evidence/paqueteria/cache_flush/<run_id>.json`: limpieza Linux previa.

Los identificadores nuevos son compactos, por ejemplo
`paqueteria_m1_pg_A_Q1_d_c10_r1`, para conservar rutas manejables en Windows.
El manifiesto conserva por separado los nombres completos de motor, perfil,
consulta y variante.

No se solicitan capturas adicionales: se conservan las evidencias de logs y
contadores. Permitir spill no obliga a observarlo; errores de consulta y OOM
kill siguen distinguiéndose según el método ya definido.

## Commits manuales

Tras pasar las pruebas y comprobar el plan:

```powershell
git add run_benchmarks.sh scripts/matrix.py tests/test_matrix.py docs/matriz_paqueteria.md README.md
git update-index --chmod=+x run_benchmarks.sh
git diff --cached --check
git diff --cached --stat
git commit -m "feat: automatiza matriz de paqueteria con reanudacion"
git push
```

Después de revisar los resultados de un bloque, desde PowerShell:

```powershell
$parcelMatrix = Get-Content .\results\paqueteria\matrix_v1\manifest.json -Raw | ConvertFrom-Json
git add results/paqueteria/matrix_v1/manifest.json results/paqueteria/matrix_v1/measurements.csv
foreach ($parcelJob in $parcelMatrix.jobs) {
    if ($null -ne $parcelJob.result -and $parcelJob.result.origin -eq "matrix") {
        git add $parcelJob.result.folder "evidence/paqueteria/cache_flush/$($parcelJob.run_id).json"
    }
}
git diff --cached --check
git diff --cached --stat
git commit -m "test: registra avance de la matriz de paqueteria"
git push
```

El código y la configuración permanecen constantes durante la matriz; los
commits de resultados no cambian sus huellas. Al completarla, el siguiente
paso será analizar métricas y planes y preparar las gráficas y el reporte.
