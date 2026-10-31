"""Pruebas de plan/reanudacion con archivos simulados, sin motores ni consultas."""
import contextlib
import copy
import csv
import gzip
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('matrix',ROOT/'scripts/matrix.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
PILOT='paqueteria_pilot_simulado'


def dump(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf-8')


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='matriz con espacios ')
        self.old=Path.cwd();os.chdir(self.temp.name)
        for name in ('docker-compose.yml','compose.runner.yml','requirements.txt','run_benchmarks.sh','scripts/matrix.py'):
            target=Path(name);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
        for folder in ('benchmark_runner','queries','config/profiles'):
            for source in (ROOT/folder).rglob('*'):
                if source.is_file() and '__pycache__' not in source.parts:
                    target=Path(source.relative_to(ROOT));target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        Path('.env').write_text((ROOT/'.env.example').read_text()+'\nMONGO_PASSWORD=private\n')
        counts=dict(records=500000,packages=1500000,q1_matching_packages=488134,q2_matching_shipments=160953)
        interval=dict(inclusive_start='2026-01-01T00:00:00Z',exclusive_end='2026-10-01T00:00:00Z')
        common=dict(case='paqueteria',model_version='paqueteria-v1',source_sha256='d'*64)
        dump(m.ROOT/'dataset_manifest.json',dict(case='paqueteria',model_version='paqueteria-v1',
             dataset='data/envios.jsonl',seed=2026,average_packages_per_shipment=3,max_nested_container_depth=3,
             q2_interval=interval,sha256='d'*64,**counts))
        dump(m.ROOT/'query_reference.json',dict(common,reference_schema='paqueteria-q1-q2-v1',source_records=500000,
             counts=counts,q2_interval=interval,q1=[{} for _ in range(12)],q2={'limit':1000}))
        for engine in m.VARIANTS:
            target={'table':'public.envios'} if engine=='postgresql' else {'collection':'envios'}
            dump(m.ROOT/f'load_{engine}.json',dict(common,engine=engine,status='ok',counts=counts,**target))
            hashes={q:m.digest(f'queries/{engine}/{q}.'+('json' if engine=='mongodb' else 'sql')) for q in ('Q1','Q2')}
            cases=[dict(query_id='Q1',variant='default',status='ok')]+[dict(query_id='Q2',variant=v,status='ok') for v in m.VARIANTS[engine]]
            dump(m.ROOT/f'validation_{engine}_PROFILE_A_BASELINE.json',dict(common,engine=engine,status='ok',
                 profile='PROFILE_A_BASELINE',server_version='18.6' if engine=='postgresql' else '9.0.4',
                 reference_sha256=m.text_digest(m.ROOT/'query_reference.json'),query_sha256=hashes,cases=cases,**target))
        rows=[];runs=[]
        for job in m.jobs():
            if (job['engine'],job['profile'],job['query'],job['concurrency'])==('postgresql','PROFILE_A_BASELINE','Q1',1):
                job['run_id']=f"{PILOT}_r{job['repetition']}"
                rows.append(self.make_result(job)['row'])
                runs.append(dict(run_id=job['run_id'],run_number=job['repetition'],status='spill',metadata=str(m.RUNS/job['run_id']/'metadata.json')))
        with (m.ROOT/f'{PILOT}.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=m.FIELDS);writer.writeheader();writer.writerows(rows)
        current=m.inputs()
        dump(m.PILOT_REGISTRY,dict(case='paqueteria',model_version='paqueteria-v1',status='completed',batch_id=PILOT,
             csv=str(m.ROOT/f'{PILOT}.csv'),engine='postgresql',profile='PROFILE_A_BASELINE',query='Q1',variant='default',
             concurrency=1,planned_repetitions=3,protocol=m.PROTOCOL,runs=runs,
             source_sha256=current['source_sha256'],method_sha256=current['method_sha256'],
             reference_sha256=current['reference_sha256'],query_sha256=current['query_sha256']['postgresql/Q1']))

    def tearDown(self):
        os.chdir(self.old);self.temp.cleanup()

    def make_result(self,job,state='spill'):
        folder=m.RUNS/job['run_id'];folder.mkdir(parents=True,exist_ok=True)
        sample=dict(ok=True,start_s=1,end_s=2,latency_ms=1000)
        summary=m.summarize([sample],300)
        cpu,memory=m.PROFILES[job['profile']]
        row=dict.fromkeys(m.FIELDS)
        row.update(timestamp_epoch=0,engine=job['engine'],profile=job['profile'],query_id='Q1' if job['query']=='Q1' else 'Q2_'+job['variant'],concurrency=job['concurrency'],run_number=job['repetition'],throughput_qps=summary['throughput_qps'],latency_p50_ms=1000,latency_p95_ms=1000,latency_p99_ms=1000,cpu_throttled_seconds=.01,max_rss_mb=1,disk_spill='true',status=state)
        with patch.dict(os.environ,{'PROFILE_ID':job['profile']}):
            c=m.configuration(SimpleNamespace(engine=job['engine'],query=job['query'],variant=job['variant'],run_id=job['run_id']))
        cache=dict(case='paqueteria',source_sha256='d'*64,status='ok',run_id=job['run_id'],engine=job['engine'],profile=job['profile'])
        telemetry={'container_memory_rss':{'result':[{'values':[[1,'1048576'],[2,'1048576']]}]}}
        maximum,sampling=m.rss_summary(telemetry,0,300)
        data=dict(case='paqueteria',model_version='paqueteria-v1',run_id=job['run_id'],completed_at_utc=m.now(),protocol=m.PROTOCOL,configuration=c,row=row,
                  checks={'container':{'image':'postgres:18.6' if job['engine']=='postgresql' else 'mongo:9.0.4',
                         'limits':dict(NanoCpus=cpu*1_000_000_000,Memory=memory,MemorySwap=memory)},
                         'settings':{'server_version':'18.6' if job['engine']=='postgresql' else '9.0.4'}},rss_sampling=sampling,
                  issues=['fallo registrado'] if state=='error' else [],oom_killed=state=='oom',warmup={'errors':[]},
                  summary=summary,measurement=dict(start_epoch=0,end_epoch=300,errors=[],cgroup_start={'cpu':{'throttled_usec':0}},cgroup_end={'cpu':{'throttled_usec':10000}}))
        dump(folder/'metadata.json',data);dump(folder/'cache_flush.json',cache)
        for name in ('latencies.json.gz','cgroup_samples.json.gz','prometheus_samples.json.gz'):
            with gzip.open(folder/name,'wt') as f:json.dump([sample] if name=='latencies.json.gz' else telemetry if name=='prometheus_samples.json.gz' else {},f)
        logs=(f"t {c['tag']}:measurement:w0 LOG: duration: 1 ms statement: {c['marker']}\n"
              f"t {c['tag']}:measurement:w0 LOG: temporary file: path tmp\n") if job['engine']=='postgresql' else json.dumps(
              {'msg':'Slow query','attr':{'command':{'aggregate':'envios','comment':c['tag']+':measurement'},'usedDisk':True}})
        with gzip.open(folder/'engine.log.gz','wt') as f:f.write(logs)
        return data

    def test_matrix_cardinality_and_unique_keys(self):
        jobs=m.jobs();self.assertEqual(len(jobs),162);self.assertEqual(len({j['key'] for j in jobs}),162)
        self.assertEqual(len({j['run_id'] for j in jobs}),162)
        self.assertEqual(sum(j['query']=='Q1' for j in jobs),54);self.assertEqual(sum(j['query']=='Q2' for j in jobs),108)
        self.assertTrue(all(len(j['run_id'])<=96 for j in jobs))

    def test_import_pilot_once_and_159_pending(self):
        data=m.fresh();done=[j for j in data['jobs'] if j['result']]
        self.assertEqual(len(done),3);self.assertTrue(all(j['result']['origin']=='pilot' for j in done))
        self.assertEqual(sum(j['result'] is None for j in data['jobs']),159)
        self.assertNotIn('private',json.dumps(data))

    def test_plan_has_no_writes(self):
        originals={str(p):p.read_bytes() for p in Path('.').rglob('*') if p.is_file()}
        args=SimpleNamespace(engine=None,profile=None,query=None,variant=None,concurrency=None)
        with contextlib.redirect_stdout(io.StringIO()) as output:m.show(m.refresh(m.load()),args)
        self.assertEqual(json.loads(output.getvalue())['pending'],159)
        self.assertFalse(m.MANIFEST.exists());self.assertFalse(m.CSV.exists())
        self.assertEqual(originals,{str(p):p.read_bytes() for p in Path('.').rglob('*') if p.is_file()})

    def test_skip_recorded_pilot_under_filter(self):
        args=SimpleNamespace(engine='postgresql',profile='PROFILE_A_BASELINE',query='Q1',variant=None,concurrency=1)
        selected=m.selected(m.fresh(),args)
        self.assertEqual(len(selected),3);self.assertTrue(all(j['result'] for j in selected))

    def test_next_block_has_three_pending(self):
        args=SimpleNamespace(engine='postgresql',profile='PROFILE_A_BASELINE',query='Q1',variant=None,concurrency=10)
        selected=m.selected(m.fresh(),args)
        self.assertEqual(len(selected),3);self.assertTrue(all(j['result'] is None for j in selected))

    def test_recovery_completed_metadata_before_csv_append(self):
        data=m.fresh();m.persist(data)
        job=next(j for j in data['jobs'] if j['result'] is None)
        self.make_result(job)
        resumed=m.refresh(m.load());self.assertEqual(sum(bool(j['result']) for j in resumed['jobs']),4)
        m.persist(resumed)
        with m.CSV.open() as f:
            reader=csv.DictReader(f);rows=list(reader);self.assertEqual(reader.fieldnames,m.FIELDS)
        self.assertEqual(len(rows),4)
        m.persist(m.refresh(m.load()))
        with m.CSV.open() as f:self.assertEqual(len(list(csv.DictReader(f))),4)

    def test_partial_run_blocks_resume_without_deleting(self):
        data=m.fresh();job=next(j for j in data['jobs'] if j['result'] is None)
        folder=m.RUNS/job['run_id'];dump(folder/'metadata.json',{'run_id':job['run_id']})
        before=(folder/'metadata.json').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'incompleta'):m.refresh(data)
        self.assertEqual((folder/'metadata.json').read_bytes(),before)

    def test_error_and_oom_results_not_retried(self):
        data=m.fresh()
        for state in ('error','oom'):
            job=next(j for j in data['jobs'] if j['result'] is None);self.make_result(job,state);m.refresh(data)
            self.assertEqual(job['result']['row']['status'],state)
        self.assertEqual(sum(bool(j['result']) for j in data['jobs']),5)

    def test_changed_code_blocks_mixing_methods(self):
        m.persist(m.fresh());p=Path('benchmark_runner/metrics.py');p.write_text(p.read_text()+'\n# cambio\n')
        with self.assertRaisesRegex(RuntimeError,'Cambio'):m.load()

    def test_changed_source_metadata_is_detected(self):
        data=m.fresh();m.persist(data);job=next(j for j in data['jobs'] if j['result'])
        p=Path(job['result']['folder'])/'metadata.json';value=m.read(p);value['row']['max_rss_mb']=999;dump(p,value)
        with self.assertRaisesRegex(RuntimeError,'Cambio'):m.load()

    def test_changed_query_is_detected(self):
        p=Path('queries/postgresql/Q1.sql');p.write_text('SELECT 1;')
        with self.assertRaisesRegex(RuntimeError,'consulta'):m.fresh()

    def test_all_original_pilot_files_remain_identical(self):
        original={p:p.read_bytes() for p in m.RUNS.rglob('*') if p.is_file()}
        csv_path=m.ROOT/f'{PILOT}.csv';csv_before=csv_path.read_bytes()
        m.persist(m.fresh());m.persist(m.refresh(m.load()))
        for p,b in original.items():self.assertEqual(p.read_bytes(),b)
        self.assertEqual(csv_path.read_bytes(),csv_before)

    def fake_commands(self):
        folder=Path('fakebin');folder.mkdir()
        # Todos los comandos Docker son simulados. Nunca se invoca un daemon.
        docker_code = """#!/usr/bin/env python3
import csv,json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0,ROOT_TESTS)
from test_matrix import MatrixTests,m
args=sys.argv[1:]
with Path('command_trace.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
if args[0]=='run':
    index=args.index('scripts/matrix.py')
    raise SystemExit(subprocess.call([sys.executable,'-B','scripts/matrix.py',*args[index+1:]]))
if args[0]=='image' and args[1]=='inspect':
    raise SystemExit(1 if os.environ.get('FAKE_RUNNER_IMAGE_MISSING') else 0)
if args[0]=='compose':
    envs=[args[i+1] for i,x in enumerate(args) if x=='--env-file']
    profile=Path(envs[-1]).stem
    cpus,memory=m.PROFILES[profile]
    assert os.environ['PROFILE_ID']==profile
    assert os.environ['COMPOSE_PROJECT_NAME']=='mongodb-postgresql-benchmark'
    assert os.environ['DB_CPUS']==str(cpus) and os.environ['DB_MEMORY']==str(memory//1024**2)+'m'
    assert 'POSTGRES_IMAGE' not in os.environ
    if 'benchmark-runner' in args and 'run' in args:
        cli=args[args.index('benchmark-runner')+1:]
        if cli[0]=='check':print('{"status":"ok"}');raise SystemExit(0)
        def value(flag):return cli[cli.index(flag)+1]
        plan=m.load();job=next(j for j in plan['jobs'] if j['run_id']==value('--run-id'))
        assert value('--engine')==job['engine'] and value('--variant')==job['variant']
        envs=[args[i+1] for i,x in enumerate(args) if x=='--env-file']
        assert envs[-1]=='config/profiles/'+job['profile']+'.env'
        assert '--interactive=false' in args
        result=MatrixTests().make_result(job)
        with Path(value('--csv')).open('a',newline='') as f:csv.DictWriter(f,fieldnames=m.FIELDS).writerow(result['row'])
    raise SystemExit(0)
raise SystemExit(99)
""".replace('ROOT_TESTS',repr(str(ROOT/'tests')))
        (folder/'docker').write_text(docker_code)
        (folder/'id').write_text('#!/bin/sh\necho 1000\n')
        (folder/'sudo').write_text('#!/bin/sh\necho CACHE_LINUX_OK\n')
        for p in folder.iterdir():p.chmod(0o755)
        env=os.environ.copy();env['PATH']=str(folder.resolve())+os.pathsep+env['PATH']
        return env

    def test_bash_plan_never_calls_compose(self):
        if not shutil.which('bash'):self.skipTest('bash no disponible')
        result=subprocess.run(['bash','run_benchmarks.sh','--plan'],env=self.fake_commands(),capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['pending'],159)
        trace=[json.loads(line) for line in Path('command_trace.jsonl').read_text().splitlines()]
        self.assertTrue(all(args[0]=='run' for args in trace))
        self.assertFalse(m.MANIFEST.exists())

    def test_bash_executes_block_once_and_resumes_without_new_queries(self):
        if not shutil.which('bash'):self.skipTest('bash no disponible')
        env=self.fake_commands()
        command=['bash','run_benchmarks.sh','--run','--engine','postgresql','--profile','PROFILE_A_BASELINE','--query','Q1','--concurrency','10']
        first=subprocess.run(command,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(first.returncode,0,first.stdout+first.stderr)
        trace=[json.loads(line) for line in Path('command_trace.jsonl').read_text().splitlines()]
        queries=[a for a in trace if a[0]=='compose' and 'benchmark-runner' in a and a[a.index('benchmark-runner')+1]=='run']
        self.assertEqual(len(queries),3)
        second=subprocess.run(command,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(second.returncode,0,second.stdout+second.stderr)
        trace=[json.loads(line) for line in Path('command_trace.jsonl').read_text().splitlines()]
        queries=[a for a in trace if a[0]=='compose' and 'benchmark-runner' in a and a[a.index('benchmark-runner')+1]=='run']
        self.assertEqual(len(queries),3)
        with m.CSV.open() as f:self.assertEqual(len(list(csv.DictReader(f))),6)
        self.assertFalse(Path('evidence/paqueteria/matrix_lock').exists())

    def test_bash_syntax(self):
        if not shutil.which('bash'):self.skipTest('bash no disponible')
        subprocess.run(['bash','-n','run_benchmarks.sh'],check=True)

    def test_bash_profile_b_overrides_inherited_profile_a(self):
        if not shutil.which('bash'):self.skipTest('bash no disponible')
        env=self.fake_commands()
        env.update(PROFILE_ID='PROFILE_A_BASELINE',DB_CPUS='4',DB_MEMORY='4096m',POSTGRES_IMAGE='wrong:0',COMPOSE_PROJECT_NAME='wrong-project')
        command=['bash','run_benchmarks.sh','--run','--engine','postgresql','--profile','PROFILE_B_MEDIUM','--query','Q2','--variant','work_mem_4MB','--concurrency','10']
        result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        data=m.load()
        rows=[j['result']['row'] for j in data['jobs'] if j['result'] and j['profile']=='PROFILE_B_MEDIUM']
        self.assertEqual(len(rows),3)
        self.assertTrue(all(row['query_id']=='Q2_work_mem_4MB' for row in rows))

    def test_bash_windows_branch_calls_wsl_without_path_conversion(self):
        if not shutil.which('bash'):self.skipTest('bash no disponible')
        env=self.fake_commands();folder=Path('fakebin')
        (folder/'uname').write_text('#!/bin/sh\necho MINGW64_NT-10.0\n')
        (folder/'cygpath').write_text('#!/bin/sh\nprintf "%s\\n" "$PWD"\n')
        (folder/'wsl.exe').write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
assert os.environ['MSYS_NO_PATHCONV']=='1'
assert os.environ['MSYS2_ARG_CONV_EXCL']=='*'
with Path('wsl_trace.jsonl').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
print('CACHE_LINUX_OK')
''')
        for name in ('uname','cygpath','wsl.exe'):(folder/name).chmod(0o755)
        command=['bash','run_benchmarks.sh','--run','--engine','postgresql','--profile','PROFILE_A_BASELINE','--query','Q1','--concurrency','10']
        result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        calls=[json.loads(line) for line in Path('wsl_trace.jsonl').read_text().splitlines()]
        self.assertEqual(len(calls),3)
        self.assertTrue(all(c[:7]==['-d','docker-desktop','-u','root','--','sh','-c'] for c in calls))
        self.assertTrue(all(c[-1]=='sync && echo 3 > /proc/sys/vm/drop_caches && echo CACHE_LINUX_OK' for c in calls))

    def test_audit_detects_modified_compressed_evidence(self):
        data=m.fresh();m.persist(data)
        job=next(j for j in data['jobs'] if j['result'])
        path=Path(job['result']['folder'])/'engine.log.gz'
        path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaisesRegex(RuntimeError,'evidencia'):m.audit(m.load())

    def test_missing_runner_image_stops_before_starting_services(self):
        if not shutil.which('bash'):self.skipTest('bash no disponible')
        env=self.fake_commands();env['FAKE_RUNNER_IMAGE_MISSING']='1'
        command=['bash','run_benchmarks.sh','--run','--engine','postgresql','--query','Q1','--concurrency','10']
        result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=30)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('imagen local',result.stderr)
        trace=[json.loads(line) for line in Path('command_trace.jsonl').read_text().splitlines()]
        self.assertFalse(any(args[0]=='compose' for args in trace))
        self.assertFalse(Path('evidence/paqueteria/matrix_lock').exists())

    def test_failed_pilot_with_unknown_metrics_is_kept_with_empty_csv_values(self):
        registry=m.read(m.PILOT_REGISTRY)
        entry=registry['runs'][0];entry['status']='error'
        path=Path(entry['metadata']);data=m.read(path)
        data['issues']=['Medicion interrumpida'];data['row']['status']='error'
        for key in ('throughput_qps','latency_p50_ms','latency_p95_ms','latency_p99_ms','cpu_throttled_seconds','max_rss_mb','disk_spill'):
            data['row'][key]=None
        dump(path,data);dump(m.PILOT_REGISTRY,registry)
        csv_path=Path(registry['csv'])
        with csv_path.open(newline='') as f:rows=list(csv.DictReader(f))
        rows[0]={key:m.csv_value(value) for key,value in data['row'].items()}
        with csv_path.open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=m.FIELDS);writer.writeheader();writer.writerows(rows)
        matrix=m.fresh();m.persist(matrix)
        self.assertEqual(sum(bool(j['result']) for j in matrix['jobs']),3)
        with m.CSV.open() as f:copied=list(csv.DictReader(f))
        self.assertEqual(copied[0]['status'],'error');self.assertEqual(copied[0]['max_rss_mb'],'')


if __name__=='__main__':unittest.main()
