"""Prueba la migracion del ejecutor sin motores, Docker ni carga real."""

import contextlib
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from benchmark_runner import __main__ as app
from benchmark_runner.load import Session
from benchmark_runner.observe import detect_spill


@contextlib.contextmanager
def project_fixture():
    original = Path.cwd()
    with tempfile.TemporaryDirectory() as folder:
        os.chdir(folder)
        try:
            counts = dict(records=500000, packages=1500000,
                          q1_matching_packages=488134, q2_matching_shipments=160953)
            interval = dict(inclusive_start='2026-01-01T00:00:00Z', exclusive_end='2026-10-01T00:00:00Z')
            manifest = dict(case='paqueteria', model_version='paqueteria-v1', dataset='data/envios.jsonl',
                            seed=2026, average_packages_per_shipment=3, max_nested_container_depth=3,
                            q2_interval=interval, sha256='b'*64, **counts)
            reference = dict(case='paqueteria', model_version='paqueteria-v1',
                             reference_schema='paqueteria-q1-q2-v1', source_sha256='b'*64,
                             source_records=500000, counts=counts, q2_interval=interval,
                             q1=[{'centro_id': f'CD-{n:02d}'} for n in range(12)], q2={'limit': 1000})

            def save(path, value):
                path = Path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value)+'\n', encoding='utf-8')

            save(app.RESULTS/'dataset_manifest.json', manifest)
            save(app.RESULTS/'query_reference.json', reference)
            for path in ('benchmark_runner/__main__.py','benchmark_runner/load.py',
                         'benchmark_runner/metrics.py','benchmark_runner/observe.py','compose.runner.yml'):
                target=Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('fixture\n', encoding='utf-8')
            for engine in ('postgresql','mongodb'):
                extension='sql' if engine=='postgresql' else 'json'
                for query in ('Q1','Q2'):
                    path=Path(f'queries/{engine}/{query}.{extension}')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('WITH paquetes_entregados AS (\nSELECT 1);\n' if extension=='sql' else '[]\n', encoding='utf-8')
                target={'table':'public.envios'} if engine=='postgresql' else {'collection':'envios'}
                common=dict(case='paqueteria',model_version='paqueteria-v1',engine=engine,status='ok',
                            source_sha256='b'*64,**target)
                loaded=dict(common,counts=counts)
                cases=([dict(query_id='Q1',variant='default',status='ok')]+[
                    dict(query_id='Q2',variant=variant,status='ok') for variant in
                    (('work_mem_4MB','work_mem_64MB') if engine=='postgresql' else ('allowDiskUse_false','allowDiskUse_true'))])
                validation=dict(common,profile='PROFILE_A_BASELINE',server_version='18.6' if engine=='postgresql' else '9.0.4',
                                reference_sha256=app.canonical_hash(app.RESULTS/'query_reference.json'),cases=cases,
                                query_sha256={q:app.canonical_hash(f'queries/{engine}/{q}.{extension}') for q in ('Q1','Q2')})
                save(app.RESULTS/f'load_{engine}.json',loaded)
                save(app.RESULTS/f'validation_{engine}_PROFILE_A_BASELINE.json',validation)
            args=SimpleNamespace(engine='postgresql',query='Q1',variant=None,run_id='parcel_test')
            with patch.dict(os.environ, {'PROFILE_ID':'PROFILE_A_BASELINE'}):
                yield args, save
        finally:
            os.chdir(original)


class ProvenanceTests(unittest.TestCase):
    def test_configuration_uses_parcel_source_and_twelve_groups(self):
        with project_fixture() as (args, _):
            config=app.configuration(args)
            self.assertEqual(config['case'],'paqueteria')
            self.assertEqual(config['expected_rows'],12)
            self.assertEqual(config['source_packages'],1500000)
            self.assertEqual(config['source_sha256'],'b'*64)
            self.assertEqual(config['marker'],'WITH paquetes_entregados AS (')
            self.assertTrue(config['tag'].startswith('bench_parcel_'))
            self.assertEqual(len(config['method_sha256']),64)

    def test_q2_preserves_each_engine_variant(self):
        with project_fixture() as (args, _):
            for engine, variant in (('mongodb','allowDiskUse_false'),('mongodb','allowDiskUse_true'),
                                    ('postgresql','work_mem_4MB'),('postgresql','work_mem_64MB')):
                args.engine,args.query,args.variant=engine,'Q2',variant
                config=app.configuration(args)
                self.assertEqual(config['expected_rows'],1000)
                self.assertEqual(config['query_id'],'Q2_'+variant)

    def test_old_case_is_rejected_even_when_source_hash_matches(self):
        with project_fixture() as (args, save):
            path=app.RESULTS/'load_postgresql.json'
            report=app.read(path)
            report['case']='orders'
            save(path,report)
            with self.assertRaisesRegex(RuntimeError,'otro caso'):
                app.configuration(args)

    def test_different_counts_or_destination_are_rejected(self):
        for change in ('count','target'):
            with self.subTest(change=change), project_fixture() as (args, save):
                path=app.RESULTS/'load_postgresql.json'
                report=app.read(path)
                if change=='count': report['counts']['packages']-=1
                else: report['table']='public.orders'
                save(path,report)
                with self.assertRaises(RuntimeError): app.configuration(args)

    def test_changed_query_is_rejected(self):
        with project_fixture() as (args, _):
            Path('queries/postgresql/Q1.sql').write_text('SELECT changed;\n',encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError,'consulta cambio'):
                app.configuration(args)

    def test_changed_reference_is_rejected(self):
        with project_fixture() as (args, save):
            path=app.RESULTS/'query_reference.json'
            reference=app.read(path)
            reference['q1'].append({'centro_id':'unexpected'})
            save(path,reference)
            with self.assertRaisesRegex(RuntimeError,'referencia cambio'):
                app.configuration(args)

    def test_line_ending_changes_do_not_invalidate_provenance(self):
        with project_fixture() as (args, _):
            first=app.configuration(args)
            for path in (Path('queries/postgresql/Q1.sql'),app.RESULTS/'query_reference.json',Path('compose.runner.yml')):
                path.write_bytes(path.read_bytes().replace(b'\n',b'\r\n'))
            second=app.configuration(args)
            self.assertEqual(first['query_sha256'],second['query_sha256'])
            self.assertEqual(first['reference_sha256'],second['reference_sha256'])
            self.assertEqual(first['method_sha256'],second['method_sha256'])
            lf=b'line\n'
            Path('reverse.txt').write_bytes(lf)
            self.assertTrue(app.matches_hash('reverse.txt',hashlib.sha256(b'line\r\n').hexdigest()))

    def test_unvalidated_variant_is_rejected(self):
        with project_fixture() as (args, save):
            args.query,args.variant='Q2','work_mem_64MB'
            path=app.RESULTS/'validation_postgresql_PROFILE_A_BASELINE.json'
            report=app.read(path)
            report['cases']=[c for c in report['cases'] if c['variant']!='work_mem_64MB']
            save(path,report)
            with self.assertRaisesRegex(RuntimeError,'no validada'):
                app.configuration(args)


class ExecutionIsolationTests(unittest.TestCase):
    def test_mongo_queries_envios_and_consumes_entire_cursor(self):
        session=Session.__new__(Session)
        session.config=dict(engine='mongodb',query='Q2',variant='allowDiskUse_false')
        session.tag='parcel:measurement'
        session.query=[{'$limit':1000}]
        session.db=MagicMock()
        cursor=session.db.__getitem__.return_value.aggregate.return_value.__enter__.return_value
        cursor.__iter__.return_value=iter([{}, {}, {}])
        self.assertEqual(session.execute(),3)
        session.db.__getitem__.assert_called_once_with('envios')
        session.db.__getitem__.return_value.aggregate.assert_called_once_with(
            session.query,comment='parcel:measurement',allowDiskUse=False)

    def test_check_does_not_execute_experimental_query(self):
        info=dict(Id='id',Name='/pg',Image='sha',RestartCount=0,
                  Config={'Image':'postgres','Labels':{'benchmark.profile':'PROFILE_A_BASELINE'}},
                  HostConfig={},State={'Running':True})
        docker=MagicMock()
        docker.find.side_effect=[info,None]
        session=MagicMock()
        settings=dict(server_version='18.6',work_mem='4MB',shared_buffers='128MB',
                      max_parallel_workers_per_gather='2',jit='on',log_temp_files='0',
                      log_min_duration_statement='0',log_line_prefix='%m [%p] %a ')
        requested=[]

        def execute(sql):
            requested.append(sql)
            session.cursor.fetchone.return_value=(settings[sql.removeprefix('SHOW ')],)

        session.cursor.execute.side_effect=execute
        config=dict(engine='postgresql',profile='PROFILE_A_BASELINE',validated_server_version='18.6')
        with patch.object(app,'Docker',return_value=docker), patch.object(app,'Group'), \
             patch.object(app,'Prometheus'), patch.object(app,'Session',return_value=session):
            app.check(config)
        self.assertTrue(all(sql.startswith('SHOW ') for sql in requested))
        self.assertEqual(len(requested),8)
        session.execute.assert_not_called()
        session.close.assert_called_once()

    def test_old_case_cache_record_stops_before_engine_check(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cache.json'
            path.write_text(json.dumps(dict(case='orders',source_sha256='b'*64)),encoding='utf-8')
            args=SimpleNamespace(run_id='test',cache_record=path,csv=app.RESULTS/'test.csv')
            config=dict(source_sha256='b'*64)
            with patch.object(app,'check') as check:
                with self.assertRaisesRegex(RuntimeError,'otro caso'):
                    app.run(args,config)
                check.assert_not_called()

    def test_csv_outside_parcel_directory_is_rejected_before_engine_check(self):
        args=SimpleNamespace(run_id='test',cache_record=Path('unused'),csv=Path('results/old.csv'))
        with patch.object(app,'check') as check:
            with self.assertRaisesRegex(RuntimeError,'results/paqueteria'):
                app.run(args,{})
            check.assert_not_called()


class SpillIsolationTests(unittest.TestCase):
    def test_orders_logs_are_not_used_as_parcel_evidence(self):
        record={'msg':'Slow query','attr':{'command':{'aggregate':'orders','comment':'tag:measurement'},'usedDisk':True}}
        result=detect_spill('mongodb',json.dumps(record),'tag',1,'')
        self.assertIsNone(result['value'])
        self.assertEqual(result['completed_query_logs'],0)

    def test_getmore_spill_uses_envios_and_inherited_comment(self):
        record={'msg':'Slow query','attr':{
            'command':{'getMore':123,'collection':'envios'},
            'originatingCommand':{'aggregate':'envios','comment':'tag:measurement'},
            'usedDisk':True}}
        result=detect_spill('mongodb',json.dumps(record),'tag',1,'')
        self.assertTrue(result['value'])
        self.assertEqual(result['completed_query_logs'],0)

    def test_complete_aggregate_log_without_spill_is_false(self):
        record={'msg':'Slow query','attr':{'command':{'aggregate':'envios','cursor':{'comment':'tag:measurement'}}}}
        result=detect_spill('mongodb',json.dumps(record),'tag',1,'')
        self.assertFalse(result['value'])
        self.assertEqual(result['completed_query_logs'],1)

    def test_postgres_cte_marker_counts_completion_without_warmup_spill(self):
        marker='WITH paquetes_entregados AS ('
        logs=('t tag:warmup:w0 LOG: temporary file: path tmp\n'
              't tag:measurement:w0 LOG: duration: 123 ms statement: '+marker+'\n SELECT 1);\n')
        result=detect_spill('postgresql',logs,'tag',1,marker)
        self.assertFalse(result['value'])
        self.assertEqual(result['completed_query_logs'],1)


if __name__=='__main__':
    unittest.main()
