import http.client
import json
import math
import os
import re
import socket
import struct
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from .metrics import PROFILES, require


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def decode_logs(raw):
    if not raw or raw[:4] not in (b'\x01\0\0\0', b'\x02\0\0\0'):
        return raw.decode('utf-8', errors='replace')
    chunks, i = [], 0
    while i < len(raw):
        require(i+8 <= len(raw), 'Cabecera Docker incompleta.')
        size = struct.unpack('>I', raw[i+4:i+8])[0]
        i += 8
        require(i+size <= len(raw), 'Log Docker incompleto.')
        chunks.append(raw[i:i+size])
        i += size
    return b''.join(chunks).decode('utf-8', errors='replace')


class Docker:
    def get(self, path, params=None, raw=False):
        connection = UnixHTTP('localhost', timeout=30)
        try:
            connection.request('GET', path + ('?'+urlencode(params) if params else ''))
            response = connection.getresponse()
            payload = response.read()
            require(response.status == 200, f'Docker GET {path}: HTTP {response.status}')
            return payload if raw else json.loads(payload)
        finally:
            connection.close()

    def find(self, service, optional=False):
        project = os.environ.get('BENCHMARK_PROJECT','mongodb-postgresql-benchmark')
        filters = json.dumps({'label': [f'com.docker.compose.project={project}',f'com.docker.compose.service={service}']})
        found = self.get('/containers/json', {'all':'true', 'filters':filters})
        if not found and optional:
            return None
        require(len(found)==1, f'Se esperaba un contenedor del servicio {service}.')
        return self.get(f"/containers/{found[0]['Id']}/json")

    def logs(self, cid, start, end):
        raw = self.get(f'/containers/{cid}/logs', dict(stdout='true', stderr='true', timestamps='true', since=math.floor(start), until=math.ceil(end)), raw=True)
        return decode_logs(raw)


def public_info(info):
    state = info['State']
    host = info['HostConfig']
    return dict(id=info['Id'], name=info['Name'].lstrip('/'), image=info['Config']['Image'], image_id=info['Image'],
                state={k:state.get(k) for k in ('Running','OOMKilled','StartedAt','FinishedAt','ExitCode')},
                restart_count=info['RestartCount'], limits={k:host.get(k) for k in ('NanoCpus','Memory','MemorySwap')},
                log_config=host.get('LogConfig'))


class Group:
    def __init__(self, info, profile):
        pid = info['State']['Pid']
        relative = next(line[3:] for line in Path(f'/host/proc/{pid}/cgroup').read_text().splitlines() if line.startswith('0::'))
        self.relative = relative
        self.path = Path('/host/cgroup') / relative.lstrip('/')
        quota, period = map(int, (self.path/'cpu.max').read_text().split())
        cpu, memory = PROFILES[profile]
        require(quota/period == cpu, 'La CPU efectiva no corresponde al perfil.')
        require(int((self.path/'memory.max').read_text()) == memory, 'La memoria efectiva no corresponde al perfil.')
        require((self.path/'memory.swap.max').read_text().strip()=='0', 'El contenedor puede utilizar swap.')

    def snapshot(self):
        def fields(name):
            return {key:int(value) for key,value in (line.split() for line in (self.path/name).read_text().splitlines())}
        return dict(epoch=time.time(), cpu=fields('cpu.stat'), memory_events=fields('memory.events'), memory_current=int((self.path/'memory.current').read_text()))


class Prometheus:
    def __init__(self, info, group):
        self.base = os.environ.get('PROMETHEUS_URL', 'http://prometheus:9090')
        self.labels = {'job':'cadvisor','name':info['Name'].lstrip('/'),'id':group.relative}

    def get(self, endpoint, params=None):
        url = self.base+'/api/v1/'+endpoint+('?' + urlencode(params) if params else '')
        with urlopen(url,timeout=30) as response:
            payload=json.load(response)
        require(payload['status']=='success', 'Prometheus devolvio un error.')
        return payload['data']

    def selector(self, metric):
        return metric+'{'+','.join(f'{k}={json.dumps(v)}' for k,v in self.labels.items())+'}'

    def check(self):
        targets = self.get('targets')['activeTargets']
        for job in ('cadvisor','node-exporter','prometheus'):
            matches=[t for t in targets if t['labels'].get('job')==job]
            require(len(matches)==1 and matches[0]['health']=='up', f'El destino {job} no esta up.')
            require(matches[0]['scrapeInterval']=='1s', f'El intervalo de {job} debe ser 1s.')
        for metric in ('container_memory_rss','container_cpu_cfs_throttled_seconds_total','container_cpu_usage_seconds_total'):
            result=self.get('query',{'query':self.selector(metric)})['result']
            require(len(result)==1, f'No se identifica la serie {metric} del motor.')

        nodes = self.get('query', {'query':'{job="node-exporter",__name__=~"node_uname_info|node_memory_SwapTotal_bytes|node_disk_read_bytes_total"}'})['result']
        names = {r['metric'].get('__name__') for r in nodes}
        require({'node_uname_info','node_memory_SwapTotal_bytes','node_disk_read_bytes_total'} <= names, 'Faltan metricas de host Linux.')

    def export(self, start, end):
        seconds=math.ceil(end-start)+10
        selectors={metric:self.selector(metric) for metric in ('container_memory_rss','container_cpu_usage_seconds_total','container_cpu_cfs_throttled_seconds_total','container_memory_usage_bytes','container_memory_working_set_bytes','container_last_seen')}
        selectors['host_linux']='{job="node-exporter",__name__=~"node_cpu_seconds_total|node_memory_.*|node_vmstat_pswpin|node_vmstat_pswpout|node_disk_.*"}'
        selectors['scrape_health']='{job=~"cadvisor|node-exporter|prometheus",__name__=~"up|scrape_duration_seconds"}'
        output={}
        for name, selector in selectors.items():
            data=self.get('query',{'query':selector+f'[{seconds}s]', 'time':end})
            for series in data['result']:
                series['values']=[v for v in series.get('values',[]) if start <= float(v[0]) <= end]
            output[name]=data
        return output


def used_disk(value):
    if isinstance(value,dict):
        return value.get('usedDisk') is True or any(used_disk(v) for v in value.values())
    return isinstance(value,list) and any(used_disk(v) for v in value)


def detect_spill(engine, logs, tag, successful, marker):
    measurement=tag+':measurement'
    evidence, completed=[],0
    for line in logs.splitlines():
        if engine=='postgresql':
            if measurement not in line:
                continue
            if 'temporary file:' in line:
                evidence.append(line)
            if marker in line and re.search(r'duration:.*(?:statement:|execute [^:]*:)',line):
                completed+=1
        else:
            try:
                record=json.loads(line[line.index('{'):])
            except (ValueError,json.JSONDecodeError):
                continue
            attr=record.get('attr',{})
            command=attr.get('command',{})
            if command.get('comment')!=measurement:
                continue
            if command.get('aggregate')=='orders' and record.get('msg')=='Slow query':
                completed+=1
            if used_disk(attr):
                evidence.append(record)
    # Evidencia positiva es suficiente para true; false requiere captura completa.
    known = bool(evidence) or successful>0 and completed>=successful
    return dict(value=True if evidence else False if known else None,
                completed_query_logs=completed, expected_successful_queries=successful,
                evidence=evidence)
