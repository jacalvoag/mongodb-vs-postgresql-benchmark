# Generación de datos de paquetería

Este paso implementa el modelo de `docs/caso_uso_paqueteria.md`. Las
distribuciones siguientes son decisiones sintéticas del caso; no representan
estadísticas reales ni requisitos adicionales del docente.

## Supuestos documentados

| Aspecto | Regla de generación |
| --- | --- |
| Escala | 500,000 envíos y 1,500,000 paquetes; semilla 2026 |
| Paquetes por envío | Ciclo 1, 2, 3, 4, 5; promedio exacto 3 |
| Centros | Tuxtla, Oaxaca, Puebla, Ciudad de México, Guadalajara y Monterrey; selección uniforme |
| Servicio | Selección ponderada: estándar 70%, exprés 30% |
| Estado del envío | Selección ponderada: finalizado 75%, en tránsito 20%, cancelado 5% |
| Origen | Ciudad del centro responsable |
| Destino | Selección uniforme de las otras cinco ciudades |
| Fecha de corte | 2026-10-01T00:00:00Z |
| Creación de finalizados | Uniforme desde 2025-01-01 hasta seis días antes del corte, inclusive |
| Creación de envíos en tránsito | Uniforme entre siete días antes del corte y un minuto antes del corte |
| Creación de cancelados | Uniforme desde 2025-01-01 hasta un minuto antes del corte |
| Compromiso estándar | Creación + 3 a 5 días enteros + 0 a 360 minutos |
| Compromiso exprés | Creación + 1 a 2 días enteros + 0 a 360 minutos |
| Entrega candidata | Anticipada: peso 25%, de 1 a 360 minutos antes; puntual: 35%; tardía: 40%, de 1 a 720 minutos después |
| Cancelación | Entre creación y el menor de creación + 24 horas o fecha de corte |
| Peso | De 0.100 a 30.000 kg, incrementos de 0.001 kg |
| Dimensiones | Cada lado entre 5 y 100 cm enteros |

Las selecciones ponderadas no fuerzan conteos exactos. El manifiesto registra
los conteos efectivos. La semilla, el código y la versión de Python permiten
reproducir la generación. No se afirma que el mismo hash se conserve entre
versiones diferentes del generador o de Python.

Los días de margen permiten completar cualquier paquete de un envío
finalizado sin inventar entregas posteriores al corte. Para envíos en tránsito
se elige un paquete que permanecerá pendiente. Los demás se entregan si su
fecha candidata ya ocurrió; en caso contrario quedan pendientes. Esta regla
permite envíos parcialmente entregados y compromisos tanto vencidos como
futuros. Las ponderaciones de entrega candidata no se interpretan como
porcentajes finales de los paquetes abiertos, porque se aplica esta condición.

Los estados son parte de una fotografía a la fecha de corte. Por eso las
fechas de creación dependen del estado, en vez de seleccionarse de forma
independiente. Los cancelados no tienen paquetes entregados; no hay
cancelaciones parciales en esta versión.

## Salidas

- `data/envios.jsonl`: documentos UTF-8, un envío por línea y saltos LF.
- `results/paqueteria/dataset_manifest.json`: identidad del caso y versión del
  modelo, semilla, versión de Python, hash del generador, conteos, profundidad,
  intervalo de Q2, tamaño y SHA-256 de los bytes del dataset.

Los identificadores de envío son UUID deterministas derivados de semilla e
índice. Los identificadores de paquete incorporan el índice del envío y la
posición del paquete; son únicos dentro de cada dataset.

El generador recalcula los retrasos desde las fechas serializadas y comprueba
fechas, estados, cierre, acumulado, dimensiones, peso y profundidad antes de
escribir cada documento. También informa cuántos paquetes satisfacen Q1 y
cuántos envíos satisfacen el filtro de Q2. Estos son conteos de selección,
no resultados de rendimiento ni la referencia completa de las consultas.

La validación independiente de Q1/Q2 en ambos motores se adaptará en su paso
correspondiente. El validador incluido aquí verifica la integridad del dataset.

## Ejecución manual desde PowerShell

En la raíz del repositorio, con los tres archivos de este paso ya colocados:

```powershell
docker compose --profile tools config --quiet
```

Pruebas pequeñas dentro de la imagen Python ya configurada. La carpeta de
pruebas se monta adicionalmente; no se construyen imágenes:

```powershell
docker compose --profile tools run --rm --no-deps --pull never -v "${PWD}/tests:/work/tests:ro" data-generator python -m unittest discover -s tests -p test_synthetic_data_generator.py -v
```

Si todas las pruebas terminan en `OK`, generar una sola vez el dataset completo:

```powershell
docker compose --profile tools run --rm --no-deps --pull never data-generator
```

El servicio actual ya ejecuta `synthetic_data_generator.py` con 500000 registros
y semilla 2026 y monta `data` y `results`. No requiere editar el Compose.
Este comando no arranca los servicios de bases de datos ni de monitoreo.

Consultar el manifiesto:

```powershell
Get-Content .\results\paqueteria\dataset_manifest.json
```

Se esperan `case=paqueteria`, `model_version=paqueteria-v1`, `records=500000`,
`packages=1500000`, `average_packages_per_shipment=3` y profundidad máxima 3.
Los demás conteos y el hash se tomarán de la ejecución real.

No se solicitan capturas para este paso. El código y el manifiesto documentan
la generación. `data/envios.jsonl` queda excluido de Git por el `.gitignore`
existente; el manifiesto sí se versiona.

## Alcance de este cambio

Los archivos de `orders` y sus resultados no se sobrescriben. Si cualquiera de
las dos nuevas salidas ya existe, el generador se detiene para evitar una
sobrescritura accidental. No se debe repetir el comando ni borrar archivos
para obtener un resultado distinto sin revisar primero lo ocurrido.

Este cambio todavía no adapta el cargador, las consultas, el validador de
consultas ni el ejecutor de mediciones. No se debe ejecutar ninguno de ellos
con `envios.jsonl` hasta completar su adaptación.

El siguiente paso será adaptar el esquema, los índices y el cargador de ambos
motores, reutilizando exactamente este dataset. El piloto anterior no aportará
filas a la matriz de paquetería.
