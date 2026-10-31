import argparse
import gzip
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .load import Clients, Session
from .metrics import FIELDS, PROFILES, append_csv, counter_delta, require, rss_summary, status, summarize
from .observe import Docker, Group, Prometheus, detect_spill, public_info


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, data):
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def compressed(path, data):
    with gzip.open(path,'wt',encoding='utf-8') as stream:
        json.dump(data,stream,ensure_ascii=False)


def configuration(args):
    profile=os.environ.get('PROFILE_ID','PROFILE_A_BASELINE')
    require(profile in PROFILES,'Perfil desconocido.')
    variants=(['default'] if args.query=='Q1' else
              ['allowDiskUse_false','allowDiskUse_true'] if args.engine=='mongodb' else
              ['work_mem_4MB','work_mem_64MB'])
    variant=args.variant or ('default' if args.query=='Q1' else None)
    require(variant in variants,f'Indica una variante de {variants}.')
    manifest=read('results/dataset_manifest.json')
    validation=read(f'results/validation_{args.engine}_PROFILE_A_BASELINE.json')
    loaded=read(f'results/load_{args.engine}.json')
    reference=read('results/query_reference.json')
    require(manifest['records']==500000,'Dataset distinto al requerido.')
    for report in (validation,loaded):
        require(report['status']=='ok' and report['source_sha256']==manifest['sha256'],'Carga o validacion de fase 4 no acreditada.')
    require(reference['source_sha256']==manifest['sha256'],'Referencia de otro dataset.')
    require(any(c['query_id']==args.query and c['variant']==variant and c['status']=='ok' for c in validation['cases']),'Consulta/variante no validada.')
    suffix='json' if args.engine=='mongodb' else 'sql'
    raw=Path(f'queries/{args.engine}/{args.query}.{suffix}').read_bytes()
    digest=lambda b:hashlib.sha256(b).hexdigest()
    require(validation['query_sha256'][args.query] in (digest(raw),digest(raw.replace(b'\r\n',b'\n'))),'La consulta cambio desde la fase 4.')
    return dict(engine=args.engine,query=args.query,variant=variant,profile=profile,
                query_id=args.query if args.query=='Q1' else 'Q2_'+variant,
                expected_rows=len(reference['q1']) if args.query=='Q1' else reference['q2']['limit'],
                source_records=manifest['records'],source_sha256=manifest['sha256'],query_sha256=digest(raw),
                marker=raw.decode('utf-8').strip().splitlines()[0],
                tag='bench_'+digest((args.run_id or 'check').encode())[:12])


def check(config):
    docker=Docker()
    info=docker.find(config['engine'])
    require(info['State']['Running'],'El motor no esta ejecutandose.')
    other=docker.find('postgresql' if config['engine']=='mongodb' else 'mongodb',optional=True)
    require(other is None or not other['State']['Running'],'Deten el otro motor antes de medir.')
    require(info['Config']['Labels'].get('benchmark.profile')==config['profile'],'La etiqueta del motor no coincide con el perfil.')
    group=Group(info,config['profile'])
    prometheus=Prometheus(info,group)
    prometheus.check()
    session=Session(config,-1)
    try:
        if config['engine']=='postgresql':
            settings={}
            for key in ('server_version','work_mem','shared_buffers','max_parallel_workers_per_gather','jit','log_temp_files','log_min_duration_statement','log_line_prefix'):
                session.cursor.execute('SHOW '+key)
                settings[key]=session.cursor.fetchone()[0]
            require(settings['log_temp_files']=='0' and settings['log_min_duration_statement']=='0' and '%a' in settings['log_line_prefix'],'Falta aplicar compose.runner.yml para los logs.')
        else:
            settings=session.db.command({'profile':-1})
            require(settings['was']==0 and settings['slowms']==0 and settings.get('sampleRate',1)==1,'MongoDB requiere profiler desactivado, slowms=0 y sampleRate=1 para estos logs.')
            settings={k:settings[k] for k in ('was','slowms','sampleRate') if k in settings}
            settings['server_version']=session.client.server_info()['version']
    finally:
        session.close()
    return docker,info,group,prometheus,dict(container=public_info(info),cgroup_path=group.relative,settings=settings)


def run(args,config):
    require(args.run_id is not None and re.fullmatch(r'[A-Za-z0-9_.-]{1,96}',args.run_id),'Identificador de corrida invalido.')
    require(args.cache_record is not None,'Falta el registro de limpieza de cache Linux.')
    cache=read(args.cache_record)
    require(cache.get('status')=='ok' and cache.get('run_id')==args.run_id and cache.get('engine')==config['engine'] and cache.get('profile')==config['profile'],'Registro de cache incorrecto.')
    docker,info,group,prometheus,checks=check(config)
    folder=Path('evidence/runs')/args.run_id
    folder.mkdir(parents=True,exist_ok=False)
    save(folder/'cache_flush.json',cache)
    report=dict(run_id=args.run_id,configuration=config,checks=checks,
                protocol=dict(warmup_seconds=60,measurement_seconds=300,cooldown_seconds=60),
                issues=[],notes=[],oom_killed=False)
    row=dict.fromkeys(FIELDS)
    row.update(timestamp_epoch=time.time(),engine=config['engine'],profile=config['profile'],query_id=config['query_id'],concurrency=args.concurrency,run_number=args.run_number,status='error')
    save(folder/'metadata.json',report)
    pool,measured=None,None
    observations=[]
    try:
        observations.append(group.snapshot())
        pool=Clients(config,args.concurrency)
        warmup=pool.phase('warmup',60)
        warmup.pop('samples')
        warmup.pop('cgroup_samples')
        report['warmup']=warmup
        require(not warmup['errors'],'Fallo durante el calentamiento: '+str(warmup['errors']))
        measured=pool.phase('measurement',300,group)
        samples=measured.pop('samples')
        points=measured.pop('cgroup_samples')
        compressed(folder/'latencies.json.gz',samples)
        compressed(folder/'cgroup_samples.json.gz',points)
        observations.extend(points)
        report['measurement']=measured
        summary=summarize(samples,300)
        report['summary']=summary
        row['timestamp_epoch']=measured['start_epoch']
        for key in ('throughput_qps','latency_p50_ms','latency_p95_ms','latency_p99_ms'):
            row[key]=summary[key]
        require(samples,'No se registraron operaciones de medicion.')
        report['issues'].extend(measured['errors'])
        try:
            row['cpu_throttled_seconds']=counter_delta(measured['cgroup_start'],measured['cgroup_end'],'throttled_usec')
            report['cpu_usage_seconds']=counter_delta(measured['cgroup_start'],measured['cgroup_end'],'usage_usec')
        except Exception as exc:
            report['issues'].append(str(exc))
    except Exception as exc:
        report['issues'].append(f'{type(exc).__name__}: {exc}')
    finally:
        if pool is not None:
            pool.close()
    cooldown_end=time.monotonic()+60
    print('cooldown: 60 segundos sin carga',flush=True)
    if measured is not None:
        try:
            time.sleep(2)  # Deja terminar la ingesta del ultimo scrape dentro del reposo.
            telemetry=prometheus.export(measured['start_epoch'],measured['end_epoch'])
            compressed(folder/'prometheus_samples.json.gz',telemetry)
            row['max_rss_mb'],report['rss_sampling']=rss_summary(telemetry,measured['start_epoch'],measured['end_epoch'])
            report['notes'].append('RSS es el maximo observado; revisar numero de muestras, extremos y huecos. No se interpolan valores.')
        except Exception as exc:
            report['issues'].append(f'Telemetria: {exc}')
        try:
            logs=docker.logs(info['Id'],measured['start_epoch'],time.time())
            with gzip.open(folder/'engine.log.gz','wt',encoding='utf-8') as stream:
                stream.write(logs)
            spill=detect_spill(config['engine'],logs,config['tag'],report.get('summary',{}).get('successful',0),config['marker'])
            # Las lineas originales estan en el log; no se duplican en metadata.json.
            evidence_count=len(spill.pop('evidence'))
            report['spill']=dict(**spill,evidence_events=evidence_count)
            row['disk_spill']=None if spill['value'] is None else str(spill['value']).lower()
            require(spill['value'] is not None,'Logs insuficientes para determinar spill.')
        except Exception as exc:
            report['issues'].append(f'Spill: {exc}')
    try:
        observations.append(group.snapshot())
    except Exception as exc:
        report['notes'].append(f'Lectura final de cgroup no disponible: {exc}')
    oom_counts=[o['memory_events']['oom_kill'] for o in observations]
    report['oom_counters']=dict(first=oom_counts[0],last=oom_counts[-1]) if oom_counts else None
    try:
        final=docker.get(f"/containers/{info['Id']}/json")
        report['container_after']=public_info(final)
        report['oom_killed']=bool(final['State'].get('OOMKilled')) or bool(oom_counts and max(oom_counts)>oom_counts[0])
        if final['RestartCount']!=info['RestartCount']:
            report['issues'].append('El motor se reinicio durante la corrida.')
    except Exception as exc:
        report['issues'].append(f'Estado final Docker: {exc}')
        report['oom_killed']=bool(oom_counts and max(oom_counts)>oom_counts[0])
    spill_value=True if row['disk_spill']=='true' else False if row['disk_spill']=='false' else None
    row['status']=status(report['oom_killed'],report['issues'],spill_value)
    time.sleep(max(0,cooldown_end-time.monotonic()))
    report['row']=row
    report['completed_at_utc']=datetime.now(timezone.utc).isoformat()
    save(folder/'metadata.json',report)
    append_csv(args.csv,row)
    print(json.dumps(dict(csv=str(args.csv),evidence=str(folder),row=row,issues=report['issues'],rss_sampling=report.get('rss_sampling')),indent=2))
    # El estado del experimento esta en la fila. No reintenta una corrida error/oom.


def main():
    parser=argparse.ArgumentParser(description='Ejecutor 60/300/60 del manual.')
    parser.add_argument('mode',choices=('check','run'))
    parser.add_argument('--engine',choices=('mongodb','postgresql'),required=True)
    parser.add_argument('--query',choices=('Q1','Q2'),default='Q1')
    parser.add_argument('--variant')
    parser.add_argument('--concurrency',type=int,choices=(1,10,50),default=1)
    parser.add_argument('--run-number',type=int,choices=(1,2,3),default=1)
    parser.add_argument('--run-id')
    parser.add_argument('--cache-record',type=Path)
    parser.add_argument('--csv',type=Path,default=Path('results/measurements.csv'))
    args=parser.parse_args()
    config=configuration(args)
    if args.mode=='check':
        *_,details=check(config)
        print(json.dumps(dict(status='ok',configuration=config,checks=details),indent=2))
    else:
        run(args,config)


if __name__=='__main__':
    main()
