# Trazabilidad de fuentes utilizadas

Esta primera versión se construyó tomando como referencia los materiales suministrados para FECOCLIMA. Los archivos fuente no se redistribuyen dentro del proyecto para evitar incorporar credenciales, información sensible o duplicados innecesarios.

## Portal y referencia visual

La navegación pública, la paleta azul, el tratamiento de encabezados y el módulo de fenología se inspiraron en la captura y referencia del portal FECOCLIMA suministradas. La implementación nueva usa componentes Nuxt reutilizables y diseño responsive; no copia código del portal original.

## Especificación de la API FECOCLIMA

El adaptador `api/app/services/fecoclima_adapter.py` implementa:

- Solicitud REST `GET` desde el backend.
- Ruta `estacion/lectura-guid-por-periodo/{guid}/{año}/{mesDesde}/{mesHasta}`.
- Encabezado de autorización `clima-key`, cuyo valor solo se lee desde `FECOCLIMA_API_KEY`.
- División por año y bloques de hasta seis meses.
- Zona horaria `America/Asuncion` y almacenamiento normalizado en UTC.
- Reintentos, timeout, validación del JSON, filtrado por fechas y eliminación de duplicados.
- Identificador inicial de la estación Raul Peña: `C2694D32-5A4B-48C0-A3B6-8345D9E4935B`.

Correspondencia principal:

| API FECOCLIMA | Modelo normalizado |
|---|---|
| `fechaMuestreo` | `sampled_at` |
| `temp` | `temperature_c` |
| `chill` | `feels_like_c` |
| `hIndex` | `heat_index_c` |
| `humid` | `humidity_pct` |
| `dewpt` | `dew_point_c` |
| `wind` | `wind_speed_ms` |
| `hiWind` | `wind_gust_ms` |
| `windDir` | `wind_direction_deg` |
| `rain` | `rain_mm` |
| `totalRain` | `total_rain_mm` |
| `barom` | `pressure_hpa` |
| `solar` | `solar_wm2` |
| `et` | `evapotranspiration_mm` |
| `uv` | `uv_index` |
| `temperaturaSuelo` | `soil_temperature_c` |
| `humedadSuelo` | `soil_moisture_pct` |
| `sensorHoja` | `leaf_sensor_value` |

La llave real entregada en la documentación no está en el código, ejemplos, historial, ZIP ni logs.

## Plantillas de soja y trigo

Las plantillas `Alerta Soja x dia(1).xlsx` y `Alerta Trigo x dia(2).xlsx` se usaron para estructurar:

- etapas fenológicas iniciales;
- duración acumulada por etapa;
- catálogo inicial de enfermedades;
- separación entre condiciones medibles y criterios pendientes de validación agronómica.

Duraciones iniciales cargadas por semilla:

- Soja: SIEMBRA 0; VE 5; V1 9; V2-V4 16; V5-V8 14; V9 10; R1 5; R3 21; R5 17; R7 11; R8 12; R9 7 días.
- Trigo: SIEMBRA 0; GERMINACIÓN 17; BROTACIÓN 20; ESPIGAMIENTO 25; FLORACIÓN 17; LLENADO 32; MADURACIÓN 12; COSECHA 7 días.

Los términos no cuantificados —por ejemplo, “humedad elevada”, “lluvias persistentes” o “temperatura cercana”— permanecen en estado pendiente. El sistema no publica umbrales inventados.
