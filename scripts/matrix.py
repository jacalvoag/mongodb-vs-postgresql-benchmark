"""Plan y registro de paqueteria. Solo procesa archivos; no conecta a motores."""
import argparse
import csv
import gzip
import hashlib
import json
import math
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark_runner.__main__ import configuration
from benchmark_runner.metrics import FIELDS, PROFILES, counter_delta, require, rss_summary, status, summarize
from benchmark_runner.observe import detect_spill

CASE = 'paqueteria'
MODEL = 'paqueteria-v1'
ROOT = Path('results/paqueteria')
BASE = ROOT/'matrix_v1'
MANIFEST = BASE/'manifest.json'
CSV = BASE/'measurements.csv'
RUNS = Path('evidence/paqueteria/runs')
CACHE = Path('evidence/paqueteria/cache_flush')
PILOT_REGISTRY = ROOT/'pilot.json'
PROTOCOL = dict(warmup_seconds=60, measurement_seconds=300, cooldown_seconds=60)
VARIANTS = {'mongodb': ('allowDiskUse_false', 'allowDiskUse_true'),
            'postgresql': ('work_mem_4MB', 'work_mem_64MB')}
ARTIFACTS = ('metadata.json', 'cache_flush.json', 'latencies.json.gz',
             'cgroup_samples.json.gz', 'prometheus_samples.json.gz', 'engine.log.gz')
IMAGE_KEYS = ('MONGO_IMAGE', 'POSTGRES_IMAGE', 'PYTHON_IMAGE',
              'CADVISOR_IMAGE', 'NODE_EXPORTER_IMAGE', 'PROMETHEUS_IMAGE')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_digest(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def artifact_digest(path):
    return digest(path) if str(path).endswith('.gz') else text_digest(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='\n', dir=path.parent,
                                         prefix='matrix_tmp_', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def jobs():
    out = []
    for engine in ('postgresql', 'mongodb'):
        for profile in PROFILES:
            for query in ('Q1', 'Q2'):
                for variant in ('default',) if query == 'Q1' else VARIANTS[engine]:
                    for concurrency in (1, 10, 50):
                        for repetition in (1, 2, 3):
                            key = f'{engine}_{profile}_{query}_{variant}_c{concurrency}_r{repetition}'
                            # Nombres compactos para la ruta profunda del repositorio en Windows.
                            variant_code = {'default': 'd', 'work_mem_4MB': 'w4', 'work_mem_64MB': 'w64',
                                            'allowDiskUse_false': 's0', 'allowDiskUse_true': 's1'}[variant]
                            engine_code = 'pg' if engine == 'postgresql' else 'mg'
                            run_id = f'paqueteria_m1_{engine_code}_{profile.split("_")[1]}_{query}_{variant_code}_c{concurrency}_r{repetition}'
                            out.append(dict(key=key, engine=engine, profile=profile, query=query,
                                            variant=variant, concurrency=concurrency, repetition=repetition,
                                            run_id=run_id, result=None))
    return out


def env_values(path):
    values = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if separator:
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def inputs():
    # configuration acredita los archivos sin conectar a motores.
    configs = {}
    for engine in VARIANTS:
        for query in ('Q1', 'Q2'):
            for variant in ('default',) if query == 'Q1' else VARIANTS[engine]:
                configs[(engine, query, variant)] = configuration(
                    SimpleNamespace(engine=engine, query=query, variant=variant, run_id=None))
    first = next(iter(configs.values()))
    require(all(c['source_sha256'] == first['source_sha256'] and
                c['reference_sha256'] == first['reference_sha256'] and
                c['method_sha256'] == first['method_sha256'] for c in configs.values()),
            'Las acreditaciones no pertenecen al mismo caso, referencia y metodo.')
    paths = ['docker-compose.yml', 'compose.runner.yml', 'requirements.txt',
             'run_benchmarks.sh', 'scripts/matrix.py']
    paths += [str(p) for p in sorted(Path('benchmark_runner').glob('*.py'))]
    paths += [str(p) for p in sorted(Path('queries').rglob('*')) if p.is_file()]
    paths += [str(ROOT/name) for name in ('dataset_manifest.json', 'query_reference.json',
              'load_mongodb.json', 'load_postgresql.json',
              'validation_mongodb_PROFILE_A_BASELINE.json', 'validation_postgresql_PROFILE_A_BASELINE.json')]
    for profile, (cpus, memory) in PROFILES.items():
        path = f'config/profiles/{profile}.env'
        values = env_values(path)
        require(values.get('PROFILE_ID') == profile and values.get('DB_CPUS') == str(cpus) and
                values.get('DB_MEMORY') == f'{memory//1024**2}m', f'Perfil inconsistente: {path}')
        paths.append(path)
    values = env_values('.env')
    require(all(values.get(key) for key in IMAGE_KEYS), 'Faltan versiones de imagenes en .env.')
    images = {key: values[key] for key in IMAGE_KEYS}
    require(images['PYTHON_IMAGE'] == 'python:3.14.10-slim', 'El controlador usa la imagen Python 3.14.10 ya preparada.')
    return dict(case=CASE, model_version=MODEL, files={p: text_digest(p) for p in paths}, images=images,
                source_sha256=first['source_sha256'], reference_sha256=first['reference_sha256'],
                method_sha256=first['method_sha256'],
                query_sha256={f'{e}/{q}': c['query_sha256'] for (e, q, _), c in configs.items()},
                expected_rows={f'{e}/{q}': c['expected_rows'] for (e, q, _), c in configs.items()},
                server_versions={e: c['validated_server_version'] for (e, _, _), c in configs.items()})


def csv_value(value):
    return '' if value is None else str(value)


def check_result(job, folder, expected):
    folder = Path(folder)
    data = read(folder/'metadata.json')
    require(data.get('completed_at_utc'), f'Corrida incompleta: {folder}. Revisar; no se repetira automaticamente.')
    config, row = data['configuration'], data['row']
    require(data.get('case') == CASE and data.get('model_version') == MODEL and
            config.get('case') == CASE and config.get('model_version') == MODEL, 'Resultado de otro caso o modelo.')
    require(data['run_id'] == job['run_id'], 'Identificador de corrida distinto.')
    require(data['protocol'] == PROTOCOL, 'Protocolo distinto a 60/300/60.')
    for key in ('engine', 'profile', 'query', 'variant'):
        require(config[key] == job[key], f'{folder}: {key} no coincide.')
    require(config['source_sha256'] == expected['source_sha256'] and config['source_records'] == 500000 and
            config['source_packages'] == 1500000, 'Dataset distinto.')
    require(config['method_sha256'] == expected['method_sha256'] and
            config['reference_sha256'] == expected['reference_sha256'], 'Metodo o referencia distintos.')
    name = job['engine']+'/'+job['query']
    require(config['query_sha256'] == expected['query_sha256'][name], 'Hash de consulta distinto.')
    require(config['expected_rows'] == expected['expected_rows'][name], 'Cantidad de filas esperadas distinta.')
    require(set(row) == set(FIELDS), 'Esquema de fila incorrecto.')
    query_id = 'Q1' if job['query'] == 'Q1' else 'Q2_'+job['variant']
    require(row['engine'] == job['engine'] and row['profile'] == job['profile'] and row['query_id'] == query_id,
            'Identidad CSV incorrecta.')
    require(row['concurrency'] == job['concurrency'] and row['run_number'] == job['repetition'],
            'Concurrencia/repeticion distintas.')
    require(row['status'] in ('ok', 'spill', 'oom', 'error'), 'Estado desconocido.')
    for key in ('timestamp_epoch', 'throughput_qps', 'latency_p50_ms', 'latency_p95_ms',
                'latency_p99_ms', 'cpu_throttled_seconds', 'max_rss_mb'):
        require(row[key] is None or math.isfinite(float(row[key])) and float(row[key]) >= 0,
                'Valor numerico no valido: '+key)
    spill_value = True if row['disk_spill'] == 'true' else False if row['disk_spill'] == 'false' else None
    require(row['status'] == status(data['oom_killed'], data['issues'], spill_value), 'Estado e incidencias incompatibles.')
    cpus, memory = PROFILES[job['profile']]
    container = data['checks']['container']
    limits = container['limits']
    require(limits['NanoCpus'] == cpus*1_000_000_000 and limits['Memory'] == memory and limits['MemorySwap'] == memory,
            'Limites Docker distintos al perfil.')
    image_key = 'POSTGRES_IMAGE' if job['engine'] == 'postgresql' else 'MONGO_IMAGE'
    require(container['image'] == expected['images'][image_key], 'Imagen distinta de la configuracion registrada.')
    require(data['checks']['settings']['server_version'] == expected['server_versions'][job['engine']],
            'Version del motor distinta a la validada.')
    if row['status'] in ('ok', 'spill'):
        require(all((folder/name).is_file() for name in ARTIFACTS), 'Faltan evidencias de una corrida completada.')
        measured, summary = data['measurement'], data['summary']
        require(not data['warmup']['errors'] and not measured['errors'] and summary['failed'] == 0, 'Hay errores de clientes.')
        require(measured['end_epoch']-measured['start_epoch'] == 300 and row['timestamp_epoch'] == measured['start_epoch'],
                'Ventana de medicion incorrecta.')
        with gzip.open(folder/'latencies.json.gz', 'rt', encoding='utf-8') as stream:
            samples = json.load(stream)
        require(summarize(samples, 300) == summary, 'Las latencias no coinciden con el resumen.')
        for key in ('throughput_qps', 'latency_p50_ms', 'latency_p95_ms', 'latency_p99_ms'):
            require(row[key] == summary[key], 'Fila y resumen difieren en '+key)
        require(row['cpu_throttled_seconds'] == counter_delta(measured['cgroup_start'], measured['cgroup_end'], 'throttled_usec'),
                'Throttling inconsistente.')
        with gzip.open(folder/'prometheus_samples.json.gz', 'rt', encoding='utf-8') as stream:
            maximum, sampling = rss_summary(json.load(stream), measured['start_epoch'], measured['end_epoch'])
        require(row['max_rss_mb'] == maximum and data['rss_sampling'] == sampling, 'RSS o muestreo inconsistentes.')
        with gzip.open(folder/'engine.log.gz', 'rt', encoding='utf-8') as stream:
            spill = detect_spill(job['engine'], stream.read(), config['tag'], summary['successful'], config['marker'])
        require(spill['value'] == spill_value and spill['value'] is not None, 'Logs y estado de spill diferentes.')
        require(all(row[key] is not None for key in FIELDS), 'Datos incompletos en fila ok/spill.')
    cache = read(folder/'cache_flush.json')
    require(cache.get('case') == CASE and cache.get('source_sha256') == expected['source_sha256'] and
            cache['status'] == 'ok' and cache['run_id'] == job['run_id'] and
            cache['engine'] == job['engine'] and cache['profile'] == job['profile'], 'Registro de cache incoherente.')
    return dict(folder=str(folder), row=row,
                sources={str(folder/name): artifact_digest(folder/name) for name in ARTIFACTS if (folder/name).is_file()})


def fresh():
    manifest = dict(case=CASE, model_version=MODEL, format_version=2, created_at_utc=now(), inputs=inputs(), jobs=jobs())
    pilot = read(PILOT_REGISTRY)
    require(pilot.get('case') == CASE and pilot.get('model_version') == MODEL and pilot.get('status') == 'completed',
            'No hay un piloto completo de paqueteria registrado.')
    for key, value in dict(engine='postgresql', profile='PROFILE_A_BASELINE', query='Q1',
                           variant='default', concurrency=1, planned_repetitions=3, protocol=PROTOCOL).items():
        require(pilot.get(key) == value, f'La celda piloto no coincide: {key}.')
    expected = manifest['inputs']
    for key in ('source_sha256', 'reference_sha256', 'method_sha256'):
        require(pilot[key] == expected[key], 'El piloto pertenece a otro dataset, referencia o metodo.')
    require(pilot['query_sha256'] == expected['query_sha256']['postgresql/Q1'], 'Consulta piloto distinta.')
    csv_path = Path(pilot['csv'])
    require(csv_path.resolve().is_relative_to(ROOT.resolve()), 'CSV piloto fuera de paqueteria.')
    with csv_path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        require(reader.fieldnames == FIELDS and len(rows) == 3, 'El CSV piloto no tiene las tres filas esperadas.')
    runs = pilot['runs']
    require(len(runs) == 3 and {r['run_number'] for r in runs} == {1, 2, 3}, 'Repeticiones piloto incorrectas.')
    for job in manifest['jobs']:
        if (job['engine'], job['profile'], job['query'], job['concurrency']) != ('postgresql', 'PROFILE_A_BASELINE', 'Q1', 1):
            continue
        item = next(r for r in runs if r['run_number'] == job['repetition'])
        job['run_id'] = item['run_id']
        folder = RUNS/job['run_id']
        require(item['metadata'] == str(folder/'metadata.json'), 'Ruta piloto inconsistente.')
        result = check_result(job, folder, expected)
        matches = [row for row in rows if row['run_number'] == str(job['repetition'])]
        require(len(matches) == 1 and all(matches[0][key] == csv_value(result['row'][key]) for key in FIELDS),
                'CSV del piloto y metadata no coinciden.')
        require(item['status'] == result['row']['status'], 'Estado piloto inconsistente.')
        result['origin'] = 'pilot'
        job['result'] = result
    manifest['pilot'] = dict(registry=str(PILOT_REGISTRY), registry_sha256=artifact_digest(PILOT_REGISTRY),
                             csv=str(csv_path), csv_sha256=artifact_digest(csv_path))
    return manifest


def load():
    if not MANIFEST.exists():
        require(not CSV.exists(), 'Existe el CSV de matriz sin manifest. No se sobrescribira.')
        return fresh()
    manifest = read(MANIFEST)
    require(manifest.get('case') == CASE and manifest['format_version'] == 2, 'Version o caso de registro desconocidos.')
    require(manifest['inputs'] == inputs(), 'Cambio el codigo, consultas, perfiles o imagenes. Revisar antes de continuar.')
    pilot = manifest['pilot']
    require(artifact_digest(pilot['registry']) == pilot['registry_sha256'] and
            artifact_digest(pilot['csv']) == pilot['csv_sha256'], 'Cambio el registro o CSV original del piloto.')
    for job in manifest['jobs']:
        if job['result']:
            result = job['result']
            metadata = str(Path(result['folder'])/'metadata.json')
            require(artifact_digest(metadata) == result['sources'][metadata], 'Cambio un resultado registrado: '+metadata)
            require(all(Path(path).is_file() for path in result['sources']), 'Faltan archivos de evidencia registrados.')
    return manifest


def refresh(manifest):
    for job in manifest['jobs']:
        if job['result'] is None:
            folder = RUNS/job['run_id']
            if folder.exists():
                require((folder/'metadata.json').is_file(), f'Carpeta sin metadatos: {folder}. No se borra ni se repite.')
                result = check_result(job, folder, manifest['inputs'])
                result['origin'] = 'matrix'
                job['result'] = result
    return manifest


def persist(manifest):
    BASE.mkdir(parents=True, exist_ok=True)
    rows = [job['result']['row'] for job in manifest['jobs'] if job['result']]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', dir=BASE,
                                         prefix='matrix_tmp_', delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(CSV)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    manifest['updated_at_utc'] = now()
    write_json(MANIFEST, manifest)


def selected(manifest, args):
    keys = ('engine', 'profile', 'query', 'variant', 'concurrency')
    return [job for job in manifest['jobs'] if all(getattr(args, key, None) in (None, job[key]) for key in keys)]


def show(manifest, args):
    subset = selected(manifest, args)
    require(subset, 'Los filtros no seleccionan ninguna ejecucion.')
    done = [job for job in subset if job['result']]
    pending = len(subset)-len(done)
    print(json.dumps(dict(case=CASE, total_matrix=len(manifest['jobs']), selected=len(subset),
                          registered=len(done), pending=pending,
                          reused_pilot=sum(bool(j['result'] and j['result']['origin'] == 'pilot') for j in subset),
                          statuses=dict(Counter(j['result']['row']['status'] for j in done)),
                          nominal_pending_seconds=pending*420, csv=str(CSV), manifest=str(MANIFEST)), indent=2))


def audit(manifest):
    for job in manifest['jobs']:
        if job['result']:
            for path, checksum in job['result']['sources'].items():
                require(artifact_digest(path) == checksum, 'Cambio una evidencia: '+path)
    print(json.dumps(dict(status='ok', audited_runs=sum(bool(j['result']) for j in manifest['jobs'])), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('plan', 'init', 'tasks', 'cache', 'sync', 'audit'))
    parser.add_argument('--engine', choices=tuple(VARIANTS))
    parser.add_argument('--profile', choices=tuple(PROFILES))
    parser.add_argument('--query', choices=('Q1', 'Q2'))
    parser.add_argument('--variant', choices=('default',)+VARIANTS['mongodb']+VARIANTS['postgresql'])
    parser.add_argument('--concurrency', type=int, choices=(1, 10, 50))
    parser.add_argument('--run-id')
    parser.add_argument('--cache-output')
    parser.add_argument('--cache-scope', default='Kernel Linux')
    args = parser.parse_args()
    manifest = refresh(load())
    if args.mode == 'plan':
        show(manifest, args)
    elif args.mode == 'tasks':
        for job in selected(manifest, args):
            if not job['result']:
                print('\t'.join(str(job[key]) for key in
                      ('engine', 'profile', 'query', 'variant', 'concurrency', 'repetition', 'run_id')))
    elif args.mode == 'cache':
        matches = [job for job in manifest['jobs'] if job['run_id'] == args.run_id]
        require(len(matches) == 1 and matches[0]['result'] is None, 'Corrida desconocida o ya registrada.')
        require(args.cache_output and 'CACHE_LINUX_OK' in args.cache_output.splitlines(), 'No se acredita la limpieza Linux.')
        job = matches[0]
        write_json(CACHE/(job['run_id']+'.json'), dict(case=CASE, source_sha256=manifest['inputs']['source_sha256'],
                   run_id=job['run_id'], engine=job['engine'], profile=job['profile'], status='ok',
                   completed_at_utc=now(), command='sync; echo 3 > /proc/sys/vm/drop_caches',
                   scope=args.cache_scope, output=args.cache_output))
    elif args.mode == 'audit':
        audit(manifest)
    else:
        persist(manifest)
        if args.mode == 'init':
            show(manifest, args)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(f'Matriz detenida: {type(error).__name__}: {error}')
