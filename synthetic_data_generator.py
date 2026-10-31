"""Genera envios sinteticos para comparar MongoDB con PostgreSQL JSONB."""

import argparse
import hashlib
import json
import platform
import random
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


MODEL_VERSION = "paqueteria-v1"
START = datetime(2025, 1, 1, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 10, 1, tzinfo=timezone.utc)
Q2_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CENTERS = (
    ("CD-01", "Centro Tuxtla", "sureste", "Chiapas", "Tuxtla Gutierrez"),
    ("CD-02", "Centro Oaxaca", "sur", "Oaxaca", "Oaxaca de Juarez"),
    ("CD-03", "Centro Puebla", "centro", "Puebla", "Puebla de Zaragoza"),
    ("CD-04", "Centro Mexico", "centro", "Ciudad de Mexico", "Ciudad de Mexico"),
    ("CD-05", "Centro Guadalajara", "occidente", "Jalisco", "Guadalajara"),
    ("CD-06", "Centro Monterrey", "norte", "Nuevo Leon", "Monterrey"),
)


def format_date(value):
    return value.strftime(DATE_FORMAT)


def parse_date(value):
    parsed = datetime.strptime(value, DATE_FORMAT).replace(tzinfo=timezone.utc)
    if format_date(parsed) != value or parsed.second != 0:
        raise ValueError("Fecha fuera del formato UTC canonico con segundos 00.")
    return parsed


def container_depth(value):
    """Cuenta contenedores; restar uno para expresar raiz=0."""
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return 0
    return 1 + max((container_depth(child) for child in children), default=0)


def random_date(rng, lower, upper):
    """Intervalo inclusivo, con resolucion de un minuto."""
    return lower + rng.randint(0, int((upper - lower) / MINUTE)) * MINUTE


def delivery_offset(rng):
    category = rng.choices(("anticipada", "puntual", "tardia"), (25, 35, 40), k=1)[0]
    if category == "anticipada":
        return -rng.randint(1, 360)
    if category == "tardia":
        return rng.randint(1, 720)
    return 0


def build_shipment(index, seed, rng):
    center_index = rng.randrange(len(CENTERS))
    center_id, center_name, region, origin_state, origin_city = CENTERS[center_index]
    destination = rng.choice([c for i, c in enumerate(CENTERS) if i != center_index])
    service = rng.choices(("estandar", "expres"), (70, 30), k=1)[0]
    state = rng.choices(("finalizado", "en_transito", "cancelado"), (75, 20, 5), k=1)[0]
    if state == "finalizado":
        # Compromiso maximo 5d6h y retraso maximo 12h: margen de seis dias.
        created = random_date(rng, START, CUTOFF - timedelta(days=6))
    elif state == "en_transito":
        created = random_date(rng, CUTOFF - timedelta(days=7), CUTOFF - MINUTE)
    else:
        created = random_date(rng, START, CUTOFF - MINUTE)

    packages = []
    deliveries = []
    cumulative_delay = 0
    package_count = 1 + index % 5
    pending_index = rng.randrange(package_count) if state == "en_transito" else None
    for package_index in range(package_count):
        days = rng.randint(3, 5) if service == "estandar" else rng.randint(1, 2)
        promise = created + timedelta(days=days, minutes=rng.randint(0, 360))
        actual = None
        if state == "cancelado":
            package_state = "cancelado"
        elif state == "finalizado":
            actual = promise + delivery_offset(rng) * MINUTE
            package_state = "entregado"
        else:
            candidate = promise + delivery_offset(rng) * MINUTE
            if package_index != pending_index and candidate <= CUTOFF:
                actual = candidate
                package_state = "entregado"
            else:
                package_state = "en_transito"
        if actual is not None:
            deliveries.append(actual)
            cumulative_delay += max(0, int((actual - promise) / MINUTE))
        packages.append({
            "paquete_id": f"PAQ-{index + 1:06d}-{package_index + 1:02d}",
            "peso_kg": rng.randint(100, 30000) / 1000,
            "dimensiones_cm": {
                "largo": rng.randint(5, 100),
                "ancho": rng.randint(5, 100),
                "alto": rng.randint(5, 100),
            },
            "estado": package_state,
            "fecha_compromiso": format_date(promise),
            "fecha_entrega": format_date(actual) if actual is not None else None,
        })
    if state == "finalizado":
        closed = max(deliveries)
    elif state == "cancelado":
        closed = random_date(rng, created, min(created + timedelta(days=1), CUTOFF))
    else:
        closed = None
    return {
        "envio_id": str(uuid5(NAMESPACE_URL, f"benchmark/envios/{seed}/{index}")),
        "fecha_creacion": format_date(created),
        "fecha_cierre": format_date(closed) if closed is not None else None,
        "estado": state,
        "servicio": service,
        "centro_distribucion": {"id": center_id, "nombre": center_name, "region": region},
        "origen": {"pais": "MX", "estado": origin_state, "ciudad": origin_city},
        "destino": {"pais": "MX", "estado": destination[3], "ciudad": destination[4]},
        "paquetes": packages,
        "retraso_acumulado_min": cumulative_delay if state == "finalizado" else None,
    }


def validate_shipment(shipment):
    """Recalcula desde las fechas serializadas; devuelve retrasos positivos."""
    created = parse_date(shipment["fecha_creacion"])
    if not START <= created < CUTOFF:
        raise ValueError("Creacion fuera del intervalo del modelo.")
    packages = shipment["paquetes"]
    if not 1 <= len(packages) <= 5:
        raise ValueError("Cantidad de paquetes fuera de 1..5.")
    if len({p["paquete_id"] for p in packages}) != len(packages):
        raise ValueError("Identificador de paquete repetido dentro del envio.")
    deliveries = []
    delays = []
    states = []
    for package in packages:
        promise = parse_date(package["fecha_compromiso"])
        if promise <= created:
            raise ValueError("Compromiso no posterior a la creacion.")
        if package["peso_kg"] <= 0 or any(v <= 0 for v in package["dimensiones_cm"].values()):
            raise ValueError("Peso o dimensiones no positivos.")
        package_state = package["estado"]
        states.append(package_state)
        if package_state == "entregado":
            actual = parse_date(package["fecha_entrega"])
            if not created <= actual <= CUTOFF:
                raise ValueError("Entrega fuera del intervalo permitido.")
            deliveries.append(actual)
            delay = max(0, int((actual - promise) / MINUTE))
            if delay > 0:
                delays.append(delay)
        elif package_state in ("en_transito", "cancelado"):
            if package["fecha_entrega"] is not None:
                raise ValueError("Paquete sin entregar con fecha real.")
        else:
            raise ValueError("Estado de paquete desconocido.")
    state = shipment["estado"]
    closed = shipment["fecha_cierre"]
    accumulated = shipment["retraso_acumulado_min"]
    if state == "finalizado":
        if any(s != "entregado" for s in states):
            raise ValueError("Envio finalizado con paquetes sin entregar.")
        if parse_date(closed) != max(deliveries):
            raise ValueError("Cierre distinto de la ultima entrega.")
        if type(accumulated) is not int or accumulated != sum(delays):
            raise ValueError("Retraso acumulado incorrecto.")
    elif state == "en_transito":
        if "en_transito" not in states or "cancelado" in states or closed is not None:
            raise ValueError("Estado de envio abierto incoherente.")
        if accumulated is not None:
            raise ValueError("Envio abierto con acumulado cerrado.")
    elif state == "cancelado":
        if any(s != "cancelado" for s in states) or not created <= parse_date(closed) <= CUTOFF:
            raise ValueError("Cancelacion incoherente.")
        if accumulated is not None:
            raise ValueError("Envio cancelado con acumulado.")
    else:
        raise ValueError("Estado de envio desconocido.")
    if container_depth(shipment) - 1 != 3:
        raise ValueError("Profundidad distinta de tres.")
    return delays


def generate(records, seed, output, manifest_path):
    output, manifest_path = Path(output), Path(manifest_path)
    if records < 1 or records % 5:
        raise ValueError("--records debe ser positivo y multiplo de 5 para promediar 3 paquetes.")
    if output.resolve() == manifest_path.resolve():
        raise ValueError("El dataset y el manifiesto deben tener rutas distintas.")
    if output.exists() or manifest_path.exists():
        raise FileExistsError("Ya existe una salida; no se sobrescribira. Revisa sus rutas.")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    rng, digest = random.Random(seed), hashlib.sha256()
    states, package_states, centers, services, center_services = (Counter() for _ in range(5))
    packages_total = matching_packages = matching_shipments = 0
    first_created = last_created = first_closed = last_closed = None
    temporary_paths = []
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=output.parent, delete=False) as stream:
            temporary_data = Path(stream.name)
            temporary_paths.append(temporary_data)
            for index in range(records):
                shipment = build_shipment(index, seed, rng)
                delays = validate_shipment(shipment)
                payload = (json.dumps(shipment, ensure_ascii=False, allow_nan=False,
                                      separators=(",", ":")) + "\n").encode("utf-8")
                stream.write(payload)
                digest.update(payload)
                packages_total += len(shipment["paquetes"])
                matching_packages += len(delays)
                state, closed = shipment["estado"], shipment["fecha_cierre"]
                matching_shipments += state == "finalizado" and format_date(Q2_START) <= closed < format_date(CUTOFF)
                states[state] += 1
                package_states.update(p["estado"] for p in shipment["paquetes"])
                center_id, service = shipment["centro_distribucion"]["id"], shipment["servicio"]
                centers[center_id] += 1
                services[service] += 1
                center_services[f"{center_id}/{service}"] += 1
                created = shipment["fecha_creacion"]
                first_created = created if first_created is None else min(first_created, created)
                last_created = created if last_created is None else max(last_created, created)
                if closed is not None:
                    first_closed = closed if first_closed is None else min(first_closed, closed)
                    last_closed = closed if last_closed is None else max(last_closed, closed)
                if (index + 1) % 50000 == 0:
                    print(f"Envios generados y comprobados: {index + 1:,}", flush=True)
        manifest = {
            "case": "paqueteria",
            "model_version": MODEL_VERSION,
            "dataset": output.as_posix(),
            "format": "jsonl/utf-8/LF",
            "seed": seed,
            "python_version": platform.python_version(),
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "records": records,
            "packages": packages_total,
            "average_packages_per_shipment": packages_total / records,
            "max_nested_container_depth": 3,
            "depth_convention": "root=0; each nested object or array adds one level",
            "observation_cutoff": format_date(CUTOFF),
            "minimum_creation_timestamp": first_created,
            "maximum_creation_timestamp": last_created,
            "minimum_close_timestamp": first_closed,
            "maximum_close_timestamp": last_closed,
            "q1_matching_packages": matching_packages,
            "q2_matching_shipments": matching_shipments,
            "q2_interval": {"inclusive_start": format_date(Q2_START), "exclusive_end": format_date(CUTOFF)},
            "shipments_by_state": dict(sorted(states.items())),
            "packages_by_state": dict(sorted(package_states.items())),
            "shipments_by_center": dict(sorted(centers.items())),
            "shipments_by_service": dict(sorted(services.items())),
            "shipments_by_center_and_service": dict(sorted(center_services.items())),
            "generation_assumptions": "docs/generacion_paqueteria.md",
            "size_bytes": temporary_data.stat().st_size,
            "sha256": digest.hexdigest(),
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=manifest_path.parent, delete=False) as stream:
            temporary_manifest = Path(stream.name)
            temporary_paths.append(temporary_manifest)
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        if output.exists() or manifest_path.exists():
            raise FileExistsError("Aparecio una salida existente; se cancela la publicacion.")
        temporary_data.replace(output)
        temporary_manifest.replace(manifest_path)
        return manifest
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=500000)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--output", type=Path, default=Path("data/envios.jsonl"))
    parser.add_argument("--manifest", type=Path, default=Path("results/paqueteria/dataset_manifest.json"))
    args = parser.parse_args()
    try:
        manifest = generate(args.records, args.seed, args.output, args.manifest)
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
