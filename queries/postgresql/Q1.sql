SELECT data->'customer'->'geo'->>'country' AS country,
       item->>'category' AS category,
       SUM((item->>'qty')::numeric * (item->>'price')::numeric) AS total_revenue,
       AVG((item->>'qty')::numeric * (item->>'price')::numeric) AS avg_revenue,
       COUNT(*) AS count
FROM orders, jsonb_array_elements(data->'items') AS item
WHERE (item->>'price')::numeric > 50
GROUP BY 1, 2;