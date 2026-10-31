import contextlib
import csv
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from benchmark_runner.load import Clients
from benchmark_runner.metrics import FIELDS, append_csv, counter_delta, coverage, percentile, rss_summary, status, summarize
from benchmark_runner.observe import decode_logs, detect_spill, public_info

class FakeSession:
    def __init__(self,config,worker):self.rows=config['expected_rows']
    def prepare(self,phase):pass
    def execute(self):
        import time
        time.sleep(.005)
        return self.rows
    def close(self):pass
class FailedSession(FakeSession):
    def execute(self):raise RuntimeError('Error simulado')

class Tests(unittest.TestCase):
    def test_percentile_interpolation(self):self.assertEqual(percentile([0,10],.95),9.5)
    def test_empty_and_single_percentile(self):
        self.assertIsNone(percentile([],.99));self.assertEqual(percentile([12],.99),12)
    def test_global_percentile(self):self.assertEqual(percentile([1,2,3,1000],.5),2.5)
    def test_tail_and_fixed_window_throughput(self):
        s=summarize([dict(ok=True,end_s=299,latency_ms=10),dict(ok=True,end_s=305,latency_ms=5000),dict(ok=False,end_s=299,latency_ms=30)],300)
        self.assertEqual(s['throughput_qps'],1/300);self.assertEqual(s['completed_after_window'],1);self.assertEqual(s['failed'],1);self.assertEqual(s['latency_p50_ms'],2505)
    def test_no_success_has_no_latency(self):
        s=summarize([dict(ok=False,end_s=5,latency_ms=4)],300)
        self.assertEqual(s['throughput_qps'],0);self.assertIsNone(s['latency_p95_ms'])
    def test_cpu_units_and_reset(self):
        a={'cpu':{'throttled_usec':1000000}};b={'cpu':{'throttled_usec':1500000}}
        self.assertEqual(counter_delta(a,b,'throttled_usec'),.5)
        with self.assertRaises(RuntimeError):counter_delta(b,a,'throttled_usec')
    def test_partial_rss_without_interpolation(self):
        data={'container_memory_rss':{'result':[{'values':[[1,'1048576'],[10,'2097152']]}]}}
        maximum,d=rss_summary(data,0,300)
        self.assertEqual(maximum,2);self.assertEqual(d['samples'],2);self.assertEqual(d['max_gap_seconds'],9)
        self.assertEqual(len(data['container_memory_rss']['result'][0]['values']),2)
    def test_absent_rss_is_not_zero(self):
        with self.assertRaises(RuntimeError):rss_summary({'container_memory_rss':{'result':[]}},0,300)
    def test_invalid_samples(self):
        for data in ([[1,'NaN']],[[1,'1'],[1,'1']]):
            with self.assertRaises(RuntimeError):coverage(data,0,300)
    def test_csv_schema_and_unknown_values(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.csv';row=dict.fromkeys(FIELDS);row['status']='error';append_csv(p,row)
            with p.open(newline='') as f:
                r=csv.DictReader(f);self.assertEqual(r.fieldnames,FIELDS);self.assertEqual(next(r)['max_rss_mb'],'')
            with self.assertRaises(RuntimeError):append_csv(p,{**row,'extra':0})
    def test_wrong_existing_csv_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.csv';p.write_text('wrong\n')
            with self.assertRaises(RuntimeError):append_csv(p,dict.fromkeys(FIELDS))
            self.assertEqual(p.read_text(),'wrong\n')
    def test_status_priority(self):
        for args,result in [((True,['x'],True),'oom'),((False,['x'],True),'error'),((False,[],True),'spill'),((False,[],False),'ok'),((False,[],None),'error')]:self.assertEqual(status(*args),result)
    def test_postgres_spill_tags(self):
        logs='t bench:warmup:w0 LOG: temporary file: path tmp\nt bench:measurement:w0 LOG: duration: 10 ms statement: SELECT data\n'
        self.assertFalse(detect_spill('postgresql',logs,'bench',1,'SELECT data')['value'])
        logs+='t bench:measurement:w0 LOG: temporary file: path tmp\n'
        self.assertTrue(detect_spill('postgresql',logs,'bench',1,'SELECT data')['value'])
    def test_missing_logs_are_unknown(self):
        self.assertIsNone(detect_spill('postgresql','','bench',1,'SELECT data')['value'])
        self.assertIsNone(detect_spill('postgresql','','bench',0,'SELECT data')['value'])
    def test_positive_spill_needs_no_negative_inference(self):
        self.assertTrue(detect_spill('postgresql','t bench:measurement:w0 LOG: temporary file: path tmp','bench',5,'SELECT data')['value'])
    def test_mongo_actual_useddisk_and_comment(self):
        r={'msg':'Slow query','attr':{'command':{'aggregate':'envios','comment':'bench:measurement'},'usedDisk':True}}
        self.assertTrue(detect_spill('mongodb',json.dumps(r),'bench',1,'')['value'])
        self.assertIsNone(detect_spill('mongodb',json.dumps(r),'other',1,'')['value'])
    def test_docker_frames(self):
        data=b'abc\n';raw=b'\x01\0\0\0'+struct.pack('>I',len(data))+data
        self.assertEqual(decode_logs(raw),'abc\n')
        with self.assertRaises(RuntimeError):decode_logs(raw[:-1])
    def test_secrets_not_exported(self):
        x={'Id':'id','Name':'/n','Image':'sha','Config':{'Image':'pg','Env':['PASSWORD=private']},'State':{'Running':True},'HostConfig':{},'RestartCount':0}
        self.assertNotIn('private',json.dumps(public_info(x)))
    def test_multiprocess_barriers(self):
        pool=Clients({'expected_rows':36},2,FakeSession)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                warm=pool.phase('warmup',.08);m=pool.phase('measurement',.08)
            self.assertFalse(warm['errors']);self.assertFalse(m['errors']);self.assertGreater(len(m['samples']),0)
            self.assertTrue(all(s['ok'] and 0<=s['start_s']<.08 for s in m['samples']))
        finally:pool.close()
    def test_worker_error_is_preserved(self):
        pool=Clients({'expected_rows':36},1,FailedSession)
        try:
            with contextlib.redirect_stdout(io.StringIO()):m=pool.phase('measurement',.04)
            self.assertTrue(m['errors']);self.assertEqual(m['totals']['failed'],1);self.assertFalse(m['samples'][0]['ok'])
        finally:pool.close()


# Verificacion del guardado de una corrida completa sin conexiones ni carga real.
class PersistenceTests(unittest.TestCase):
    def exercise(self, missing_rss=False, query_error=False, oom=False):
        import copy
        import gzip
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        from benchmark_runner import __main__ as app
        config=dict(case='paqueteria',model_version='paqueteria-v1',source_sha256='test-source',engine='postgresql',query='Q1',query_id='Q1',profile='PROFILE_A_BASELINE',tag='bench',marker='SELECT data')
        snapshot=dict(epoch=0,cpu={'usage_usec':2000000,'throttled_usec':100000},memory_events={'oom_kill':0},memory_current=1)
        info=dict(Id='id',Name='/pg',Image='sha',Config={'Image':'pg'},State={'Running':True,'OOMKilled':oom},HostConfig={},RestartCount=0)
        class G:
            def snapshot(self):return copy.deepcopy(snapshot)
        class D:
            def get(self,*a):return copy.deepcopy(info)
            def logs(self,*a):return 't bench:measurement:w0 LOG: duration: 4 ms statement: SELECT data\nt bench:measurement:w0 LOG: temporary file: path tmp\n'
        class P:
            def export(self,*a):
                return {'container_memory_rss':{'result':[] if missing_rss else [{'values':[[i,'1048576'] for i in range(1,270)]}]}}
        class C:
            def __init__(self,*a):pass
            def phase(self,phase,*a):
                before=copy.deepcopy(snapshot);after=copy.deepcopy(snapshot);after['cpu']['usage_usec']+=1000000;after['cpu']['throttled_usec']+=10000
                return dict(start_epoch=0,end_epoch=60 if phase=='warmup' else 300,drain_seconds=1,
                            errors=['error de consulta'] if phase=='measurement' and query_error else [],
                            samples=[] if phase=='warmup' else [dict(start_s=1,end_s=2,latency_ms=1000,ok=not query_error,error='fallo' if query_error else None)],
                            cgroup_samples=[before,after],cgroup_start=before,cgroup_end=after)
            def close(self):pass
        with tempfile.TemporaryDirectory() as directory:
            old=os.getcwd()
            try:
                os.chdir(directory)
                Path('cache.json').write_text(json.dumps(dict(case='paqueteria',source_sha256='test-source',status='ok',run_id='sample',engine='postgresql',profile='PROFILE_A_BASELINE')))
                args=SimpleNamespace(run_id='sample',cache_record=Path('cache.json'),concurrency=1,run_number=1,csv=Path('results/paqueteria/test.csv'))
                with patch.object(app,'check',return_value=(D(),info,G(),P(),{})),patch.object(app,'Clients',C),patch.object(app.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):app.run(args,config)
                meta=json.loads(Path('evidence/paqueteria/runs/sample/metadata.json').read_text())
                with Path('results/paqueteria/test.csv').open() as stream:rows=list(csv.DictReader(stream))
                self.assertEqual(len(rows),1);self.assertEqual(list(rows[0]),FIELDS)
                self.assertTrue(Path('evidence/paqueteria/runs/sample/engine.log.gz').is_file())
                return meta,rows[0]
            finally:os.chdir(old)
    def test_partial_telemetry_records_one_spill_row(self):
        meta,row=self.exercise()
        self.assertEqual(row['status'],'spill');self.assertEqual(row['max_rss_mb'],'1.0');self.assertEqual(meta['rss_sampling']['samples'],269)
    def test_missing_metric_stays_blank_and_error(self):
        meta,row=self.exercise(missing_rss=True)
        self.assertEqual(row['status'],'error');self.assertEqual(row['max_rss_mb'],'');self.assertTrue(meta['issues'])
    def test_query_error_is_not_hidden_by_spill(self):
        meta,row=self.exercise(query_error=True)
        self.assertEqual(row['status'],'error');self.assertTrue(meta['issues'])
    def test_oom_is_not_reclassified_as_spill(self):
        meta,row=self.exercise(oom=True)
        self.assertEqual(row['status'],'oom');self.assertTrue(meta['oom_killed'])

if __name__=='__main__':unittest.main()
