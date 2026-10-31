"""Carga y comprueba paqueteria; requiere un destino envios vacio."""

import argparse
import hashlib
import json
import os
import platform
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path


DATASET = Path("data/envios.jsonl")
MANIFEST = Path("results/paqueteria/dataset_manifest.json")
Q2_START = "2026-01-01T00:00:00Z"
CUTOFF = "2026-10-01T00:00:00Z"
COUNT_KEYS = ("records", "packages", "q1_matching_packages", "q2_matching_shipments")

# Las fechas son cadenas UTC canonicas. Entrega > compromiso equivale a
# retraso positivo; aqui solo contamos seleccionados, sin medir Q1/Q2.
POSTGRESQL_COUNTS_SQL = """
    SELECT COUNT(*),
           COALESCE(SUM(jsonb_array_length(data->'paquetes')), 0),
           COALESCE(SUM((
               SELECT COUNT(*)
               FROM jsonb_array_elements(data->'paquetes') AS package
               WHERE package->>'estado' = 'entregado'
                 AND package->>'fecha_entrega' > package->>'fecha_compromiso'
           )), 0),
           COUNT(*) FILTER (
               WHERE data->>'estado' = 'finalizado'
                 AND data->>'fecha_cierre' >= %s
                 AND data->>'fecha_cierre' < %s
           )
    FROM public.envios
"""


def mongodb_counts_pipeline():
    return [{"$group": {
        "_id": None,
        "records": {"$sum": 1},
        "packages": {"$sum": {"$size": "$paquetes"}},
        "q1_matching_packages": {"$sum": {"$size": {"$filter": {
            "input": "$paquetes", "as": "package",
            "cond": {"$and": [
                {"$eq": ["$$package.estado", "entregado"]},
                {"$gt": ["$$package.fecha_entrega", "$$package.fecha_compromiso"]},
            ]},
        }}}},
        "q2_matching_shipments": {"$sum": {"$cond": [
            {"$and": [
                {"$eq": ["$estado", "finalizado"]},
                {"$gte": ["$fecha_cierre", Q2_START]},
                {"$lt": ["$fecha_cierre", CUTOFF]},
            ]}, 1, 0,
        ]}},
    }}]


def verify_source(dataset=DATASET, manifest_path=MANIFEST):
    dataset, manifest_path = Path(dataset), Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {
        "case": "paqueteria",
        "model_version": "paqueteria-v1",
        "format": "jsonl/utf-8/LF",
        "seed": 2026,
        "records": 500000,
        "packages": 1500000,
        "average_packages_per_shipment": 3.0,
        "max_nested_container_depth": 3,
        "observation_cutoff": CUTOFF,
        "q2_interval": {"inclusive_start": Q2_START, "exclusive_end": CUTOFF},
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"El manifiesto no cumple {key}={value}.")
    if manifest.get("dataset") != DATASET.as_posix():
        raise RuntimeError("El manifiesto no corresponde a data/envios.jsonl.")
    if dataset.stat().st_size != manifest["size_bytes"]:
        raise RuntimeError("El tamano del dataset no coincide con el manifiesto.")
    digest = hashlib.sha256()
    with dataset.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != manifest["sha256"]:
        raise RuntimeError("El SHA-256 del dataset no coincide con el manifiesto.")
    print("Archivo fuente verificado: " + digest.hexdigest(), flush=True)
    return manifest


def source_rows(dataset=DATASET):
    with Path(dataset).open(encoding="utf-8", newline="\n") as stream:
        for number, line in enumerate(stream, 1):
            document = json.loads(line)
            yield number, document, line.rstrip("\n")


def check_counts(actual, manifest):
    for key in COUNT_KEYS:
        if actual.get(key) != manifest.get(key) or key not in actual or key not in manifest:
            raise RuntimeError(
                f"Conteo incorrecto en {key}: {actual.get(key)} != {manifest.get(key)}"
            )


def create_mongodb_indexes(collection):
    specs = json.loads(Path("queries/mongodb/indexes.json").read_text(encoding="utf-8"))
    for spec in specs:
        options = {key: value for key, value in spec.items() if key != "key"}
        collection.create_index(list(spec["key"].items()), **options)


def load_mongodb(manifest):
    from pymongo import MongoClient

    with MongoClient(
        host="mongodb", port=27017,
        username=os.environ["MONGO_USER"], password=os.environ["MONGO_PASSWORD"],
        authSource="admin", serverSelectionTimeoutMS=10000,
        appname="paqueteria_data_loader",
    ) as client:
        client.admin.command("ping")
        collection = client[os.environ["MONGO_DB"]]["envios"]
        if collection.find_one({}, {"_id": 1}) is not None:
            raise RuntimeError("MongoDB envios ya contiene datos; carga cancelada.")
        create_mongodb_indexes(collection)
        batch, inserted = [], 0
        for number, document, _ in source_rows():
            batch.append(document)
            if len(batch) == 1000:
                inserted += len(collection.insert_many(batch, ordered=True).inserted_ids)
                batch.clear()
                if number % 50000 == 0:
                    print(f"MongoDB: {inserted:,} envios cargados", flush=True)
        if batch:
            inserted += len(collection.insert_many(batch, ordered=True).inserted_ids)
        if inserted != manifest["records"]:
            raise RuntimeError("El numero de documentos insertados es incorrecto.")
        result = next(collection.aggregate(mongodb_counts_pipeline()), None)
        if result is None:
            raise RuntimeError("MongoDB no devolvio los conteos de verificacion.")
        counts = {key: int(result[key]) for key in COUNT_KEYS}
        check_counts(counts, manifest)
        indexes = [{"name": index["name"], "key": dict(index["key"]),
                    "unique": bool(index.get("unique", index["name"] == "_id_"))}
                   for index in collection.list_indexes()]
        expected = next((i for i in indexes if i["name"] == "envios_envio_id_idx"), None)
        if expected is None or expected["key"] != {"envio_id": 1} or not expected["unique"]:
            raise RuntimeError("No se comprobo el indice unico de envio_id.")
        return {
            "server_version": client.server_info()["version"],
            "driver_version": version("pymongo"),
            "database": collection.database.name,
            "collection": collection.name,
            "counts": counts,
            "indexes": indexes,
        }


def load_postgresql(manifest):
    import psycopg

    with psycopg.connect(
        host="postgresql", port=5432,
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"], connect_timeout=10,
        application_name="paqueteria_data_loader",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.envios')")
            if cursor.fetchone()[0] is not None:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM public.envios)")
                if cursor.fetchone()[0]:
                    raise RuntimeError("PostgreSQL envios ya contiene datos; carga cancelada.")
            cursor.execute(Path("queries/postgresql/setup.sql").read_text(encoding="utf-8"))
            inserted = 0
            with cursor.copy("COPY public.envios (id, data) FROM STDIN") as copy:
                for number, document, raw_json in source_rows():
                    copy.write_row((document["envio_id"], raw_json))
                    inserted = number
                    if number % 50000 == 0:
                        print(f"PostgreSQL: {number:,} envios enviados", flush=True)
            if inserted != manifest["records"]:
                raise RuntimeError("El numero de filas enviadas es incorrecto.")
            cursor.execute("ANALYZE public.envios")
            cursor.execute(POSTGRESQL_COUNTS_SQL, (Q2_START, CUTOFF))
            counts = dict(zip(COUNT_KEYS, map(int, cursor.fetchone())))
            check_counts(counts, manifest)
            cursor.execute("SHOW server_version")
            server_version = cursor.fetchone()[0]
            cursor.execute("""
                SELECT indexname, indexdef FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'envios'
                ORDER BY indexname
            """)
            indexes = [{"name": name, "definition": definition}
                       for name, definition in cursor.fetchall()]
        result = {
            "server_version": server_version,
            "driver_version": version("psycopg"),
            "database": os.environ["POSTGRES_DB"],
            "table": "public.envios",
            "counts": counts,
            "indexes": indexes,
        }
    # El contexto de conexion confirma la transaccion antes de devolver exito.
    return result


def run_load(engine):
    output = Path(f"results/paqueteria/load_{engine}.json")
    if output.exists():
        raise RuntimeError(f"Ya existe {output}; no se repetira la carga.")
    manifest = verify_source()
    loader = load_mongodb if engine == "mongodb" else load_postgresql
    result = loader(manifest)
    result.update({
        "case": "paqueteria",
        "model_version": manifest["model_version"],
        "engine": engine,
        "status": "ok",
        "dataset": manifest["dataset"],
        "source_records": manifest["records"],
        "source_sha256": manifest["sha256"],
        "python_version": platform.python_version(),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=("mongodb", "postgresql"))
    args = parser.parse_args()
    try:
        run_load(args.engine)
    except (RuntimeError, FileExistsError, FileNotFoundError) as error:
        parser.exit(1, f"Carga detenida: {error}\n")


if __name__ == "__main__":
    main()
