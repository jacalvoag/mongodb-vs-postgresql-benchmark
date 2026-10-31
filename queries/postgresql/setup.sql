CREATE TABLE IF NOT EXISTS public.orders (
    id UUID PRIMARY KEY,
    data JSONB
);

CREATE INDEX IF NOT EXISTS orders_timestamp_idx
ON public.orders USING btree ((data->>'timestamp'));