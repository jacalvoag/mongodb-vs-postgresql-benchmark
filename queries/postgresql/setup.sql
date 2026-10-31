CREATE TABLE IF NOT EXISTS public.envios (
    id UUID PRIMARY KEY,
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS envios_fecha_cierre_idx
ON public.envios USING btree ((data->>'fecha_cierre'));
