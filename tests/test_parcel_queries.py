"""Comprueba referencia, empates y validacion con ejemplos pequenos conocidos."""

import contextlib
import copy
import io
import json
import os
import random
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path("scripts").resolve()))
import parcel_reference as reference
import validate_queries as validator


@contextlib.contextmanager
def working_directory(path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def sample_shipment():
    return {
        "envio_id": "example-1", "centro_distribucion": {"id": "CD-01"},
        "servicio": "estandar", "estado": "finalizado",
        "fecha_creacion": "2026-03-10T08:00:00Z",
        "fecha_cierre": "2026-03-12T13:30:00Z", "retraso_acumulado_min": 120,
        "paquetes": [
            {"estado": "entregado", "fecha_compromiso": "2026-03-12T12:00:00Z",
             "fecha_entrega": delivered}
            for delivered in ("2026-03-12T11:40:00Z", "2026-03-12T12:30:00Z", "2026-03-12T13:30:00Z")
        ],
    }


def projected(shipment_id, score):
    return {"envio_id": shipment_id, "centro_id": "CD-01", "servicio": "estandar",
            "fecha_cierre": "2026-03-12T13:30:00Z", "retraso_acumulado_min": score}


def small_reference():
    top = reference.TopKReference(3)
    for shipment_id, score in (("a", 500), ("b", 400), ("c", 400), ("d", 400), ("e", 100)):
        top.add(projected(shipment_id, score))
    return {
        "q1": [{"centro_id": "CD-01", "servicio": "estandar",
                "paquetes_retrasados": 2, "retraso_total_min": 120, "retraso_promedio_min": "60"}],
        "q2": top.finish(),
    }


class BusinessReferenceTests(unittest.TestCase):
    def test_known_example_has_only_delays_30_and_90(self):
        delays, candidate = reference.inspect_shipment(sample_shipment())
        self.assertEqual(delays, [30, 90])
        self.assertEqual(candidate["retraso_acumulado_min"], 120)

    def test_delivered_packages_of_open_shipments_are_in_q1_only(self):
        shipment = sample_shipment()
        shipment.update(estado="en_transito", fecha_cierre=None, retraso_acumulado_min=None)
        shipment["paquetes"][2].update(estado="en_transito", fecha_entrega=None)
        delays, candidate = reference.inspect_shipment(shipment)
        self.assertEqual(delays, [30])
        self.assertIsNone(candidate)

    def test_cancelled_packages_do_not_contribute(self):
        shipment = sample_shipment()
        shipment.update(estado="cancelado", fecha_cierre="2026-03-11T00:00:00Z",
                        retraso_acumulado_min=None)
        for package in shipment["paquetes"]:
            package.update(estado="cancelado", fecha_entrega=None)
        self.assertEqual(reference.inspect_shipment(shipment), ([], None))

    def test_corrupted_stored_accumulated_delay_is_rejected(self):
        shipment = sample_shipment()
        shipment["retraso_acumulado_min"] = 119
        with self.assertRaisesRegex(RuntimeError, "Acumulado"):
            reference.inspect_shipment(shipment)

    def test_close_must_be_latest_delivery(self):
        shipment = sample_shipment()
        shipment["fecha_cierre"] = "2026-03-12T12:30:00Z"
        with self.assertRaisesRegex(RuntimeError, "Cierre"):
            reference.inspect_shipment(shipment)

    def test_q2_start_is_inclusive_and_end_exclusive(self):
        for created, promise, delivered, included in (
            ("2025-12-30T00:00:00Z", "2025-12-31T23:30:00Z", "2026-01-01T00:00:00Z", True),
            ("2025-12-30T00:00:00Z", "2025-12-31T23:29:00Z", "2025-12-31T23:59:00Z", False),
            ("2026-09-29T00:00:00Z", "2026-09-30T23:30:00Z", "2026-10-01T00:00:00Z", False),
        ):
            with self.subTest(delivered=delivered):
                shipment = sample_shipment()
                shipment.update(fecha_creacion=created, fecha_cierre=delivered, retraso_acumulado_min=30)
                shipment["paquetes"] = [{"estado": "entregado", "fecha_compromiso": promise,
                                         "fecha_entrega": delivered}]
                delays, candidate = reference.inspect_shipment(shipment)
                self.assertEqual(delays, [30])
                self.assertEqual(candidate is not None, included)

    def test_future_delivery_and_nonminute_dates_are_rejected(self):
        with self.assertRaises(RuntimeError):
            reference.utc_date("2026-03-12T13:30:01Z")
        shipment = sample_shipment()
        shipment["paquetes"][2]["fecha_entrega"] = "2026-10-01T00:01:00Z"
        with self.assertRaisesRegex(RuntimeError, "Entrega"):
            reference.inspect_shipment(shipment)

    def test_streaming_reference_counts_and_averages(self):
        closed = sample_shipment()
        opened = sample_shipment()
        opened.update(envio_id="example-2", estado="en_transito", fecha_cierre=None,
                      retraso_acumulado_min=None)
        opened["paquetes"][2].update(estado="en_transito", fecha_entrega=None)
        manifest = {"records": 2, "packages": 6, "q1_matching_packages": 3,
                    "q2_matching_shipments": 1, "sha256": "test-source"}
        with tempfile.TemporaryDirectory() as folder:
            dataset = Path(folder) / "envios.jsonl"
            dataset.write_text("\n".join(json.dumps(s) for s in (closed, opened)) + "\n", encoding="utf-8")
            result = reference.build_reference(manifest, dataset, limit=1)
            self.assertEqual(result["q1"][0]["paquetes_retrasados"], 3)
            self.assertEqual(result["q1"][0]["retraso_total_min"], 150)
            self.assertEqual(result["q1"][0]["retraso_promedio_min"], "50")
            self.assertEqual(result["q2"]["cutoff_min"], 120)
            self.assertEqual(result["source_sha256"], "test-source")
            with self.assertRaisesRegex(RuntimeError, "manifiesto"):
                reference.build_reference({**manifest, "packages": 5}, dataset, limit=1)


class TopKTests(unittest.TestCase):
    def test_streaming_top_k_matches_full_sort_with_ties_in_any_input_order(self):
        scores = [random.Random(seed).randint(0, 20) for seed in range(400)]
        rows = [projected(str(i), score) for i, score in enumerate(scores)]
        limit = 37
        cutoff = sorted(scores, reverse=True)[limit - 1]
        required = {row["envio_id"] for row in rows if row["retraso_acumulado_min"] > cutoff}
        boundary = {row["envio_id"] for row in rows if row["retraso_acumulado_min"] == cutoff}
        shuffled = rows.copy()
        random.Random(2026).shuffle(shuffled)
        orders = [rows, list(reversed(rows)), shuffled,
                  sorted(rows, key=lambda r: r["retraso_acumulado_min"])]
        for order in orders:
            top = reference.TopKReference(limit)
            for row in order:
                top.add(row)
            actual = top.finish()
            self.assertEqual(actual["candidate_count"], 400)
            self.assertEqual(actual["cutoff_min"], cutoff)
            self.assertEqual(set(actual["required"]), required)
            self.assertEqual(set(actual["boundary"]), boundary)

    def test_all_ties_are_retained(self):
        top = reference.TopKReference(3)
        for i in range(10):
            top.add(projected(str(i), 0))
        actual = top.finish()
        self.assertEqual(actual["required"], {})
        self.assertEqual(len(actual["boundary"]), 10)

    def test_insufficient_candidates_are_rejected(self):
        top = reference.TopKReference(3)
        top.add(projected("a", 10))
        with self.assertRaisesRegex(RuntimeError, "suficientes"):
            top.finish()


class ResultValidationTests(unittest.TestCase):
    def test_q1_accepts_engine_numeric_representations(self):
        expected = small_reference()
        row = copy.deepcopy(expected["q1"][0])
        row["retraso_total_min"] = "120.000000000"
        row["retraso_promedio_min"] = 60.0
        reference.validate_q1([row], expected)
        row["retraso_promedio_min"] = Decimal("60.000000001")
        reference.validate_q1([row], expected)

    def test_q1_rejects_wrong_counts_totals_and_averages(self):
        expected = small_reference()
        for key, value in (("paquetes_retrasados", 3), ("retraso_total_min", "120.00000001"),
                           ("retraso_promedio_min", 61), ("retraso_promedio_min", "NaN")):
            with self.subTest(key=key, value=value):
                row = {**expected["q1"][0], key: value}
                with self.assertRaises(RuntimeError):
                    reference.validate_q1([row], expected)

    def test_q1_rejects_missing_duplicate_or_unexpected_groups(self):
        expected = small_reference()
        row = expected["q1"][0]
        for rows in ([], [row, row], [{**row, "centro_id": "CD-99"}], [{**row, "extra": 1}]):
            with self.assertRaises(RuntimeError):
                reference.validate_q1(rows, expected)

    def test_q2_accepts_different_valid_tie_selections(self):
        expected = small_reference()
        reference.validate_q2([projected("a", 500), projected("b", 400), projected("c", 400)], expected)
        reference.validate_q2([projected("a", 500), projected("d", 400), projected("b", 400)], expected)

    def test_q2_rejects_omitted_superior_duplicate_wrong_fields_or_order(self):
        expected = small_reference()
        correct = [projected("a", 500), projected("b", 400), projected("c", 400)]
        invalids = [
            [projected("b", 400), projected("c", 400), projected("d", 400)],
            [projected("a", 500), projected("b", 400), projected("b", 400)],
            list(reversed(correct)), correct[:2],
            [projected("a", 500), projected("b", 400), projected("e", 100)],
        ]
        for key, value in (("centro_id", "CD-99"), ("servicio", "expres"),
                           ("fecha_cierre", "2026-03-12T13:31:00Z"), ("retraso_acumulado_min", 399)):
            changed = copy.deepcopy(correct)
            changed[1][key] = value
            invalids.append(changed)
        for rows in invalids:
            with self.assertRaises(RuntimeError):
                reference.validate_q2(rows, expected)


class ExecutionControlTests(unittest.TestCase):
    def test_variants_execute_once_and_each_saves_one_plan(self):
        expected = small_reference()
        q2 = [projected("a", 500), projected("b", 400), projected("c", 400)]
        executions, explanations = [], []

        def execute(query_id, setting):
            executions.append((query_id, setting))
            return expected["q1"] if query_id == "Q1" else q2

        def explain(query_id, setting):
            explanations.append((query_id, setting))
            return {"unit_test_plan": True}

        with tempfile.TemporaryDirectory() as folder, working_directory(folder), \
             contextlib.redirect_stdout(io.StringIO()):
            cases = validator.run_cases("mongodb", execute, explain, expected)
            self.assertEqual([c["status"] for c in cases], ["ok", "ok", "ok"])
            self.assertEqual(executions, [("Q1", None), ("Q2", False), ("Q2", True)])
            self.assertEqual(explanations, executions)
            for case in cases:
                self.assertTrue(Path(case["results_file"]).is_file())
                self.assertTrue(Path(case["plan_file"]).is_file())

    def test_wrong_results_save_error_and_are_not_explained_or_retried(self):
        expected = small_reference()
        q2 = [projected("a", 500), projected("b", 400), projected("c", 400)]
        executions, explanations = [], []

        def execute(query_id, setting):
            executions.append((query_id, setting))
            return [{**expected["q1"][0], "retraso_total_min": 119}] if query_id == "Q1" else q2

        def explain(query_id, setting):
            explanations.append((query_id, setting))
            return {"unit_test_plan": True}

        with tempfile.TemporaryDirectory() as folder, working_directory(folder), \
             contextlib.redirect_stdout(io.StringIO()):
            cases = validator.run_cases("mongodb", execute, explain, expected)
            self.assertEqual(cases[0]["status"], "error")
            self.assertEqual(cases[0]["phase"], "compare_with_reference")
            self.assertEqual(len(executions), 3)
            self.assertEqual(explanations, [("Q2", False), ("Q2", True)])
            self.assertTrue(Path(cases[0]["error_file"]).is_file())

    def test_existing_output_stops_before_reading_source_or_querying(self):
        with tempfile.TemporaryDirectory() as folder, working_directory(folder):
            path = validator.case_paths("postgresql", "Q1", "default")["plan_file"]
            path.parent.mkdir(parents=True)
            path.write_text("existing plan", encoding="utf-8")
            with patch.object(validator, "verify_source") as verify:
                with self.assertRaisesRegex(RuntimeError, "Ya existe"):
                    validator.run_mode("postgresql")
                verify.assert_not_called()
            self.assertEqual(path.read_text(), "existing plan")

    def test_reference_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder, working_directory(folder):
            validator.REFERENCE.parent.mkdir(parents=True)
            validator.REFERENCE.write_text("existing reference", encoding="utf-8")
            with patch.object(validator, "verify_source") as verify:
                with self.assertRaisesRegex(RuntimeError, "Ya existe"):
                    validator.run_mode("reference")
                verify.assert_not_called()

    def test_decimal_normalization_preserves_exact_values(self):
        self.assertEqual(validator.normalize([{"value": Decimal("123.456"), "count": 2}]),
                         [{"value": "123.456", "count": 2}])


if __name__ == "__main__":
    unittest.main()
