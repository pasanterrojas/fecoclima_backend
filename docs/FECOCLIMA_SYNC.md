# Sincronización API FECOCLIMA

El adaptador usa la ruta parametrizada por GUID, año, mes inicial y mes final.

Reglas implementadas:

1. La solicitud sale únicamente desde el backend.
2. El encabezado `clima-key` se toma de `FECOCLIMA_API_KEY`.
3. Los rangos se dividen por año.
4. Cada petición cubre como máximo seis meses.
5. `fechaMuestreo` se interpreta en `America/Asuncion` y se guarda en UTC.
6. Se normalizan los campos meteorológicos conocidos.
7. La combinación `station_id + sampled_at` es única; la sincronización es idempotente.
8. Se registran duración, cantidad recibida, creados, actualizados y errores.
9. Hay timeout y reintentos con espera exponencial.
10. Los campos de suelo u hoja nulos se conservan como nulos; no se inventan valores.

La estación inicial **Raul Peña** se crea con su GUID documental, pero la llave de autorización no se distribuye.
