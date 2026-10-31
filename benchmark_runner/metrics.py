import csv
import math
from pathlib import Path

FIELDS = ['timestamp_epoch', 'engine', 'profile', 'query_id', 'concurrency', 'run_number',
          'throughput_qps', 'latency_p50_ms', 'latency_p95_ms', 'latency_p99_ms',
          'cpu_throttled_seconds', 'max_rss_mb', 'disk_spill', 'status']
PROFILES = {'PROFILE_A_BASELINE': (4, 4096 * 1024**2),
            'PROFILE_B_MEDIUM': (2, 2048 * 1024**2),
            'PROFILE_C_SEVERE': (1, 1024 * 1024**2)}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def summarize(samples, seconds):
    good = [s for s in samples if s['ok']]
    finished = sum(s['end_s'] <= seconds for s in good)
    latency = [s['latency_ms'] for s in good]
    return dict(admitted=len(samples), successful=len(good), failed=len(samples)-len(good),
                completed_in_window=finished, completed_after_window=len(good)-finished,
                throughput_qps=finished/seconds,
                latency_p50_ms=percentile(latency, .50),
                latency_p95_ms=percentile(latency, .95),
                latency_p99_ms=percentile(latency, .99))


def counter_delta(before, after, field):
    delta = after['cpu'][field] - before['cpu'][field]
    require(delta >= 0, f'El contador {field} se reinicio.')
    return delta / 1_000_000


def coverage(values, start, end):
    require(end > start and values, 'Faltan muestras en la ventana de medicion.')
    times = [float(t) for t, _ in values]
    require(all(math.isfinite(float(v)) and math.isfinite(t) for t, (_, v) in zip(times, values)), 'Muestras no finitas.')
    require(all(start <= t <= end for t in times), 'Muestras fuera de la ventana.')
    require(all(b > a for a, b in zip(times, times[1:])), 'Marcas de tiempo repetidas o desordenadas.')
    return dict(samples=len(times), nominal_interval_seconds=1,
                samples_per_second=len(times)/(end-start),
                first_offset_seconds=times[0]-start, last_offset_seconds=end-times[-1],
                max_gap_seconds=max((b-a for a, b in zip(times, times[1:])), default=0))


def rss_summary(telemetry, start, end):
    series = telemetry['container_memory_rss']['result']
    require(len(series) == 1, 'Se esperaba una serie RSS del contenedor medido.')
    values = series[0]['values']
    detail = coverage(values, start, end)
    require(all(float(v) >= 0 for _, v in values), 'RSS negativo.')
    return max(float(v) for _, v in values)/1024**2, detail


def status(oom, issues, spill):
    return 'oom' if oom else 'error' if issues or spill is None else 'spill' if spill is True else 'ok'


def append_csv(path, row):
    require(set(row) == set(FIELDS), 'La fila no coincide con las 14 columnas del manual.')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            require(next(csv.reader(stream)) == FIELDS, 'El CSV existente tiene otro encabezado.')
    with path.open('a', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator='\n')
        if not exists:
            writer.writeheader()
        writer.writerow({k: '' if v is None else v for k, v in row.items()})
