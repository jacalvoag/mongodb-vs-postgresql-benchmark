SELECT data->>'envio_id' AS envio_id,
       data->'centro_distribucion'->>'id' AS centro_id,
       data->>'servicio' AS servicio,
       data->>'fecha_cierre' AS fecha_cierre,
       (data->>'retraso_acumulado_min')::bigint AS retraso_acumulado_min
FROM public.envios
WHERE data->>'estado' = 'finalizado'
  AND data->>'fecha_cierre' >= '2026-01-01T00:00:00Z'
  AND data->>'fecha_cierre' < '2026-10-01T00:00:00Z'
ORDER BY (data->>'retraso_acumulado_min')::bigint DESC
LIMIT 1000;
