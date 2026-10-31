"""Referencia independiente de Q1/Q2, calculada desde las fechas del JSONL."""

import heapq
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


CASE = "paqueteria"
MODEL = "paqueteria-v1"
REFERENCE_SCHEMA = "paqueteria-q1-q2-v1"
START = "2025-01-01T00:00:00Z"
Q2_START = "2026-01-01T00:00:00Z"
CUTOFF = "2026-10-01T00:00:00Z"
ABS_TOL = Decimal("0.000001")
REL_TOL = Decimal("0.000000000001")
Q1_FIELDS = {"centro_id", "servicio", "paquetes_retrasados", "retraso_total_min", "retraso_promedio_min"}
Q2_FIELDS = {"envio_id", "centro_id", "servicio", "fecha_cierre", "retraso_acumulado_min"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def utc_date(value):
    require(isinstance(value, str), "La fecha debe ser una cadena UTC.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0,
            "La fecha debe estar en UTC.")
    require(parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value and parsed.second == 0,
            "La fecha debe usar formato canonico UTC y segundos 00.")
    return parsed


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise RuntimeError(f"Valor no numerico: {value!r}") from error
    require(result.is_finite(), "No se aceptan NaN o infinitos.")
    return result


class TopKReference:
    """Conserva los superiores al corte y todos sus posibles empates."""

    def __init__(self, limit):
        require(limit > 0, "El limite Top-K debe ser positivo.")
        self.limit = limit
        self.heap = []
        self.rows = {}
        self.candidate_count = 0

    def add(self, row):
        self.candidate_count += 1
        score = row["retraso_acumulado_min"]
        old_cutoff = self.heap[0] if len(self.heap) == self.limit else None
        if old_cutoff is not None and score < old_cutoff:
            return
        require(row["envio_id"] not in self.rows, "Envio repetido entre candidatos Top-K.")
        self.rows[row["envio_id"]] = row
        if len(self.heap) < self.limit:
            heapq.heappush(self.heap, score)
        elif score > self.heap[0]:
            heapq.heapreplace(self.heap, score)
        if old_cutoff is not None and self.heap[0] > old_cutoff:
            cutoff = self.heap[0]
            self.rows = {key: value for key, value in self.rows.items()
                         if value["retraso_acumulado_min"] >= cutoff}

    def finish(self):
        require(len(self.heap) == self.limit, "No hay suficientes envios para el Top-K.")
        cutoff = self.heap[0]
        # Antes de llenar el heap pudieron conservarse valores menores al corte.
        required = {key: value for key, value in sorted(self.rows.items())
                    if value["retraso_acumulado_min"] > cutoff}
        boundary = {key: value for key, value in sorted(self.rows.items())
                    if value["retraso_acumulado_min"] == cutoff}
        return {"limit": self.limit, "candidate_count": self.candidate_count,
                "cutoff_min": cutoff, "required": required, "boundary": boundary}


def inspect_shipment(shipment):
    """Recalcula retrasos y comprueba estados/cierre sin importar el generador."""
    created = utc_date(shipment["fecha_creacion"])
    cutoff = utc_date(CUTOFF)
    require(utc_date(START) <= created < cutoff, "Creacion fuera del intervalo.")
    packages = shipment["paquetes"]
    require(isinstance(packages, list) and 1 <= len(packages) <= 5, "Arreglo de paquetes invalido.")
    delays, deliveries, states = [], [], []
    for package in packages:
        promise = utc_date(package["fecha_compromiso"])
        require(promise > created, "Compromiso anterior o igual a la creacion.")
        state = package["estado"]
        states.append(state)
        if state == "entregado":
            delivered = utc_date(package["fecha_entrega"])
            require(created <= delivered <= cutoff, "Entrega fuera del intervalo permitido.")
            deliveries.append(delivered)
            # La precision de un minuto es parte del contrato del dataset.
            seconds = int((delivered - promise).total_seconds())
            require(seconds % 60 == 0, "Retraso con fraccion de minuto.")
            if seconds > 0:
                delays.append(seconds // 60)
        else:
            require(state in ("en_transito", "cancelado"), "Estado de paquete desconocido.")
            require(package["fecha_entrega"] is None, "Paquete sin entregar con fecha real.")
    state = shipment["estado"]
    closed, accumulated = shipment["fecha_cierre"], shipment["retraso_acumulado_min"]
    candidate = None
    if state == "finalizado":
        require(all(s == "entregado" for s in states), "Envio finalizado con paquetes pendientes.")
        require(utc_date(closed) == max(deliveries), "Cierre distinto de la ultima entrega.")
        require(type(accumulated) is int and accumulated == sum(delays), "Acumulado guardado incorrecto.")
        if Q2_START <= closed < CUTOFF:
            candidate = {
                "envio_id": shipment["envio_id"],
                "centro_id": shipment["centro_distribucion"]["id"],
                "servicio": shipment["servicio"],
                "fecha_cierre": closed,
                # Se usa el valor recalculado, despues de comprobar el guardado.
                "retraso_acumulado_min": sum(delays),
            }
    elif state == "en_transito":
        require("en_transito" in states and "cancelado" not in states, "Envio abierto incoherente.")
        require(closed is None and accumulated is None, "Envio abierto con cierre o acumulado.")
    elif state == "cancelado":
        require(all(s == "cancelado" for s in states), "Cancelacion parcial no permitida.")
        require(created <= utc_date(closed) <= cutoff, "Fecha de cancelacion invalida.")
        require(accumulated is None, "Envio cancelado con acumulado.")
    else:
        raise RuntimeError("Estado de envio desconocido.")
    return delays, candidate


def build_reference(manifest, dataset, limit=1000):
    groups = defaultdict(lambda: [0, 0])
    top = TopKReference(limit)
    records = packages = matching_packages = 0
    with Path(dataset).open(encoding="utf-8") as stream:
        for records, line in enumerate(stream, 1):
            try:
                shipment = json.loads(line)
                delays, candidate = inspect_shipment(shipment)
            except (KeyError, ValueError, TypeError, RuntimeError) as error:
                raise RuntimeError(f"Documento {records}: {error}") from error
            packages += len(shipment["paquetes"])
            matching_packages += len(delays)
            if delays:
                key = (shipment["centro_distribucion"]["id"], shipment["servicio"])
                groups[key][0] += len(delays)
                groups[key][1] += sum(delays)
            if candidate is not None:
                top.add(candidate)
            if records % 50000 == 0:
                print(f"Referencia: {records:,} envios procesados", flush=True)
    counts = {"records": records, "packages": packages,
              "q1_matching_packages": matching_packages,
              "q2_matching_shipments": top.candidate_count}
    for key, value in counts.items():
        require(value == manifest[key], f"Referencia y manifiesto difieren en {key}.")
    return {
        "case": CASE, "model_version": MODEL, "reference_schema": REFERENCE_SCHEMA,
        "source_sha256": manifest["sha256"], "source_records": records,
        "observation_cutoff": CUTOFF,
        "q2_interval": {"inclusive_start": Q2_START, "exclusive_end": CUTOFF},
        "counts": counts,
        "q1": [{"centro_id": center, "servicio": service,
                "paquetes_retrasados": count, "retraso_total_min": total,
                "retraso_promedio_min": str(Decimal(total) / count)}
               for (center, service), (count, total) in sorted(groups.items())],
        "q2": top.finish(),
    }


def validate_reference(reference, manifest):
    require(reference.get("case") == CASE and reference.get("model_version") == MODEL,
            "La referencia pertenece a otro caso o modelo.")
    require(reference.get("reference_schema") == REFERENCE_SCHEMA, "Esquema de referencia incorrecto.")
    require(reference.get("source_sha256") == manifest["sha256"], "La referencia pertenece a otro dataset.")
    require(reference.get("source_records") == manifest["records"], "Numero de envios de referencia incorrecto.")
    require(reference.get("q2_interval") == manifest["q2_interval"], "Periodo de referencia incorrecto.")
    require(reference["q2"]["limit"] == 1000, "La referencia debe corresponder al Top-1000.")
    for key in ("records", "packages", "q1_matching_packages", "q2_matching_shipments"):
        require(reference["counts"][key] == manifest[key], f"Conteo de referencia incorrecto: {key}")


def validate_q1(rows, reference):
    expected = {(r["centro_id"], r["servicio"]): r for r in reference["q1"]}
    for row in rows:
        require(set(row) == Q1_FIELDS, "Q1 devuelve campos diferentes de los definidos.")
    actual = {(r["centro_id"], r["servicio"]): r for r in rows}
    require(len(actual) == len(rows), "Q1 devuelve grupos duplicados.")
    require(actual.keys() == expected.keys(), "Los grupos de Q1 son diferentes.")
    for key, target in expected.items():
        row = actual[key]
        require(number(row["paquetes_retrasados"]) == target["paquetes_retrasados"], f"Conteo Q1 incorrecto: {key}")
        total = Decimal(target["retraso_total_min"])
        require(number(row["retraso_total_min"]) == total, f"Suma Q1 incorrecta: {key}")
        average = total / target["paquetes_retrasados"]
        tolerance = max(ABS_TOL, REL_TOL * abs(average))
        require(abs(number(row["retraso_promedio_min"]) - average) <= tolerance,
                f"Promedio Q1 incorrecto: {key}")


def validate_q2(rows, reference):
    target = reference["q2"]
    require(len(rows) == target["limit"], "Q2 devuelve una cantidad distinta del Top-K.")
    allowed = {**target["required"], **target["boundary"]}
    seen = set()
    previous = Decimal("Infinity")
    for row in rows:
        require(set(row) == Q2_FIELDS, "Q2 devuelve campos diferentes de los definidos.")
        shipment_id = row["envio_id"]
        require(shipment_id not in seen and shipment_id in allowed,
                f"Envio Q2 duplicado o fuera del Top-K: {shipment_id}")
        seen.add(shipment_id)
        expected = allowed[shipment_id]
        for key in ("centro_id", "servicio", "fecha_cierre"):
            require(row[key] == expected[key], f"Campo {key} de Q2 incorrecto: {shipment_id}")
        score = number(row["retraso_acumulado_min"])
        require(score == expected["retraso_acumulado_min"], f"Retraso Q2 incorrecto: {shipment_id}")
        require(score <= previous, "Q2 no esta ordenada de forma descendente.")
        previous = score
    require(set(target["required"]).issubset(seen), "Q2 omite envios superiores al corte.")
