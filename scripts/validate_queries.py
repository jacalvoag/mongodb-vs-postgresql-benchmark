"""Valida Q1/Q2 de paqueteria y guarda planes; no realiza el benchmark."""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

from load_dataset import DATASET, check_counts, verify_source
from parcel_reference import (
    ABS_TOL, REL_TOL, CASE, MODEL, REFERENCE_SCHEMA, build_reference,
    require, validate_q1, validate_q2, validate_reference,
)


ROOT = Path("results/paqueteria")
REFERENCE = ROOT / "query_reference.json"
PROFILE = os.environ.get("PROFILE_ID", "PROFILE_A_BASELINE")
CASES = {
    "mongodb": [("Q1", "default", None),
                ("Q2", "allowDiskUse_false", False),
                ("Q2", "allowDiskUse_true", True)],
    "postgresql": [("Q1", "default", None),
                   ("Q2", "work_mem_4MB", "4MB"),
                   ("Q2", "work_mem_64MB", "64MB")],
}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str, allow_nan=False)
        stream.write("\n")


def case_paths(engine, query_id, variant):
    name = f"{engine}_{query_id}_{variant}.json"
    return {
        "results_file": ROOT / "query_results" / PROFILE / name,
        "plan_file": Path("evidence/paqueteria/plans") / PROFILE / name,
        "error_file": Path("evidence/paqueteria/errors") / PROFILE / name,
    }


def preflight_outputs(engine):
    paths = [ROOT / f"validation_{engine}_{PROFILE}.json"]
    for query_id, variant, _ in CASES[engine]:
        paths.extend(case_paths(engine, query_id, variant).values())
    for path in paths:
        require(not path.exists(), f"Ya existe {path}; no se sobrescribe ni se repite la validacion.")


def verify_loaded_source(engine, manifest):
    path = ROOT / f"load_{engine}.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    require(loaded.get("status") == "ok" and loaded.get("engine") == engine,
            f"No hay una carga correcta registrada para {engine}.")
    require(loaded.get("case") == CASE and loaded.get("model_version") == MODEL,
            "La carga registrada pertenece a otro caso o modelo.")
    require(loaded.get("source_sha256") == manifest["sha256"], "La carga pertenece a otro dataset.")
    check_counts(loaded["counts"], manifest)
    field, expected = ("collection", "envios") if engine == "mongodb" else ("table", "public.envios")
    require(loaded.get(field) == expected, "La carga registrada tiene un destino diferente.")
    return loaded


def normalize(rows):
    # PostgreSQL devuelve numeric como Decimal; MongoDB devuelve int/double.
    # Las columnas tienen los mismos nombres en SQL y en los pipelines.
    return [{key: str(value) if isinstance(value, Decimal) else value
             for key, value in row.items()} for row in rows]


def run_cases(engine, execute, explain, reference):
    results = []
    for query_id, variant, setting in CASES[engine]:
        paths = case_paths(engine, query_id, variant)
        result = {"query_id": query_id, "variant": variant, "setting": setting}
        phase = "execute"
        print(f"Validando {engine} {query_id} {variant}", flush=True)
        try:
            rows = normalize(execute(query_id, setting))
            save(paths["results_file"], rows)
            phase = "compare_with_reference"
            validator = validate_q1 if query_id == "Q1" else validate_q2
            validator(rows, reference)
            phase = "explain"
            save(paths["plan_file"], explain(query_id, setting))
            result.update({"status": "ok", "rows": len(rows),
                           "results_file": str(paths["results_file"]),
                           "plan_file": str(paths["plan_file"])})
        except Exception as error:
            result.update({"status": "error", "phase": phase,
                           "error_type": type(error).__name__, "message": str(error),
                           "error_file": str(paths["error_file"])})
            save(paths["error_file"], result)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return results


def run_mongodb(reference):
    from pymongo import MongoClient
    from bson import json_util

    queries = {q: json.loads(Path(f"queries/mongodb/{q}.json").read_text(encoding="utf-8"))
               for q in ("Q1", "Q2")}
    with MongoClient(host="mongodb", port=27017, username=os.environ["MONGO_USER"],
                     password=os.environ["MONGO_PASSWORD"], authSource="admin",
                     serverSelectionTimeoutMS=10000, appname="paqueteria_query_validator") as client:
        db = client[os.environ["MONGO_DB"]]
        db.command("ping")
        parameters = client.admin.command({"getParameter": 1, "allowDiskUseByDefault": 1})

        def execute(query_id, setting):
            options = {} if setting is None else {"allowDiskUse": setting}
            return list(db["envios"].aggregate(queries[query_id], **options))

        def explain(query_id, setting):
            command = {"aggregate": "envios", "pipeline": queries[query_id], "cursor": {}}
            if setting is not None:
                command["allowDiskUse"] = setting
            plan = db.command({"explain": command, "verbosity": "executionStats"})
            return json.loads(json_util.dumps(plan))

        metadata = {"server_version": client.server_info()["version"],
                    "driver_version": version("pymongo"), "database": db.name,
                    "collection": "envios",
                    "server_settings": {"allowDiskUseByDefault": parameters["allowDiskUseByDefault"]}}
        return metadata, run_cases("mongodb", execute, explain, reference)


def run_postgresql(reference):
    import psycopg
    from psycopg.rows import dict_row

    queries = {q: Path(f"queries/postgresql/{q}.sql").read_text(encoding="utf-8")
               for q in ("Q1", "Q2")}
    with psycopg.connect(host="postgresql", port=5432, user=os.environ["POSTGRES_USER"],
                         password=os.environ["POSTGRES_PASSWORD"], dbname=os.environ["POSTGRES_DB"],
                         connect_timeout=10, autocommit=True,
                         application_name="paqueteria_query_validator") as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            def set_memory(setting):
                if setting is None:
                    cursor.execute("RESET work_mem")
                else:
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
            cursor.execute("""
                SELECT current_setting('work_mem') AS work_mem,
                       current_setting('shared_buffers') AS shared_buffers,
                       current_setting('max_parallel_workers_per_gather') AS max_parallel_workers_per_gather,
                       current_setting('jit') AS jit
            """)
            metadata = {"server_version": server_version, "driver_version": version("psycopg"),
                        "database": os.environ["POSTGRES_DB"], "table": "public.envios",
                        "server_settings": cursor.fetchone()}
            return metadata, run_cases("postgresql", execute, explain, reference)


def run_mode(mode):
    if mode == "reference":
        require(not REFERENCE.exists(), f"Ya existe {REFERENCE}; se conserva la referencia.")
    else:
        preflight_outputs(mode)
    manifest = verify_source()
    if mode == "reference":
        reference = build_reference(manifest, DATASET)
        save(REFERENCE, reference)
        summary = {"status": "ok", "reference": str(REFERENCE),
                   "q1_groups": len(reference["q1"]),
                   "q2_candidates": reference["q2"]["candidate_count"],
                   "q2_required": len(reference["q2"]["required"]),
                   "q2_boundary_candidates": len(reference["q2"]["boundary"]),
                   "q2_cutoff_min": reference["q2"]["cutoff_min"]}
        print(json.dumps(summary, indent=2), flush=True)
        return summary
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    validate_reference(reference, manifest)
    loaded = verify_loaded_source(mode, manifest)
    runner = run_mongodb if mode == "mongodb" else run_postgresql
    metadata, cases = runner(reference)
    extension = "json" if mode == "mongodb" else "sql"
    hashes = {q: file_hash(f"queries/{mode}/{q}.{extension}") for q in ("Q1", "Q2")}
    report = {
        "case": CASE, "model_version": MODEL, "reference_schema": REFERENCE_SCHEMA,
        "engine": mode, "profile": PROFILE, **metadata,
        "source_records": manifest["records"], "source_sha256": manifest["sha256"],
        "loaded_database": loaded["database"],
        "reference_file": str(REFERENCE), "reference_sha256": file_hash(REFERENCE),
        "query_sha256": hashes,
        "numeric_tolerance": {"scope": "Q1.retraso_promedio_min only",
                              "absolute": str(ABS_TOL), "relative": str(REL_TOL)},
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "cases": cases, "status": "ok" if all(c["status"] == "ok" for c in cases) else "error",
    }
    output = ROOT / f"validation_{mode}_{PROFILE}.json"
    save(output, report)
    print(json.dumps({"status": report["status"], "report": str(output)}, indent=2), flush=True)
    if report["status"] != "ok":
        raise SystemExit(1)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("reference", "mongodb", "postgresql"))
    args = parser.parse_args()
    try:
        run_mode(args.mode)
    except Exception as error:
        parser.exit(1, f"Validacion detenida: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
