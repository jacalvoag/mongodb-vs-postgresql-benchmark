#!/usr/bin/env bash
# Matriz de paqueteria; por defecto SOLO muestra el plan.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

usage() {
  cat <<'HELP'
Uso: bash run_benchmarks.sh [--plan|--run] [filtros]
Filtros: --engine postgresql|mongodb
         --profile PROFILE_A_BASELINE|PROFILE_B_MEDIUM|PROFILE_C_SEVERE
         --query Q1|Q2  --concurrency 1|10|50  --variant nombre
Sin --run no se inicia ninguna consulta del experimento.
HELP
}
mode=plan
filters=()
while (($#)); do
  case "$1" in
    --plan) mode=plan; shift ;;
    --run) mode=run; shift ;;
    --engine|--profile|--query|--concurrency|--variant)
      (($# >= 2)) || { usage; exit 2; }
      filters+=("$1" "$2"); shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

windows=false
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    windows=true
    # Conservar /work y la orden Linux de wsl.exe; evitar conversion MSYS.
    export MSYS_NO_PATHCONV=1
    export MSYS2_ARG_CONV_EXCL='*'
    native_root="$(pwd -W 2>/dev/null || cygpath -m "$PWD")"
    ;;
  *) native_root="$PWD" ;;
esac

meta() {
  local mount="type=bind,source=$native_root,target=/work"
  case "$1" in plan|tasks|audit) mount+=",readonly" ;; esac
  docker run --rm --pull=never --network none --mount "$mount" --workdir /work \
    python:3.14.10-slim python -B scripts/matrix.py "$@"
}
dc() {
  local profile="$1"; shift
  local db_cpus db_memory
  case "$profile" in
    PROFILE_A_BASELINE) db_cpus=4; db_memory=4096m ;;
    PROFILE_B_MEDIUM) db_cpus=2; db_memory=2048m ;;
    PROFILE_C_SEVERE) db_cpus=1; db_memory=1024m ;;
    *) echo "Perfil desconocido: $profile" >&2; return 2 ;;
  esac
  # La sesion de PowerShell puede heredar A; cada llamada fija el perfil elegido.
  # Las versiones de imagen proceden de .env, no de variables heredadas.
  env -u MONGO_IMAGE -u POSTGRES_IMAGE -u PYTHON_IMAGE -u CADVISOR_IMAGE \
    -u NODE_EXPORTER_IMAGE -u PROMETHEUS_IMAGE \
    COMPOSE_PROJECT_NAME=mongodb-postgresql-benchmark \
    PROFILE_ID="$profile" DB_CPUS="$db_cpus" DB_MEMORY="$db_memory" \
    docker compose -f docker-compose.yml -f compose.runner.yml --env-file .env \
    --env-file "config/profiles/$profile.env" "$@"
}
flush_cache() {
  if "$windows"; then
    wsl.exe -d docker-desktop -u root -- sh -c 'sync && echo 3 > /proc/sys/vm/drop_caches && echo CACHE_LINUX_OK'
  elif [[ "$(id -u)" == 0 ]]; then
    sh -c 'sync && echo 3 > /proc/sys/vm/drop_caches && echo CACHE_LINUX_OK'
  else
    sudo -n sh -c 'sync && echo 3 > /proc/sys/vm/drop_caches && echo CACHE_LINUX_OK'
  fi
}

meta plan "${filters[@]}"
[[ "$mode" == run ]] || exit 0
mkdir -p evidence/paqueteria
if ! mkdir evidence/paqueteria/matrix_lock 2>/dev/null; then
  echo 'Existe evidence/paqueteria/matrix_lock. Comprueba que no haya otro controlador antes de continuar.' >&2
  exit 1
fi
cleanup() { rmdir evidence/paqueteria/matrix_lock 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

meta init "${filters[@]}"
meta tasks "${filters[@]}" > results/paqueteria/matrix_v1/pending.tsv
if [[ ! -s results/paqueteria/matrix_v1/pending.tsv ]]; then
  echo 'El bloque seleccionado ya esta registrado. No se ejecutaron consultas.'
  exit 0
fi
# Compose run podria construir automaticamente si falta la imagen del servicio.
# Acreditar su existencia mantiene esta etapa libre de construcciones.
if ! docker image inspect mongodb-postgresql-benchmark-benchmark-runner:latest >/dev/null 2>&1; then
  echo 'Falta la imagen local de benchmark-runner. Preparala con el procedimiento del ejecutor antes de medir.' >&2
  exit 1
fi
monitors_started=false
while IFS=$'\t' read -r engine profile query variant concurrency repetition run_id <&3; do
  [[ -n "$run_id" ]] || continue
  echo "Escenario: $engine / $profile / $query / $variant / c$concurrency / repeticion $repetition"
  dc "$profile" --profile tools config --quiet
  if ! "$monitors_started"; then
    dc "$profile" up -d --pull never --no-build --no-deps cadvisor node-exporter prometheus
    monitors_started=true
  fi
  if [[ "$engine" == postgresql ]]; then
    engine_profile=postgres; other=mongodb; other_profile=mongo
  else
    engine_profile=mongo; other=postgresql; other_profile=postgres
  fi
  dc "$profile" --profile "$other_profile" stop "$other"
  dc "$profile" --profile "$engine_profile" up -d --pull never --no-build --no-deps "$engine"

  # Solo espera disponibilidad despues de arrancar/cambiar perfil. No reintenta mediciones.
  ready=false
  for attempt in 1 2 3 4 5; do
    if dc "$profile" --profile tools run --rm --pull never --no-deps --interactive=false -T benchmark-runner \
      check --engine "$engine" --query "$query" --variant "$variant" --concurrency "$concurrency" \
      > results/paqueteria/matrix_v1/readiness.log 2>&1; then
      ready=true; break
    fi
    echo "Esperando disponibilidad ($attempt/5); aun no se inicio la repeticion."
    [[ "$attempt" == 5 ]] || sleep 2
  done
  if ! "$ready"; then cat results/paqueteria/matrix_v1/readiness.log; exit 1; fi
  output="$(flush_cache | tr -d '\r')"
  scope='Kernel Linux local'
  if "$windows"; then scope='Kernel Linux de Docker Desktop/WSL2'; fi
  meta cache --run-id "$run_id" --cache-output "$output" --cache-scope "$scope"
  dc "$profile" --profile tools run --rm --pull never --no-deps --interactive=false -T benchmark-runner \
    run --engine "$engine" --query "$query" --variant "$variant" --concurrency "$concurrency" \
    --run-number "$repetition" --run-id "$run_id" \
    --cache-record "evidence/paqueteria/cache_flush/$run_id.json" --csv results/paqueteria/matrix_v1/measurements.csv
  meta sync
  # error/oom guardados tambien cuentan como intentos: no se repiten automaticamente.
done 3< results/paqueteria/matrix_v1/pending.tsv
meta audit
meta plan "${filters[@]}"
echo 'Bloque finalizado. Revisa los estados y las evidencias originales.'
