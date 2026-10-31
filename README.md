# MongoDB vs PostgreSQL JSONB: paquetería

Experimento académico para comparar consultas sobre envíos y sus paquetes
embebidos bajo límites de CPU y memoria. Utiliza MongoDB WiredTiger y
PostgreSQL JSONB en Docker, con el mismo conjunto de documentos en ambos.

## Datos y consultas

- 500,000 envíos y 1,500,000 paquetes; promedio exacto de tres por envío.
- Semilla 2026 y profundidad máxima de tres, con la raíz en nivel cero.
- Fecha de corte: 2026-10-01T00:00:00Z. Fechas UTC con precisión de minuto.
- MongoDB: colección `envios`. PostgreSQL: `public.envios(id UUID PRIMARY KEY, data JSONB)`.
- Q1: cantidad, suma y promedio de retrasos positivos por centro y servicio.
- Q2: los 1,000 envíos finalizados con mayor retraso acumulado durante
  enero-septiembre de 2026, con las variantes de memoria definidas.

El archivo `data/envios.jsonl` está excluido de Git. Su identidad y parámetros
se registran en `results/paqueteria/dataset_manifest.json`. Los supuestos
sintéticos y reglas de integridad están documentados y no se presentan como
estadísticas reales de una empresa.

## Método de medición

| Perfil | CPU | Memoria |
| --- | ---: | ---: |
| PROFILE_A_BASELINE | 4 | 4096 MiB |
| PROFILE_B_MEDIUM | 2 | 2048 MiB |
| PROFILE_C_SEVERE | 1 | 1024 MiB |

Se usan concurrencias 1/10/50 y tres repeticiones. Cada repetición incluye
limpieza de caché Linux, calentamiento 60s, medición 300s, drenaje de consultas
pendientes y reposo 60s. La monitorización conserva el intervalo nominal de
un segundo y los huecos reales observados.

El ejecutor guarda una fila de 14 columnas y evidencias originales por
repetición. Los estados son ok, spill, oom o error; no se repite una corrida
para obtener un resultado favorable. Las convenciones de latencia, throughput,
RSS y operaciones pendientes se detallan en la documentación del método.

## Reproducción y ejecución

Seguir los pasos en este orden:

1. [Caso de uso y modelo](docs/caso_uso_paqueteria.md).
2. [Generación reproducible](docs/generacion_paqueteria.md).
3. [Carga e índices en ambos motores](docs/carga_paqueteria.md).
4. [Consultas, referencia independiente y planes](docs/consultas_paqueteria.md).
5. [Método de medición](docs/fase5.md) y [ejecutor y primera celda](docs/ejecucion_paqueteria.md).
6. [Matriz completa y reanudación](docs/matriz_paqueteria.md).

Una vez preparados los pasos anteriores, desde Git Bash en Windows y en la
raíz del repositorio:

```bash
bash run_benchmarks.sh --plan
```

El modo predeterminado sólo muestra el plan. Para ejecutar un bloque:

```bash
bash run_benchmarks.sh --run --engine postgresql --profile PROFILE_A_BASELINE --query Q1 --concurrency 10
```

La matriz tiene 162 ejecuciones al separar las dos variantes de Q2. Integra
las tres repeticiones del piloto de paquetería y registra las pendientes para
continuar sin duplicarlas. Los resultados consolidados están en
`results/paqueteria/matrix_v1/` y las evidencias en `evidence/paqueteria/`.

Las credenciales locales se configuran en `.env`, excluido de Git. Se utiliza
`.env.example` como plantilla y los archivos de `config/profiles/` para los
límites. No se deben borrar volúmenes ni modificar el método durante la matriz.
