"""Valida Q1/Q2 contra el dataset y guarda planes; no realiza el benchmark."""

import argparse
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from load_dataset import DATASET, verify_source


REFERENCE = Path("results/query_reference.json")
ABS_TOL = Decimal("0.000001")
REL_TOL = Decimal("0.000000000001")
PROFILE = os.environ.get("PROFILE_ID", "PROFILE_A_BASELINE")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def build_reference(manifest):
    groups = defaultdict(lambda: [0, 0])
    candidates = []
    records = items = matching_items = 0
    with DATASET.open(encoding="utf-8") as stream:
        for line in stream:
            order = json.loads(line, parse_float=Decimal)
            records += 1
            for item in order["items"]:
                items += 1
                price = Decimal(item["price"])
                if price > 50:
                    key = (order["customer"]["geo"]["country"], item["category"])
                    groups[key][0] += 1
                    groups[key][1] += item["qty"] * int(price * 100)
                    matching_items += 1
            if order["payment"]["status"] == "completed" and order["timestamp"] >= "2026-01-01T00:00:00Z":
                cents = int(Decimal(order["payment"]["final_total"]) * 100)
                candidates.append((cents, order["order_id"], order["customer"]["id"]))
            if records % 50000 == 0:
                print(f"Referencia: {records:,} ordenes procesadas", flush=True)
    actual = (records, items, matching_items, len(candidates))
    expected = tuple(manifest[k] for k in ("records", "items", "q1_matching_items", "q2_matching_orders"))
    require(actual == expected, "La referencia no coincide con los conteos del manifiesto.")
    require(len(candidates) >= 1000, "No hay suficientes ordenes para el Top-1000.")
    candidates.sort(reverse=True)
    cutoff = candidates[999][0]
    required, boundary = {}, {}
    for cents, order_id, customer_id in candidates:
        if cents < cutoff:
            break
        target = required if cents > cutoff else boundary
        target[order_id] = {"customer_id": customer_id, "cents": cents}
    reference = {
        "source_sha256": manifest["sha256"],
        "q1": [
            {"country": country, "category": category, "count": count, "total_cents": total}
            for (country, category), (count, total) in sorted(groups.items())
        ],
        "q2": {"limit": 1000, "cutoff_cents": cutoff, "required": required, "boundary": boundary},
    }
    save(REFERENCE, reference)
    print(json.dumps({"status": "ok", "reference": str(REFERENCE), "q1_groups": len(groups), "q2_required": len(required), "q2_boundary_candidates": len(boundary), "q2_cutoff_cents": cutoff}, indent=2))


def close(actual, expected):
    return abs(Decimal(str(actual)) - expected) <= max(ABS_TOL, REL_TOL * abs(expected))


def validate_q1(rows, reference):
    expected = {(r["country"], r["category"]): r for r in reference["q1"]}
    actual = {(r["country"], r["category"]): r for r in rows}
    require(len(actual) == len(rows), "Q1 devuelve grupos duplicados.")
    require(actual.keys() == expected.keys(), "Los grupos de Q1 son diferentes.")
    for key, target in expected.items():
        row = actual[key]
        total = Decimal(target["total_cents"]) / 100
        require(row["count"] == target["count"], f"Conteo Q1 incorrecto: {key}")
        require(close(row["total_revenue"], total), f"Suma Q1 incorrecta: {key}")
        require(close(row["avg_revenue"], total / target["count"]), f"Promedio Q1 incorrecto: {key}")


def validate_q2(rows, reference):
    target = reference["q2"]
    require(len(rows) == target["limit"], "Q2 no devuelve 1000 ordenes.")
    allowed = {**target["required"], **target["boundary"]}
    seen = set()
    previous = Decimal("Infinity")
    for row in rows:
        order_id = row["order_id"]
        require(order_id not in seen and order_id in allowed, f"Orden Q2 duplicada o fuera del Top-K: {order_id}")
        seen.add(order_id)
        expected = allowed[order_id]
        amount = Decimal(str(row["final_total"]))
        require(row["customer_id"] == expected["customer_id"], f"Cliente Q2 incorrecto: {order_id}")
        require(amount == Decimal(expected["cents"]) / 100, f"Importe Q2 incorrecto: {order_id}")
        require(amount <= previous, "Q2 no esta ordenada de forma descendente.")
        previous = amount
    require(set(target["required"]).issubset(seen), "Q2 omite ordenes superiores al corte.")


def normalize(engine, query_id, rows):
    if engine == "postgresql":
        return [{key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()} for row in rows]
    if query_id == "Q1":
        return [{"country": r["_id"]["country"], "category": r["_id"]["category"], "total_revenue": str(r["total_revenue"]), "avg_revenue": str(r["avg_revenue"]), "count": r["count"]} for r in rows]
    return [{"order_id": r["order_id"], "customer_id": r["customer"]["id"], "final_total": str(r["payment"]["final_total"])} for r in rows]


def run_cases(engine, cases, execute, explain, reference):
    results = []
    for query_id, variant, setting in cases:
        name = f"{engine}_{query_id}_{variant}"
        plan_path = Path("evidence/plans") / PROFILE / f"{name}.json"
        result_path = Path("results/query_results") / PROFILE / f"{name}.json"
        result = {"query_id": query_id, "variant": variant, "setting": setting}
        print(f"Ejecutando {name}", flush=True)
        try:
            rows = normalize(engine, query_id, execute(query_id, setting))
            save(result_path, rows)
            save(plan_path, explain(query_id, setting))
            validator = validate_q1 if query_id == "Q1" else validate_q2
            validator(rows, reference)
            result.update({"status": "ok", "rows": len(rows), "results_file": str(result_path), "plan_file": str(plan_path)})
        except Exception as error:
            result.update({"status": "error", "error_type": type(error).__name__, "message": str(error)})
            save(Path("evidence/errors") / PROFILE / f"{name}.json", result)
        results.append(result)
        print(json.dumps(result, indent=2), flush=True)
    return results


def run_mongodb(reference):
    from pymongo import MongoClient
    from bson import json_util

    queries = {q: json.loads(Path(f"queries/mongodb/{q}.json").read_text()) for q in ("Q1", "Q2")}
    with MongoClient(host="mongodb", port=27017, username=os.environ["MONGO_USER"], password=os.environ["MONGO_PASSWORD"], authSource="admin", serverSelectionTimeoutMS=10000) as client:
        db = client[os.environ["MONGO_DB"]]
        db.command("ping")

        def execute(query_id, setting):
            options = {} if setting is None else {"allowDiskUse": setting}
            return list(db.orders.aggregate(queries[query_id], **options))

        def explain(query_id, setting):
            command = {"aggregate": "orders", "pipeline": queries[query_id], "cursor": {}}
            if setting is not None:
                command["allowDiskUse"] = setting
            plan = db.command({"explain": command, "verbosity": "executionStats"})
            return json.loads(json_util.dumps(plan))

        cases = [("Q1", "default", None), ("Q2", "allowDiskUse_false", False), ("Q2", "allowDiskUse_true", True)]
        return client.server_info()["version"], run_cases("mongodb", cases, execute, explain, reference)


def run_postgresql(reference):
    import psycopg
    from psycopg.rows import dict_row

    queries = {q: Path(f"queries/postgresql/{q}.sql").read_text() for q in ("Q1", "Q2")}
    with psycopg.connect(host="postgresql", port=5432, user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"], dbname=os.environ["POSTGRES_DB"], connect_timeout=10, autocommit=True) as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            def set_memory(setting):
                if setting is not None:
                    cursor.execute("SELECT set_config('work_mem', %s, false)", (setting,)).fetchone()

            def execute(query_id, setting):
                set_memory(setting)
                cursor.execute(queries[query_id])
                return cursor.fetchall()

            def explain(query_id, setting):
                set_memory(setting)
                cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + queries[query_id])
                return cursor.fetchone()["QUERY PLAN"]

            cursor.execute("SHOW server_version")
            server_version = cursor.fetchone()["server_version"]
            cases = [("Q1", "default", None), ("Q2", "work_mem_4MB", "4MB"), ("Q2", "work_mem_64MB", "64MB")]
            return server_version, run_cases("postgresql", cases, execute, explain, reference)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("reference", "mongodb", "postgresql"))
    args = parser.parse_args()
    manifest = verify_source()
    if args.mode == "reference":
        build_reference(manifest)
        return
    reference = json.loads(REFERENCE.read_text())
    require(reference["source_sha256"] == manifest["sha256"], "La referencia pertenece a otro dataset.")
    runner = run_mongodb if args.mode == "mongodb" else run_postgresql
    server_version, cases = runner(reference)
    extension = "json" if args.mode == "mongodb" else "sql"
    hashes = {q: hashlib.sha256(Path(f"queries/{args.mode}/{q}.{extension}").read_bytes()).hexdigest() for q in ("Q1", "Q2")}
    report = {
        "engine": args.mode, "profile": PROFILE, "server_version": server_version,
        "source_sha256": manifest["sha256"], "query_sha256": hashes,
        "numeric_tolerance": {"absolute": str(ABS_TOL), "relative": str(REL_TOL)},
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "cases": cases, "status": "ok" if all(c["status"] == "ok" for c in cases) else "error",
    }
    output = Path(f"results/validation_{args.mode}_{PROFILE}.json")
    save(output, report)
    print(json.dumps({"status": report["status"], "report": str(output)}, indent=2))
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
