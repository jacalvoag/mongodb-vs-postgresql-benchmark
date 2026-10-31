# mongodb-vs-postgresql-benchmark

## Dataset sintético

El dataset se genera con synthetic_data_generator.py mediante el servicio
data-generator de Docker Compose.

Configuración:
- 500 000 órdenes.
- Semilla: 2026.
- Entre 1 y 5 ítems por orden, con promedio exacto de 3.
- Fechas desde 2025-01-01 hasta antes de 2027-01-01, en UTC.
- Estados completed, pending y cancelled con pesos de 75, 15 y 10.
- Precios entre 1.00 y 250.00; cantidades entre 1 y 5.
- Importes calculados en centavos, con redondeo al centavo más cercano
  y mitades hacia arriba para descuentos e impuestos por ítem.
- Tres niveles de anidamiento contando la raíz como nivel cero.

Las distribuciones son decisiones de implementación del equipo.

El archivo data/orders.jsonl contiene una orden JSON por línea.
results/dataset_manifest.json registra los parámetros, conteos, versión
de Python y SHA-256.

Ambos motores deben cargarse desde el mismo archivo. El dataset generado
se excluye de Git; el código y el resumen permiten reproducirlo y verificarlo.