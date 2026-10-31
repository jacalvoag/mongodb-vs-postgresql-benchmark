import json
import multiprocessing as mp
import os
import queue
import time
from pathlib import Path

from .metrics import require


class Session:
    def __init__(self, config, worker):
        self.config, self.worker = config, worker
        self.query = Path(f"queries/{config['engine']}/{config['query']}." + ('json' if config['engine']=='mongodb' else 'sql')).read_text()
        if config['engine'] == 'mongodb':
            from pymongo import MongoClient
            self.client = MongoClient('mongodb', username=os.environ['MONGO_USER'], password=os.environ['MONGO_PASSWORD'], authSource='admin', serverSelectionTimeoutMS=10000)
            self.client.admin.command('ping')
            self.db = self.client[os.environ.get('MONGO_DB', 'benchmark')]
            self.query = json.loads(self.query)
        else:
            import psycopg
            self.client = psycopg.connect(host='postgresql', user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'], dbname=os.environ['POSTGRES_DB'], autocommit=True, prepare_threshold=None, connect_timeout=10)
            self.cursor = self.client.cursor()
            if config['query'] == 'Q2':
                self.cursor.execute("SELECT set_config('work_mem', %s, false)", (config['variant'].replace('work_mem_', ''),)).fetchone()

    def prepare(self, phase):
        self.tag = f"{self.config['tag']}:{phase}"
        if self.config['engine'] == 'postgresql':
            self.cursor.execute("SELECT set_config('application_name', %s, false)", (f'{self.tag}:w{self.worker}',)).fetchone()

    def execute(self):
        if self.config['engine'] == 'mongodb':
            options = {'comment': self.tag}
            if self.config['query'] == 'Q2':
                options['allowDiskUse'] = self.config['variant'] == 'allowDiskUse_true'
            with self.db['envios'].aggregate(self.query, **options) as cursor:
                return sum(1 for _ in cursor)
        self.cursor.execute(self.query)
        return len(self.cursor.fetchall())

    def close(self):
        self.client.close()


def worker(number, config, commands, events, factory):
    session = None
    try:
        session = factory(config, number)
        events.put(('ready', number, None))
        while True:
            command = commands.recv()
            if command[0] == 'close':
                return
            if command[0] == 'prepare':
                session.prepare(command[1])
                events.put(('prepared', number, None))
                continue
            _, phase, start, end = command
            time.sleep(max(0, start-time.monotonic()))
            totals = dict(successful=0, failed=0)
            while time.monotonic() < end:
                began = time.monotonic()
                if began >= end:
                    break
                error = None
                try:
                    rows = session.execute()
                    require(rows == config['expected_rows'], f'Resultado inesperado: {rows} filas.')
                except Exception as exc:
                    error = f'{type(exc).__name__}: {exc}'
                finished = time.monotonic()
                totals['failed' if error else 'successful'] += 1
                if phase == 'measurement':
                    events.put(('sample', number, dict(start_s=began-start, end_s=finished-start, latency_ms=(finished-began)*1000, ok=error is None, error=error)))
                if error:
                    events.put(('error', number, error))
                    break
            events.put(('done', number, totals))
    except Exception as exc:
        events.put(('fatal', number, f'{type(exc).__name__}: {exc}'))
    finally:
        if session is not None:
            session.close()


class Clients:
    def __init__(self, config, count, factory=Session):
        self.context = mp.get_context('spawn')
        self.events = self.context.Queue()
        self.pipes, self.processes = [], []
        try:
            for n in range(count):
                receiver, sender = self.context.Pipe(duplex=False)
                p = self.context.Process(target=worker, args=(n, config, receiver, self.events, factory))
                p.start()
                receiver.close()
                self.pipes.append(sender)
                self.processes.append(p)
            self.wait('ready')
        except BaseException:
            self.close()
            raise

    def wait(self, expected):
        remaining = set(range(len(self.processes)))
        deadline = time.monotonic()+120  # Solo arranque/preparacion; no limita latencias de consultas.
        while remaining:
            require(time.monotonic() < deadline, f'Tiempo agotado en {expected}.')
            try:
                kind, number, data = self.events.get(timeout=.1)
            except queue.Empty:
                require(all(p.is_alive() for p in self.processes), 'Un cliente termino antes de prepararse.')
                continue
            require(kind == expected, f'Cliente no preparado: {kind}: {data}')
            remaining.discard(number)

    def phase(self, name, seconds, group=None):
        for pipe in self.pipes:
            pipe.send(('prepare', name))
        self.wait('prepared')
        start = time.monotonic()+.2
        end = start+seconds
        epoch = time.time()+(start-time.monotonic())
        for pipe in self.pipes:
            pipe.send(('run', name, start, end))
        time.sleep(max(0, start-time.monotonic()))
        points, samples, errors, finished = [], [], [], set()
        totals = dict(successful=0, failed=0)
        def snapshot():
            try:
                value = group.snapshot()
                points.append(value)
                return value
            except Exception as exc:
                errors.append(f'Lectura de cgroup: {exc}')
                return None
        initial = snapshot() if group else None
        final, boundary_read = None, False
        next_point, next_message = start+1, start+30
        print(f'{name}: {seconds} segundos', flush=True)
        while time.monotonic() < end or len(finished) < len(self.processes):
            now = time.monotonic()
            if group and now >= end and not boundary_read:
                final, boundary_read = snapshot(), True
            elif group and now < end and now >= next_point:
                snapshot()
                next_point = now+1
            if now >= next_message:
                print(f'{name}: {min(now-start, seconds):.0f}/{seconds}s', flush=True)
                next_message = now+30
            try:
                kind, number, data = self.events.get(timeout=.05)
            except queue.Empty:
                for n, p in enumerate(self.processes):
                    if n not in finished and not p.is_alive():
                        errors.append(f'Cliente {n} termino sin resultado final.')
                        finished.add(n)
                continue
            if kind == 'sample':
                samples.append(data)
            elif kind == 'done':
                finished.add(number)
                for key in totals:
                    totals[key] += data[key]
            elif kind in ('fatal', 'error'):
                errors.append(f'Cliente {number}: {data}')
                if kind == 'fatal':
                    finished.add(number)
            else:
                raise RuntimeError(f'Evento inesperado: {kind}')
        return dict(start_epoch=epoch, end_epoch=epoch+seconds,
                    drain_seconds=max(0,time.monotonic()-end), totals=totals,
                    errors=errors, samples=samples, cgroup_samples=points,
                    cgroup_start=initial, cgroup_end=final)

    def close(self):
        for pipe in self.pipes:
            try:
                pipe.send(('close',))
                pipe.close()
            except OSError:
                pass
        deadline = time.monotonic()+5
        for process in self.processes:
            process.join(max(0, deadline-time.monotonic()))
            if process.is_alive():
                process.terminate()
                process.join()
        self.events.close()
