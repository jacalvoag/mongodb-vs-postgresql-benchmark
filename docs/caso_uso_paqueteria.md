# Caso de uso: analisis de retrasos en entregas de paqueteria

## 1. Problema y objetivo

Una empresa de paqueteria opera varios centros de distribucion y ofrece
servicios estandar y expres. Un envio puede contener varios paquetes,
con fechas comprometidas y fechas reales de entrega diferentes.

La empresa necesita identificar en que centros y servicios se concentran
los retrasos y seleccionar los envios finalizados con mayor impacto acumulado.

Se comparara MongoDB frente a PostgreSQL JSONB al ejecutar estas operaciones
sobre el mismo conjunto sintetico, bajo los limites de CPU y memoria de la
practica. La unidad documental sera el envio. Los paquetes son elementos de
su arreglo y se contaran separadamente al interpretar el trabajo procesado.

## 2. Alcance y decisiones del caso

Se estudia analitica de entregas a una fecha de corte; no se implementaran
rastreo en tiempo real, cobros, autenticacion, notificaciones o gestion de rutas.
Los datos seran sinteticos. Los nombres, catalogos y distribuciones que se
usen para generarlos son supuestos del experimento, no estadisticas de una
empresa real ni condiciones adicionales atribuidas al docente.

Para conservar la escala y el protocolo de la practica, trabajaremos con
500000 envios, 1500000 paquetes y promedio exacto de tres paquetes por envio.
Cada bloque de cinco envios tendra 1, 2, 3, 4 y 5 paquetes. La semilla sera 2026.
Se mantendran los tres perfiles, las concurrencias 1/10/50, las tres repeticiones
y el ciclo 60/300/60, salvo una indicacion docente que los cambie expresamente.

Se utilizaran seis centros de distribucion y dos servicios: estandar y expres.
Un centro sera el responsable del despacho de todos los paquetes de un envio.
La generacion documentara los conteos efectivos por centro, servicio y estado;
no afirmaremos que una distribucion sintetica representa el mercado real.

Fecha de corte: 2026-10-01T00:00:00Z.
Las fechas de creacion perteneceran al intervalo desde 2025-01-01T00:00:00Z
hasta antes de esa fecha de corte. Las entregas y cierres registrados no
podran ser posteriores al corte. Los compromisos de paquetes aun abiertos
si pueden ser posteriores al corte.

Todas las fechas seran UTC, con formato fijo YYYY-MM-DDTHH:MM:SSZ y segundos
00. La precision de un minuto permite verificar los retrasos con enteros.
No se impondran porcentajes de spill, OOM o consultas exitosas.

## 3. Modelo documental

MongoDB utilizara la coleccion envios.
PostgreSQL utilizara la tabla envios(id UUID PRIMARY KEY, data JSONB), donde
id coincide con envio_id y data contiene el documento completo.
Ambos motores recibiran los mismos documentos de origen.

| Campo del envio | Tipo JSON / regla |
| --- | --- |
| envio_id | Cadena UUID, unica |
| fecha_creacion | Fecha UTC obligatoria |
| fecha_cierre | Fecha UTC para finalizado o cancelado; null para en_transito |
| estado | finalizado, en_transito o cancelado |
| servicio | estandar o expres |
| centro_distribucion | Objeto con id, nombre y region |
| origen | Objeto con pais, estado y ciudad |
| destino | Objeto con pais, estado y ciudad |
| paquetes | Arreglo no vacio de 1 a 5 objetos |
| retraso_acumulado_min | Entero >= 0 para finalizado; null en otros estados |

| Campo de cada paquete | Tipo JSON / regla |
| --- | --- |
| paquete_id | Cadena unica en el dataset |
| peso_kg | Numero positivo |
| dimensiones_cm | Objeto con largo, ancho y alto positivos |
| estado | entregado, en_transito o cancelado |
| fecha_compromiso | Fecha UTC posterior a fecha_creacion |
| fecha_entrega | Fecha UTC si entregado; null en otro estado |

Con la raiz en nivel 0, paquetes es nivel 1, cada objeto paquete nivel 2 y
su objeto dimensiones_cm nivel 3. Los objetos del centro, origen y destino
estaran en nivel 1. La profundidad maxima sera tres.

Se embeben los paquetes porque las dos operaciones analizan el envio y sus
paquetes conjuntamente. Centro, origen y destino conservan el contexto del
envio. Esta representacion evita dependencias de otras colecciones para las
consultas de comparacion y se conserva igualmente dentro del JSONB.

## 4. Reglas de integridad

1. Los identificadores de envio y paquete no se repiten.
2. Todos los envios tienen al menos un paquete.
3. El servicio y el centro responsable se definen a nivel envio.
4. Para un paquete entregado, fecha_entrega es obligatoria y no anterior
   a fecha_creacion. Puede ser anterior, igual o posterior a fecha_compromiso.
5. Un paquete en_transito o cancelado tiene fecha_entrega=null.
6. Un envio finalizado tiene todos sus paquetes entregados. fecha_cierre
   es la maxima fecha_entrega de sus paquetes.
7. Un envio en_transito tiene al menos un paquete en_transito y ninguno
   cancelado. Los demas paquetes pueden estar entregados. fecha_cierre=null.
8. Un envio cancelado tiene todos sus paquetes cancelados y fecha_cierre
   registra la cancelacion, entre fecha_creacion y la fecha de corte.
9. No se modelan cancelaciones parciales en esta version.
10. Las fechas reales son coherentes con la fecha de corte. El generador
    ajustara sus fechas de creacion y estados para cumplir estas relaciones;
    no asignara estados y fechas contradictorios de forma independiente.
11. La referencia independiente recalculara retrasos desde las fechas,
    incluida la validacion del acumulado guardado en cada envio finalizado.

## 5. Definicion del retraso

Para un paquete entregado:

    retraso_min = max(0, (fecha_entrega - fecha_compromiso) en minutos)

Para un envio finalizado:

    retraso_acumulado_min = suma(retraso_min de todos sus paquetes)

El acumulado expresa el impacto sobre sus paquetes. Por ejemplo, dos paquetes
con 30 y 90 minutos de retraso aportan 120 minutos acumulados. La entrega
anticipada de otro paquete aporta cero y no compensa esos retrasos.
Esta magnitud se distingue de la duracion total de transporte del envio.

En envios en_transito o cancelados, retraso_acumulado_min sera null para no
presentar un total cerrado cuando no se ha completado la entrega.
Q1 calculara el retraso desde las fechas de cada paquete, sin depender de
un retraso precalculado por paquete. Q2 ordenara por el acumulado del envio,
cuya correccion se comprobara durante la generacion y validacion.

## 6. Q1: concentracion de retrasos por centro y servicio

Pregunta: en el historico registrado hasta la fecha de corte, que centros y
tipos de servicio concentran mayor cantidad y tiempo de retraso?

Operacion equivalente en ambos motores:

1. Desanidar paquetes.
2. Seleccionar paquetes con estado entregado y fecha_entrega valida.
3. Calcular el retraso en minutos desde las dos fechas.
4. Conservar los paquetes con retraso_min > 0.
5. Agrupar por centro_distribucion.id y servicio.
6. Obtener paquetes_retrasados, retraso_total_min y retraso_promedio_min.

Incluye paquetes ya entregados de envios todavia en_transito. Los paquetes
cancelados o sin entrega se excluyen. No se restringe Q1 al periodo de Q2:
Q1 es el analisis historico completo disponible en el dataset.
Los grupos sin paquetes retrasados no aparecen. No se exige un orden de salida.

Q1 conserva el trabajo de desanidar, filtrar, calcular y agrupar. Su diferencia
respecto al ejemplo de ventas se justifica por las fechas y preguntas del caso.

## 7. Q2: envios finalizados con mayor retraso acumulado

Pregunta: que envios finalizados durante enero-septiembre de 2026 presentan
mayor impacto acumulado de retrasos y deben revisarse?

Operacion equivalente en ambos motores:

1. Filtrar estado=finalizado.
2. Filtrar fecha_cierre >= 2026-01-01T00:00:00Z y
   fecha_cierre < 2026-10-01T00:00:00Z.
3. Ordenar retraso_acumulado_min de mayor a menor.
4. Obtener los primeros 1000 envios.
5. Proyectar envio_id, centro_distribucion.id, servicio,
   fecha_cierre y retraso_acumulado_min.

El limite superior exclusivo evita ambiguedades con la ultima hora del periodo.
Si hay empates en el corte, se aceptara cualquier seleccion valida entre
ellos; no se modificara una consulta para forzar resultados identicos en
orden cuando el orden secundario no forma parte de la operacion definida.

Se mantendran allowDiskUse=false/true en MongoDB y work_mem=4MB/64MB en
PostgreSQL para comparar el comportamiento del ordenamiento bajo distintas
condiciones de memoria.
Las variantes permiten observar diferencias; no garantizan spill u OOM.

## 8. Indices iniciales y comparabilidad

MongoDB: indice unico en envio_id e indice simple en fecha_cierre, ademas
del indice automatico de _id.
PostgreSQL: PK id e indice B-tree sobre la expresion que extrae fecha_cierre
como texto del JSONB.

Las fechas usan exclusivamente el formato UTC fijo definido arriba, por lo
que Q2 puede comparar sus cadenas cronologicamente en ambos motores. El SQL
y el indice deberan emplear la misma expresion para esa comparacion. Q1
convertira fechas para calcular diferencias y no almacenara una conversion
privilegiada en un solo motor.

No se creara inicialmente un indice sobre retraso_acumulado_min. Es una
decision experimental para observar el ordenamiento y sus recursos. Los
mismos criterios de indexacion se aplicaran a ambos motores.

## 9. Documento de ejemplo y comprobacion

```json
{
  "envio_id": "bf9d48b9-74b8-5d11-972f-9334e88d7ac8",
  "fecha_creacion": "2026-03-10T08:00:00Z",
  "fecha_cierre": "2026-03-12T13:30:00Z",
  "estado": "finalizado",
  "servicio": "estandar",
  "centro_distribucion": {
    "id": "CD-01",
    "nombre": "Centro Tuxtla",
    "region": "sureste"
  },
  "origen": {
    "pais": "MX",
    "estado": "Chiapas",
    "ciudad": "Tuxtla Gutierrez"
  },
  "destino": {
    "pais": "MX",
    "estado": "Oaxaca",
    "ciudad": "Oaxaca de Juarez"
  },
  "paquetes": [
    {
      "paquete_id": "PAQ-000001-01",
      "peso_kg": 0.8,
      "dimensiones_cm": {
        "largo": 20,
        "ancho": 15,
        "alto": 10
      },
      "estado": "entregado",
      "fecha_compromiso": "2026-03-12T12:00:00Z",
      "fecha_entrega": "2026-03-12T11:40:00Z"
    },
    {
      "paquete_id": "PAQ-000001-02",
      "peso_kg": 2.4,
      "dimensiones_cm": {
        "largo": 35,
        "ancho": 25,
        "alto": 20
      },
      "estado": "entregado",
      "fecha_compromiso": "2026-03-12T12:00:00Z",
      "fecha_entrega": "2026-03-12T12:30:00Z"
    },
    {
      "paquete_id": "PAQ-000001-03",
      "peso_kg": 1.2,
      "dimensiones_cm": {
        "largo": 30,
        "ancho": 20,
        "alto": 15
      },
      "estado": "entregado",
      "fecha_compromiso": "2026-03-12T12:00:00Z",
      "fecha_entrega": "2026-03-12T13:30:00Z"
    }
  ],
  "retraso_acumulado_min": 120
}
```

Resultado Q1 de este unico documento:

| centro_id | servicio | paquetes_retrasados | retraso_total_min | retraso_promedio_min |
| --- | --- | ---: | ---: | ---: |
| CD-01 | estandar | 2 | 120 | 60 |

El paquete entregado 20 minutos antes del compromiso queda fuera de Q1.
El envio cumple el filtro temporal de Q2 y su valor de ordenamiento es 120.
Su presencia efectiva entre los primeros 1000 dependera de los demas envios.
Este ejemplo es funcional; sus numeros no son resultados del benchmark.

## 10. Adaptacion del proyecto

Se conservaran Docker, monitoreo, perfiles y los calculos genericos de
latencia, throughput, CPU y memoria que ya se comprobaron.

Se adaptaran, en este orden:

1. synthetic_data_generator.py y el manifiesto del nuevo dataset.
2. queries/postgresql/setup.sql, queries/mongodb/indexes.json y el cargador,
   para utilizar envios en ambos motores.
3. Q1/Q2 de ambos motores y su referencia independiente.
4. La normalizacion y comparacion de resultados de validate_queries.py.
5. Las referencias a orders en el cliente MongoDB del ejecutor y en la
   identificacion de sus logs; se revisaran tambien nombres y archivos de
   referencia para no mezclar datasets ni resultados de casos distintos.
6. La matriz, despues de validar el nuevo modelo y sus consultas.

El piloto de orders se conserva como prueba tecnica previa. Sus resultados
no se utilizaran como mediciones de paqueteria y no se reutilizara su celda
en la nueva matriz. La reutilizacion del piloto que proponia la fase 6
anterior queda pendiente de sustituir para este caso.

Esta definicion no genera datos, no recarga motores ni ejecuta mediciones.
Los porcentajes y rangos concretos de la generacion se documentaran como
supuestos al implementar el generador, respetando las reglas de integridad.
