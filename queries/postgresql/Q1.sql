WITH paquetes_entregados AS (
    SELECT e.data->'centro_distribucion'->>'id' AS centro_id,
           e.data->>'servicio' AS servicio,
           EXTRACT(EPOCH FROM (
               (p.paquete->>'fecha_entrega')::timestamptz
               - (p.paquete->>'fecha_compromiso')::timestamptz
           )) / 60 AS retraso_min
    FROM public.envios AS e
    CROSS JOIN LATERAL jsonb_array_elements(e.data->'paquetes') AS p(paquete)
    WHERE p.paquete->>'estado' = 'entregado'
      AND jsonb_typeof(p.paquete->'fecha_entrega') = 'string'
)
SELECT centro_id,
       servicio,
       COUNT(*) AS paquetes_retrasados,
       SUM(retraso_min) AS retraso_total_min,
       AVG(retraso_min) AS retraso_promedio_min
FROM paquetes_entregados
WHERE retraso_min > 0
GROUP BY centro_id, servicio;
