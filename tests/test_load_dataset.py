"""Pruebas pequenas del cargador, sin conexiones a bases de datos."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location("parcel_loader", Path("scripts/load_dataset.py"))
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)


@contextlib.contextmanager
def working_directory(path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def manifest_fixture(payload):
    return {
        "case": "paqueteria", "model_version": "paqueteria-v1",
        "format": "jsonl/utf-8/LF", "seed": 2026,
        "dataset": "data/envios.jsonl", "records": 500000, "packages": 1500000,
        "average_packages_per_shipment": 3.0, "max_nested_container_depth": 3,
        "observation_cutoff": "2026-10-01T00:00:00Z",
        "q2_interval": {"inclusive_start": "2026-01-01T00:00:00Z",
                        "exclusive_end": "2026-10-01T00:00:00Z"},
        "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
    }


COUNTS = {"records": 2, "packages": 6,
          "q1_matching_packages": 3, "q2_matching_shipments": 1}
ENV = {"MONGO_USER": "test", "MONGO_PASSWORD": "test", "MONGO_DB": "benchmark",
       "POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test", "POSTGRES_DB": "benchmark"}


class SourceTests(unittest.TestCase):
    def test_case_size_hash_and_interval_are_checked_before_connection(self):
        payload = b'{"envio_id":"sample"}\n'
        with tempfile.TemporaryDirectory() as folder:
            dataset, manifest_file = Path(folder) / "data", Path(folder) / "manifest"
            dataset.write_bytes(payload)
            manifest = manifest_fixture(payload)
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(loader.verify_source(dataset, manifest_file), manifest)
            invalids = (("case", "orders"), ("model_version", "other"),
                        ("dataset", "data/orders.jsonl"), ("seed", 9),
                        ("packages", 5), ("size_bytes", len(payload) + 1),
                        ("sha256", "0" * 64), ("q2_interval", {}))
            for key, value in invalids:
                with self.subTest(key=key):
                    broken = {**manifest, key: value}
                    manifest_file.write_text(json.dumps(broken), encoding="utf-8")
                    with self.assertRaises(RuntimeError):
                        loader.verify_source(dataset, manifest_file)

    def test_source_rows_preserves_payload_and_shipment_id(self):
        raw = '{"envio_id":"uuid-here","paquetes":[],"peso":1.25}'
        with tempfile.TemporaryDirectory() as folder:
            dataset = Path(folder) / "envios.jsonl"
            dataset.write_text(raw + "\n", encoding="utf-8")
            number, document, json_text = next(loader.source_rows(dataset))
            self.assertEqual(number, 1)
            self.assertEqual(document["envio_id"], "uuid-here")
            self.assertEqual(json_text, raw)

    def test_each_count_must_match_and_missing_keys_are_rejected(self):
        loader.check_counts(COUNTS, COUNTS)
        for key in loader.COUNT_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(RuntimeError):
                    loader.check_counts({**COUNTS, key: COUNTS[key] + 1}, COUNTS)
                missing = {k: v for k, v in COUNTS.items() if k != key}
                with self.assertRaises(RuntimeError):
                    loader.check_counts(missing, missing)


class MongoDBTests(unittest.TestCase):
    def test_index_unique_option_is_applied(self):
        collection = MagicMock()
        loader.create_mongodb_indexes(collection)
        collection.create_index.assert_any_call(
            [("envio_id", 1)], name="envios_envio_id_idx", unique=True)
        collection.create_index.assert_any_call(
            [("fecha_cierre", 1)], name="envios_fecha_cierre_idx")
        self.assertEqual(collection.create_index.call_count, 2)

    def make_client(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.server_info.return_value = {"version": "test"}
        database = client.__getitem__.return_value
        database.name = "benchmark"
        collection = database.__getitem__.return_value
        collection.database = database
        collection.name = "envios"
        collection.find_one.return_value = None
        collection.aggregate.return_value = iter([COUNTS])
        collection.list_indexes.return_value = [
            {"name": "_id_", "key": {"_id": 1}},
            {"name": "envios_fecha_cierre_idx", "key": {"fecha_cierre": 1}},
            {"name": "envios_envio_id_idx", "key": {"envio_id": 1}, "unique": True},
        ]
        return client, collection

    def test_load_uses_envios_and_checks_insertions_and_counts(self):
        client, collection = self.make_client()
        factory = MagicMock(return_value=client)
        documents = [{"envio_id": "id-1"}, {"envio_id": "id-2"}]
        inserted_ids = []

        def insert(batch, ordered):
            self.assertTrue(ordered)
            inserted_ids.extend(d["envio_id"] for d in batch)
            return SimpleNamespace(inserted_ids=list(range(len(batch))))

        collection.insert_many.side_effect = insert
        rows = [(i, doc, json.dumps(doc)) for i, doc in enumerate(documents, 1)]
        with patch.dict(sys.modules, {"pymongo": SimpleNamespace(MongoClient=factory)}), \
             patch.dict(os.environ, ENV), patch.object(loader, "version", return_value="test"), \
             patch.object(loader, "source_rows", return_value=iter(rows)):
            result = loader.load_mongodb(COUNTS)
        client.__getitem__.return_value.__getitem__.assert_called_once_with("envios")
        self.assertEqual(inserted_ids, ["id-1", "id-2"])
        self.assertEqual(result["counts"], COUNTS)
        self.assertEqual(result["collection"], "envios")
        self.assertTrue(next(i for i in result["indexes"] if i["name"] == "envios_envio_id_idx")["unique"])

    def test_existing_collection_is_not_modified(self):
        client, collection = self.make_client()
        collection.find_one.return_value = {"_id": "existing"}
        factory = MagicMock(return_value=client)
        with patch.dict(sys.modules, {"pymongo": SimpleNamespace(MongoClient=factory)}), \
             patch.dict(os.environ, ENV), self.assertRaisesRegex(RuntimeError, "ya contiene"):
            loader.load_mongodb(COUNTS)
        collection.create_index.assert_not_called()
        collection.insert_many.assert_not_called()


class PostgreSQLTests(unittest.TestCase):
    def test_copy_uses_envio_id_and_commits_after_checking_counts(self):
        connection, cursor = MagicMock(), MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [(None,), tuple(COUNTS.values()), ("test",)]
        cursor.fetchall.return_value = [("envios_pkey", "PRIMARY KEY"),
                                       ("envios_fecha_cierre_idx", "INDEX")]
        copy = cursor.copy.return_value.__enter__.return_value
        document = {"envio_id": "uuid-first"}
        rows = [(1, document, '{"envio_id":"uuid-first"}'),
                (2, {"envio_id": "uuid-second"}, '{"envio_id":"uuid-second"}')]
        factory = MagicMock(return_value=connection)
        with patch.dict(sys.modules, {"psycopg": SimpleNamespace(connect=factory)}), \
             patch.dict(os.environ, ENV), patch.object(loader, "version", return_value="test"), \
             patch.object(loader, "source_rows", return_value=iter(rows)):
            result = loader.load_postgresql(COUNTS)
        cursor.copy.assert_called_once_with("COPY public.envios (id, data) FROM STDIN")
        copy.write_row.assert_any_call(("uuid-first", '{"envio_id":"uuid-first"}'))
        cursor.execute.assert_any_call(loader.POSTGRESQL_COUNTS_SQL, (loader.Q2_START, loader.CUTOFF))
        connection.__exit__.assert_called_once_with(None, None, None)
        self.assertEqual(result["counts"], COUNTS)
        self.assertEqual(result["table"], "public.envios")

    def test_existing_table_is_not_modified(self):
        connection, cursor = MagicMock(), MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [("envios",), (True,)]
        factory = MagicMock(return_value=connection)
        with patch.dict(sys.modules, {"psycopg": SimpleNamespace(connect=factory)}), \
             patch.dict(os.environ, ENV), self.assertRaisesRegex(RuntimeError, "ya contiene"):
            loader.load_postgresql(COUNTS)
        cursor.copy.assert_not_called()
        self.assertEqual(cursor.execute.call_count, 2)


class ReportTests(unittest.TestCase):
    def test_existing_report_stops_before_source_or_database_access(self):
        with tempfile.TemporaryDirectory() as folder, working_directory(folder):
            report = Path("results/paqueteria/load_mongodb.json")
            report.parent.mkdir(parents=True)
            report.write_text("existing", encoding="utf-8")
            with patch.object(loader, "verify_source") as verify, \
                 patch.object(loader, "load_mongodb") as load:
                with self.assertRaisesRegex(RuntimeError, "no se repetira"):
                    loader.run_load("mongodb")
            verify.assert_not_called()
            load.assert_not_called()
            self.assertEqual(report.read_text(), "existing")

    def test_failed_load_does_not_publish_success(self):
        with tempfile.TemporaryDirectory() as folder, working_directory(folder):
            with patch.object(loader, "verify_source", return_value={}), \
                 patch.object(loader, "load_mongodb", side_effect=RuntimeError("count mismatch")):
                with self.assertRaises(RuntimeError):
                    loader.run_load("mongodb")
            self.assertFalse(Path("results/paqueteria/load_mongodb.json").exists())

    def test_success_report_identifies_source_case_and_engine(self):
        manifest = {**COUNTS, "model_version": "paqueteria-v1",
                    "dataset": "data/envios.jsonl", "sha256": "f" * 64}
        with tempfile.TemporaryDirectory() as folder, working_directory(folder):
            with patch.object(loader, "verify_source", return_value=manifest), \
                 patch.object(loader, "load_postgresql", return_value={"counts": COUNTS}), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = loader.run_load("postgresql")
            saved = json.loads(Path("results/paqueteria/load_postgresql.json").read_text())
            self.assertEqual(saved, result)
            self.assertEqual(saved["case"], "paqueteria")
            self.assertEqual(saved["engine"], "postgresql")
            self.assertEqual(saved["status"], "ok")
            self.assertEqual(saved["source_sha256"], "f" * 64)


if __name__ == "__main__":
    unittest.main()
