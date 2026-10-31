"""Carga y verifica el dataset del experimento; requiere un destino vacio."""

import argparse
import hashlib
import json
import os
import platform
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path


DATASET = Path("data/orders.jsonl")
MANIFEST = Path("results/dataset_manifest.json")
CUTOFF = "2026-01-01T00:00:00Z"
COUNT_KEYS = ("records", "items", "q1_matching_items", "q2_matching_orders")


def verify_source():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = {
        "records": 500000,
        "items": 1500000,
        "average_items_per_order": 3.0,
        "max_nested_container_depth": 3,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"El manifiesto no cumple {key}={value}.")
    if DATASET.stat().st_size != manifest["size_bytes"]:
        raise RuntimeError("El tamano del dataset no coincide con el manifiesto.")
    digest = hashlib.sha256()
    with DATASET.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != manifest["sha256"]:
        raise RuntimeError("El SHA-256 del dataset no coincide con el manifiesto.")
    print("Archivo fuente verificado: " + digest.hexdigest(), flush=True)
    return manifest


def source_rows():
    with DATASET.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            yield number, json.loads(line), line.rstrip("\n")


def check_counts(actual, manifest):
    for key in COUNT_KEYS:
        if actual[key] != manifest[key]:
            raise RuntimeError(
                f"Conteo incorrecto en {key}: {actual[key]} != {manifest[key]}"
            )


def load_mongodb(manifest):
    from pymongo import MongoClient

    with MongoClient(
        host="mongodb",
        port=27017,
        username=os.environ["MONGO_USER"],
        password=os.environ["MONGO_PASSWORD"],
        authSource="admin",
        serverSelectionTimeoutMS=10000,
    ) as client:
        client.admin.command("ping")
        collection = client[os.environ["MONGO_DB"]]["orders"]
        if collection.find_one({}, {"_id": 1}) is not None:
            raise RuntimeError("MongoDB orders ya contiene datos; carga cancelada.")
        specs = json.loads(Path("queries/mongodb/indexes.json").read_text())
        for spec in specs:
            collection.create_index(list(spec["key"].items()), name=spec["name"])

        batch = []
        inserted = 0
        for number, document, _ in source_rows():
            batch.append(document)
            if len(batch) == 1000:
                inserted += len(collection.insert_many(batch, ordered=True).inserted_ids)
                batch.clear()
                if number % 50000 == 0:
                    print(f"MongoDB: {inserted:,} ordenes cargadas", flush=True)
        if batch:
            inserted += len(collection.insert_many(batch, ordered=True).inserted_ids)
        if inserted != manifest["records"]:
            raise RuntimeError("El numero de documentos insertados es incorrecto.")

        result = next(collection.aggregate([{"$group": {
            "_id": None,
            "records": {"$sum": 1},
            "items": {"$sum": {"$size": "$items"}},
            "q1_matching_items": {"$sum": {"$size": {"$filter": {
                "input": "$items", "as": "item",
                "cond": {"$gt": ["$$item.price", 50]},
            }}}},
            "q2_matching_orders": {"$sum": {"$cond": [
                {"$and": [
                    {"$eq": ["$payment.status", "completed"]},
                    {"$gte": ["$timestamp", CUTOFF]},
                ]}, 1, 0,
            ]}},
        }}]))
        counts = {key: int(result[key]) for key in COUNT_KEYS}
        check_counts(counts, manifest)
        indexes = [
            {"name": index["name"], "key": dict(index["key"])}
            for index in collection.list_indexes()
        ]
        return {
            "server_version": client.server_info()["version"],
            "driver_version": version("pymongo"),
            "database": collection.database.name,
            "counts": counts,
            "indexes": indexes,
        }


def load_postgresql(manifest):
    import psycopg

    with psycopg.connect(
        host="postgresql",
        port=5432,
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        connect_timeout=10,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.orders')")
            if cursor.fetchone()[0] is not None:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM public.orders)")
                if cursor.fetchone()[0]:
                    raise RuntimeError("PostgreSQL orders ya contiene datos; carga cancelada.")
            cursor.execute(Path("queries/postgresql/setup.sql").read_text())
            inserted = 0
            with cursor.copy("COPY public.orders (id, data) FROM STDIN") as copy:
                for number, document, raw_json in source_rows():
                    copy.write_row((document["order_id"], raw_json))
                    inserted = number
                    if number % 50000 == 0:
                        print(f"PostgreSQL: {number:,} ordenes enviadas", flush=True)
            if inserted != manifest["records"]:
                raise RuntimeError("El numero de filas enviadas es incorrecto.")
            cursor.execute("ANALYZE public.orders")
            cursor.execute("""
                SELECT COUNT(*),
                       COALESCE(SUM(jsonb_array_length(data->'items')), 0),
                       COALESCE(SUM((
                           SELECT COUNT(*)
                           FROM jsonb_array_elements(data->'items') AS item
                           WHERE (item->>'price')::numeric > 50
                       )), 0),
                       COUNT(*) FILTER (
                           WHERE data->'payment'->>'status' = 'completed'
                           AND (data->>'timestamp')::timestamptz >= %s::timestamptz
                       )
                FROM public.orders
            """, (CUTOFF,))
            counts = dict(zip(COUNT_KEYS, map(int, cursor.fetchone())))
            check_counts(counts, manifest)
            cursor.execute("SHOW server_version")
            server_version = cursor.fetchone()[0]
            cursor.execute("""
                SELECT indexname, indexdef FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'orders'
                ORDER BY indexname
            """)
            indexes = [
                {"name": name, "definition": definition}
                for name, definition in cursor.fetchall()
            ]
        result = {
            "server_version": server_version,
            "driver_version": version("psycopg"),
            "database": os.environ["POSTGRES_DB"],
            "counts": counts,
            "indexes": indexes,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=("mongodb", "postgresql"))
    args = parser.parse_args()
    output = Path(f"results/load_{args.engine}.json")
    if output.exists():
        parser.error(f"Ya existe {output}; no se repetira la carga.")
    manifest = verify_source()
    loader = load_mongodb if args.engine == "mongodb" else load_postgresql
    result = loader(manifest)
    result.update({
        "engine": args.engine,
        "status": "ok",
        "source_sha256": manifest["sha256"],
        "python_version": platform.python_version(),
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()