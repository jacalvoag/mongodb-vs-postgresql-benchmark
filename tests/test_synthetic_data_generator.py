"""Comprueba integridad temporal, calculos, reproducibilidad y salidas pequenas."""

import copy
import hashlib
import json
import random
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import synthetic_data_generator as generator


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def independent_delay(package):
    if package["estado"] != "entregado":
        return 0
    seconds = (date(package["fecha_entrega"]) - date(package["fecha_compromiso"])).total_seconds()
    return max(0, int(seconds // 60))


class ShipmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = random.Random(2026)
        cls.shipments = [generator.build_shipment(i, 2026, rng) for i in range(1000)]

    def test_shape_depth_and_global_identifiers(self):
        shipment_ids, package_ids = set(), set()
        package_count = 0
        for shipment in self.shipments:
            UUID(shipment["envio_id"])
            self.assertNotIn(shipment["envio_id"], shipment_ids)
            shipment_ids.add(shipment["envio_id"])
            self.assertEqual(generator.container_depth(shipment) - 1, 3)
            self.assertTrue(1 <= len(shipment["paquetes"]) <= 5)
            for package in shipment["paquetes"]:
                self.assertNotIn(package["paquete_id"], package_ids)
                package_ids.add(package["paquete_id"])
                package_count += 1
        self.assertEqual(package_count, 3000)

    def test_dates_states_and_accumulated_delay_independently(self):
        cutoff = datetime(2026, 10, 1, tzinfo=timezone.utc)
        lower = datetime(2025, 1, 1, tzinfo=timezone.utc)
        pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z")
        for shipment in self.shipments:
            created = date(shipment["fecha_creacion"])
            self.assertTrue(lower <= created < cutoff)
            dates = [shipment["fecha_creacion"]]
            delivered = []
            package_states = []
            for package in shipment["paquetes"]:
                package_states.append(package["estado"])
                dates.append(package["fecha_compromiso"])
                self.assertGreater(date(package["fecha_compromiso"]), created)
                self.assertGreater(package["peso_kg"], 0)
                self.assertTrue(all(v > 0 for v in package["dimensiones_cm"].values()))
                if package["estado"] == "entregado":
                    actual = date(package["fecha_entrega"])
                    self.assertTrue(created <= actual <= cutoff)
                    delivered.append(actual)
                    dates.append(package["fecha_entrega"])
                else:
                    self.assertIsNone(package["fecha_entrega"])
            if shipment["estado"] == "finalizado":
                self.assertEqual(set(package_states), {"entregado"})
                self.assertEqual(date(shipment["fecha_cierre"]), max(delivered))
                total = sum(independent_delay(p) for p in shipment["paquetes"])
                self.assertEqual(shipment["retraso_acumulado_min"], total)
            elif shipment["estado"] == "en_transito":
                self.assertIn("en_transito", package_states)
                self.assertNotIn("cancelado", package_states)
                self.assertIsNone(shipment["fecha_cierre"])
                self.assertIsNone(shipment["retraso_acumulado_min"])
            else:
                self.assertEqual(shipment["estado"], "cancelado")
                self.assertEqual(set(package_states), {"cancelado"})
                self.assertTrue(created <= date(shipment["fecha_cierre"]) <= cutoff)
                self.assertIsNone(shipment["retraso_acumulado_min"])
            if shipment["fecha_cierre"] is not None:
                dates.append(shipment["fecha_cierre"])
            for value in dates:
                self.assertRegex(value, pattern)

    def test_sample_covers_business_cases(self):
        self.assertEqual({s["estado"] for s in self.shipments}, {"finalizado", "en_transito", "cancelado"})
        self.assertEqual({s["servicio"] for s in self.shipments}, {"estandar", "expres"})
        self.assertEqual(len({s["centro_distribucion"]["id"] for s in self.shipments}), 6)
        self.assertTrue(any(s["estado"] == "en_transito" and any(
            p["estado"] == "entregado" for p in s["paquetes"]) for s in self.shipments))
        signs = set()
        for shipment in self.shipments:
            for package in shipment["paquetes"]:
                if package["estado"] == "entregado":
                    delta = date(package["fecha_entrega"]) - date(package["fecha_compromiso"])
                    seconds = delta.total_seconds()
                    signs.add((seconds > 0) - (seconds < 0))
        self.assertEqual(signs, {-1, 0, 1})

    def test_validator_checks_all_generated_states(self):
        for shipment in self.shipments:
            expected = [independent_delay(p) for p in shipment["paquetes"] if independent_delay(p) > 0]
            self.assertEqual(generator.validate_shipment(shipment), expected)

    def test_corrupted_accumulated_delay_is_rejected(self):
        shipment = copy.deepcopy(next(s for s in self.shipments if s["estado"] == "finalizado"))
        shipment["retraso_acumulado_min"] += 1
        with self.assertRaisesRegex(ValueError, "acumulado"):
            generator.validate_shipment(shipment)

    def test_inconsistent_states_and_future_delivery_are_rejected(self):
        shipment = copy.deepcopy(next(s for s in self.shipments if s["estado"] == "en_transito"))
        shipment["estado"] = "finalizado"
        with self.assertRaises(ValueError):
            generator.validate_shipment(shipment)
        shipment = copy.deepcopy(next(s for s in self.shipments if s["estado"] == "finalizado"))
        shipment["paquetes"][0]["fecha_entrega"] = "2026-10-02T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "Entrega"):
            generator.validate_shipment(shipment)

    def test_minute_precision_is_required(self):
        with self.assertRaises(ValueError):
            generator.parse_date("2026-01-01T00:00:01Z")


class OutputTests(unittest.TestCase):
    def test_manifest_matches_serialized_data_and_query_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output, manifest_file = root / "data/envios.jsonl", root / "results/manifest.json"
            manifest = generator.generate(100, 2026, output, manifest_file)
            payload = output.read_bytes()
            shipments = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
            self.assertEqual(len(shipments), 100)
            self.assertTrue(payload.endswith(b"\n"))
            self.assertNotIn(b"\r", payload)
            self.assertEqual(manifest, json.loads(manifest_file.read_text(encoding="utf-8")))
            self.assertEqual(manifest["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(manifest["size_bytes"], len(payload))
            self.assertEqual(manifest["packages"], 300)
            self.assertEqual(manifest["average_packages_per_shipment"], 3)
            self.assertEqual(sum(manifest["shipments_by_state"].values()), 100)
            self.assertEqual(sum(manifest["packages_by_state"].values()), 300)
            self.assertEqual(sum(manifest["shipments_by_center"].values()), 100)
            self.assertEqual(sum(manifest["shipments_by_service"].values()), 100)
            self.assertEqual(sum(manifest["shipments_by_center_and_service"].values()), 100)
            matching_packages = sum(independent_delay(p) > 0 for s in shipments for p in s["paquetes"])
            matching_shipments = sum(s["estado"] == "finalizado" and
                "2026-01-01T00:00:00Z" <= s["fecha_cierre"] < "2026-10-01T00:00:00Z" for s in shipments)
            self.assertEqual(manifest["q1_matching_packages"], matching_packages)
            self.assertEqual(manifest["q2_matching_shipments"], matching_shipments)

    def test_same_seed_same_bytes_and_different_seed_changes_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, seed in (("a", 2026), ("b", 2026), ("c", 2027)):
                generator.generate(10, seed, root / (name + ".jsonl"), root / (name + ".json"))
            self.assertEqual((root / "a.jsonl").read_bytes(), (root / "b.jsonl").read_bytes())
            self.assertNotEqual((root / "a.jsonl").read_bytes(), (root / "c.jsonl").read_bytes())

    def test_existing_outputs_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output, manifest = root / "envios.jsonl", root / "manifest.json"
            output.write_bytes(b"existing data")
            with self.assertRaises(FileExistsError):
                generator.generate(5, 2026, output, manifest)
            self.assertEqual(output.read_bytes(), b"existing data")
            output.unlink()
            manifest.write_bytes(b"existing manifest")
            with self.assertRaises(FileExistsError):
                generator.generate(5, 2026, output, manifest)
            self.assertEqual(manifest.read_bytes(), b"existing manifest")
            self.assertFalse(output.exists())

    def test_invalid_size_and_identical_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for records in (0, -5, 7):
                with self.assertRaises(ValueError):
                    generator.generate(records, 2026, root / "data", root / "manifest")
            with self.assertRaises(ValueError):
                generator.generate(5, 2026, root / "same", root / "same")
            self.assertEqual(list(root.iterdir()), [])

    def test_failure_does_not_publish_partial_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(generator, "build_shipment", side_effect=ValueError("invalid fixture")):
                with self.assertRaises(ValueError):
                    generator.generate(5, 2026, root / "envios.jsonl", root / "manifest.json")
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
