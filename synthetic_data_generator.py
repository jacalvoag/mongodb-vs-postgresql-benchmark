"""Genera las ordenes JSON del experimento MongoDB frente a PostgreSQL JSONB."""

import argparse
import hashlib
import json
import platform
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5


START = datetime(2025, 1, 1, tzinfo=timezone.utc)
END = datetime(2027, 1, 1, tzinfo=timezone.utc)
CUTOFF = "2026-01-01T00:00:00Z"
DATE_SECONDS = int((END - START).total_seconds())

LOCATIONS = (
    ("MX", "Chiapas", "Tuxtla Gutierrez", "29000", [-93.1167, 16.75]),
    ("US", "California", "Los Angeles", "90001", [-118.2437, 34.0522]),
    ("CA", "Ontario", "Toronto", "M5H", [-79.3832, 43.6532]),
    ("ES", "Madrid", "Madrid", "28001", [-3.7038, 40.4168]),
    ("CO", "Bogota DC", "Bogota", "110111", [-74.0721, 4.711]),
    ("AR", "Buenos Aires", "Buenos Aires", "C1000", [-58.3816, -34.6037]),
)
CATEGORIES = ("electronics", "home", "books", "sports", "clothing", "food")
BRANDS = ("brand_a", "brand_b", "brand_c", "brand_d")


def random_uuid(rng):
    return str(UUID(int=rng.getrandbits(128), version=4))


def container_depth(value):
    """Cuenta objetos y arreglos; el objeto raiz cuenta como nivel 1."""
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return 0
    return 1 + max((container_depth(child) for child in children), default=0)


def build_order(index, seed, rng):
    country, state, city, zip_code, coordinates = rng.choice(LOCATIONS)
    timestamp = START + timedelta(seconds=rng.randrange(DATE_SECONDS))
    items = []
    subtotal_cents = discount_cents = tax_cents = 0

    # Cada bloque de cinco ordenes contiene 1, 2, 3, 4 y 5 items.
    for _ in range(1 + index % 5):
        qty = rng.randint(1, 5)
        price_cents = rng.randint(100, 25000)
        discount_pct = rng.choice((0, 0, 0, 5, 10, 15))
        tax_pct = rng.choice((0, 8, 16))
        gross = qty * price_cents
        discount = (gross * discount_pct + 50) // 100
        tax = ((gross - discount) * tax_pct + 50) // 100

        items.append({
            "sku": f"SKU-{rng.randint(1, 10000):05d}",
            "category": rng.choice(CATEGORIES),
            "brand": rng.choice(BRANDS),
            "qty": qty,
            "price": price_cents / 100,
            "tax_pct": tax_pct,
            "discounts": [discount_pct] if discount_pct else [],
        })
        subtotal_cents += gross
        discount_cents += discount
        tax_cents += tax

    return {
        "order_id": str(uuid5(NAMESPACE_URL, f"benchmark/orders/{seed}/{index}")),
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "customer": {
            "id": f"C-{rng.randint(1, 100000):06d}",
            "tier": rng.choice(("bronze", "silver", "gold", "platinum")),
            "segment": rng.choice(("retail", "business")),
            "geo": {
                "country": country,
                "state": state,
                "city": city,
                "zip": zip_code,
                "coordinates": coordinates,
            },
        },
        "items": items,
        "payment": {
            "method": rng.choice(("card", "transfer", "wallet")),
            "status": rng.choices(
                ("completed", "pending", "cancelled"),
                weights=(75, 15, 10),
                k=1,
            )[0],
            "transaction_id": random_uuid(rng),
            "gateway_latency_ms": rng.randint(5, 500),
            "totals": {
                "subtotal": subtotal_cents / 100,
                "discount": discount_cents / 100,
                "tax": tax_cents / 100,
            },
            "final_total": (subtotal_cents - discount_cents + tax_cents) / 100,
        },
        "metadata": {
            "channel": rng.choice(("web", "mobile", "store")),
            "user_agent": rng.choice(("browser-desktop", "browser-mobile", "app")),
            "session_id": random_uuid(rng),
            "flags": rng.sample(("gift", "priority", "newsletter"), rng.randint(0, 2)),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=500000)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--output", type=Path, default=Path("data/orders.jsonl"))
    parser.add_argument("--manifest", type=Path, default=Path("results/dataset_manifest.json"))
    args = parser.parse_args()

    if args.records < 1 or args.records % 5:
        parser.error("--records debe ser positivo y multiplo de 5 para promediar 3 items.")
    if args.output.resolve() == args.manifest.resolve():
        parser.error("El dataset y el manifiesto deben tener rutas distintas.")
    if args.output.exists() or args.manifest.exists():
        parser.error("Ya existe una salida; utiliza otras rutas para no sobrescribirla.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    digest = hashlib.sha256()
    items_total = q1_items = q2_orders = max_depth = 0
    minimum_date = maximum_date = None
    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=args.output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            for index in range(args.records):
                order = build_order(index, args.seed, rng)
                payload = (
                    json.dumps(order, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                stream.write(payload)
                digest.update(payload)
                items_total += len(order["items"])
                q1_items += sum(item["price"] > 50 for item in order["items"])
                q2_orders += (
                    order["payment"]["status"] == "completed"
                    and order["timestamp"] >= CUTOFF
                )
                max_depth = max(max_depth, container_depth(order) - 1)
                date = order["timestamp"]
                minimum_date = date if minimum_date is None else min(minimum_date, date)
                maximum_date = date if maximum_date is None else max(maximum_date, date)
                if (index + 1) % 50000 == 0:
                    print(f"Ordenes generadas: {index + 1:,}", flush=True)
        temporary.replace(args.output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    manifest = {
        "dataset": args.output.as_posix(),
        "format": "jsonl/utf-8/LF",
        "seed": args.seed,
        "python_version": platform.python_version(),
        "records": args.records,
        "items": items_total,
        "average_items_per_order": items_total / args.records,
        "max_nested_container_depth": max_depth,
        "depth_convention": "root=0; each nested object or array adds one level",
        "minimum_timestamp": minimum_date,
        "maximum_timestamp": maximum_date,
        "q1_matching_items": q1_items,
        "q2_matching_orders": q2_orders,
        "size_bytes": args.output.stat().st_size,
        "sha256": digest.hexdigest(),
    }
    with args.manifest.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()