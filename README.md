# FECOCLIMA IA — Backend

Backend independiente y dockerizado para monitoreo agrometeorológico, fenología, alertas determinísticas, importación/sincronización de estaciones, pronósticos, información satelital, RAG, agentes de IA y reportes PDF.

## Inteligencia por parcela

El portal ahora parte del polígono, el cultivo y la fecha de siembra. Ofrece vistas Productor/Técnico, ventanas determinísticas de 72/96/120 horas, cobertura de varias estaciones, Sentinel-1/Sentinel-2, agentes OpenAI/Edge por widget, chat persistente con fotos, avisos y PDF con plantillas HTML.

Consulte [la guía de arquitectura, configuración, API y migración](docs/PARCEL_INTELLIGENCE.md). Para activar esta versión se requieren la migración `0006_plot_intelligence`, el seed de agentes/roles y las nuevas dependencias del backend. La consulta anterior por estación se conserva en `/fecoclima-ia/clima`.

Al dibujar un polígono se inicia automáticamente la búsqueda en Copernicus. En polígonos existentes, el botón **Traer imágenes de Copernicus** solicita una búsqueda nueva, y el navegador sigue su procesamiento hasta mostrar el resultado. Cada trabajo procesa la escena válida más reciente y busca otras dos en días UTC anteriores, con los mismos controles de calidad del polígono. Se descargan imágenes e indicadores para las tres fechas; no se limita a consultar escenas que ya estaban almacenadas. Si no hay tres escenas válidas en el período configurado o el proveedor falla, se conserva lo descargado y se informa el resultado parcial. Las consultas por polígono y `/parcels/{id}/satellite/history` muestran las tres escenas utilizables más recientes para la geometría actual. Una escena nueva desplaza a la más antigua de la vista, sin borrar observaciones ni archivos.

Al pulsar **Analizar**, el cliente envía `satellite_observation_id` junto con `window_hours` a `/parcels/{id}/analyses` (o su variante pública). El servidor valida que la escena corresponda al polígono y construye el contexto IA con su imagen, fecha e indicadores. `context.satellite_selection` registra la elección, que no puede ser sustituida por una sincronización automática posterior. Los clientes que omiten el identificador conservan el comportamiento de utilizar la última escena disponible. Si se redibuja una parcela existente, una escena de vista previa autorizada del mismo navegador se conserva también en el histórico de la parcela, manteniendo el registro original.

En **Administración → Parcelas → Histórico de parcelas e imágenes** se consultan todas las parcelas, incluidas las eliminadas y los polígonos explorados sin cultivo. Las rutas `/admin/parcel-history`, `/{id}` y `/{id}/images` permiten consultar el registro y sus escenas con paginación; las imágenes se sirven desde `/{id}/images/{observation_id}/{layer}` y aceptan `download=true`. Requieren rol administrador o superadministrador. El histórico conserva la geometría de procesamiento, las capas RGB/índices disponibles y las referencias a los análisis que utilizaron cada escena. Aprovecha los registros y el borrado lógico existentes; no requiere una migración adicional. Para conservar las imágenes se debe mantener también el volumen persistente `/data/satellite` en los respaldos.

## Componentes

- FastAPI y Python 3.12.
- PostgreSQL 16 con extensión pgvector.
- SQLAlchemy 2 y Alembic.
- API HTTP, worker y scheduler en procesos separados.
- pgAdmin.
- Motor determinístico de alertas para soja y trigo.
- Adaptador backend para la API FECOCLIMA.
- Importación idempotente de CSV/XLSX.
- Pronóstico Open-Meteo.
- Catálogo Copernicus/STAC configurable.
- Documentos RAG PDF, DOCX y TXT.
- Generación de PDF en segundo plano.
- Autenticación con access/refresh token, cookies HttpOnly, CSRF, recuperación de contraseña, bloqueo por intentos y auditoría.

## Inicio rápido con Docker

Requisitos: Docker Engine y Docker Compose v2.

```bash
cp .env.example .env
```

Edite `.env` y cambie como mínimo:

- `POSTGRES_PASSWORD` y el mismo valor dentro de `DATABASE_URL`.
- `JWT_SECRET`.
- `ENCRYPTION_KEY`.
- `SUPERADMIN_EMAIL` y `SUPERADMIN_PASSWORD`.
- `PGADMIN_DEFAULT_PASSWORD`.
- `FECOCLIMA_API_KEY` cuando vaya a sincronizar estaciones de la API FECOCLIMA (no se envía al navegador).

Levante los servicios:

```bash
docker compose up -d --build
```

Verifique:

```bash
docker compose ps
docker compose logs -f migrate api worker scheduler
```

Servicios predeterminados:

- API: `http://localhost:8000`
- OpenAPI/Swagger: `http://localhost:8000/docs`
- Healthcheck: `http://localhost:8000/health`
- Readiness: `http://localhost:8000/ready`
- Métricas: `http://localhost:8000/metrics`
- pgAdmin: `http://localhost:5050`
- PostgreSQL local: `127.0.0.1:5437`

La migración y la carga inicial se ejecutan mediante el servicio `migrate` antes de iniciar la API. El superusuario se crea únicamente cuando no existe ningún usuario.

## Comandos útiles

```bash
make up
make logs
make ps
make down
```

Ejecutar pruebas fuera de Docker:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

## Producción

1. Configure `ENVIRONMENT=production`.
2. Use secretos aleatorios y distintos para base de datos, JWT, cifrado, superusuario y pgAdmin.
3. Configure `COOKIE_SECURE=true`.
4. Establezca `COOKIE_DOMAIN`, `TRUSTED_HOSTS`, `CORS_ORIGINS` y `FRONTEND_BASE_URL` con sus dominios HTTPS reales.
5. Mantenga PostgreSQL y pgAdmin ligados a `127.0.0.1` o elimine sus puertos públicos.
6. Publique solo la API detrás de Caddy, Nginx o un balanceador TLS.
7. Cambie `FORWARDED_ALLOW_IPS=*` por la red o IP de su proxy cuando sea posible.
8. Configure copias de seguridad y pruebe restauraciones periódicamente.
9. Use `LOAD_DEMO_DATA=false` después de la primera preparación del ambiente real.

El backend aborta el arranque en producción cuando detecta configuraciones inseguras evidentes.

## Sincronización FECOCLIMA

La integración consulta desde el backend:

```text
GET /estacion/lectura-guid-por-periodo/{guid}/{año}/{mesDesde}/{mesHasta}
Header: clima-key
```

El adaptador:

- divide por año;
- limita cada solicitud a seis meses;
- normaliza fechas según la zona horaria configurada para cada estación;
- evita duplicados por estación y fecha de muestreo;
- aplica timeout y reintentos;
- registra el resultado en trabajos e integraciones.

La estación inicial Raul Peña se carga con su GUID público. La llave `clima-key` no está incluida y debe configurarse en `.env`.

Para agregar otra estación desde **Administración → Estaciones**, registre su nombre, código, coordenadas y zona horaria. Si usa `FECOCLIMA_API`, asigne su GUID externo propio (único) y pruebe la conexión en **Datos y sincronización**. Para `EXCEL` o `CSV`, importe archivos asignados a esa estación; para `MANUAL`, cargue los datos por el flujo administrativo. Ajuste las unidades de sensores en `integration_config` y `leaf_sensor_config` sin almacenar secretos allí. El portal muestra las estaciones activas y conserva las lecturas separadas por estación.

## Disponibilidad de la IA

Cada respuesta del agente expone un estado: `completed` si respondió el modelo o `fallback` si se usó un resumen basado en datos. El portal y **Administración → Panel administrativo** muestran esa diferencia sin exponer credenciales ni detalles privados del proveedor. Un HTTP 401 en las ejecuciones significa que el proveedor no autorizó la clave: reemplace `AI_API_KEY` en el entorno del backend y recree el contenedor `api` (y `worker`/`scheduler` si también consumen IA). Compruebe después el modelo en **Administración → Agentes IA**. Los datos meteorológicos y las alertas siguen disponibles sin IA. El botón **Explicar** consulta al agente solo cuando el productor lo solicita y utiliza exclusivamente los datos de esa consulta; también identifica claramente las respuestas de respaldo.

## Estructura

```text
app/
├── api/endpoints/      # rutas HTTP
├── core/               # configuración, seguridad, cifrado y logs
├── db/                 # modelos y sesión SQLAlchemy
├── schemas/            # contratos Pydantic
├── services/           # reglas, FECOCLIMA, RAG, PDF, imports, IA, etc.
├── scheduler.py        # crea trabajos periódicos
└── worker.py           # ejecuta trabajos en segundo plano
alembic/                # migraciones
scripts/                # entrypoint de contenedores
tests/                  # pruebas unitarias
docs/                   # documentación técnica y diagramas
postman/                # colección de API
```

## Reglas agronómicas

El color de alerta se calcula exclusivamente en Python. La IA recibe el resultado estructurado y solo lo explica. Los umbrales ambiguos de las fuentes quedan marcados como pendientes de validación técnica y no se inventan automáticamente.

## Datos persistentes

Docker utiliza:

- `postgres_data`: base de datos.
- `pgadmin_data`: configuración de pgAdmin.
- `fecoclima_data`: reportes, documentos, exportaciones, errores y respaldos.

Para detener sin borrar información:

```bash
docker compose down
```

Para borrar también los volúmenes —acción destructiva—:

```bash
docker compose down -v
```

## Avisos y alertas

El administrador concentra el servicio en **Avisos y alertas**, con pestañas Alertas, Libreta de direcciones y Configuración. Los enlaces antiguos a eventos y libreta redirigen a esta sección. Se retiraron los envíos automáticos del boletín diario y del monitor anterior de alertas de parcela.

- La inscripción pública `/fecoclima-ia/suscripciones` registra correo, usuario opcional de Telegram, preferencias de canal y consentimiento `notifications-v1` con fecha. Requiere confirmar la dirección; Telegram requiere además iniciar el bot desde el enlace firmado. Una baja voluntaria nunca puede revertirse desde administración sin una nueva inscripción confirmada.
- Cada alerta tiene nombre, código inmutable `ALT-…`, prompt y estado. Cada alerta debe tener una estación meteorológica seleccionada. Solo se notifican contactos confirmados con parcelas vinculadas dentro de su área de influencia actual, incluida la cobertura parcial con área positiva. Cada alerta configura su frecuencia (valor inicial: 120 minutos), ventana histórica (72 horas) y si actualiza el análisis completo de parcelas. El scheduler detecta ciclos pendientes cada minuto y el worker los ejecuta sin depender del navegador; necesita lecturas recientes y continuidad suficiente. Los prompts se interpretan con el agente de datos y se convierten a expresiones verificables: todas las variables numéricas disponibles de las estaciones, comparaciones mayores/menores/igualdad/rangos, condiciones continuas, promedios, acumulados de lluvia/evapotranspiración, mínimos, máximos, cambios y combinaciones anidadas con «y»/«o». No hay umbrales ni reglas de enfermedad preestablecidos. Se entienden números escritos y variaciones de redacción; se normalizan unidades a las almacenadas. Los datos no disponibles o detalles ambiguos se explican sin inventar condiciones. «Interpretar condición» muestra el resumen de la regla antes de guardarla. Una condición interpretada se reutiliza durante 15 minutos entre vista previa y guardado; editar nombre o estado conserva la regla guardada sin otra llamada IA. Ejemplo: `Cuando la humedad relativa sea mayor a 85% durante 10 horas seguidas y la temperatura entre 15 y 25 es propenso a enfermedad de roya`. Humedad y temperatura deben mantenerse simultáneamente durante esas diez horas; es un riesgo, no un diagnóstico.
- Se avisa al comenzar un episodio por estación. Mientras siga cumplido no se repite; solo una evaluación negativa lo rearma. La falta de datos no dispara ni rearma. «Comprobar sin enviar» no cambia el estado del monitor.
- El agente notificador general (especialidad interna `DAILY_DIGEST`, conservada para compatibilidad con el CMS) redacta con su prompt publicado. Guardar Configuración publica una versión de sus instrucciones. Modelo/proveedor se cambian en Agentes. Si falla la IA no se consume la transición; se reintenta la evaluación posterior.
- Cada entrega conserva contenido, ejecución del agente, destinatario, canal, intentos y resultado. Los envíos fallidos se reintentan hasta cinco veces con espera creciente. Antes de cada envío se vuelven a comprobar la suscripción, los canales y el estado de la alerta. Desactivar el servicio pausa las entregas; revocar consentimiento o desactivar la alerta cancela las pendientes. Correo se convierte a HTML escapado y Telegram a texto.

Para habilitarlo: aplicar `alembic upgrade head`, mantener `api`, `worker` y `scheduler` levantados, configurar `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS` y un `SMTP_FROM` real; configurar `FRONTEND_BASE_URL` con la URL pública (incluido `/fecoclima-ia`). En Configuración guardar el token de BotFather, elegir canales, revisar el prompt del notificador y habilitar el servicio. Crear y activar las alertas deseadas. Nunca se incluyen los tokens en las respuestas de configuración.

Pruebas: `tests/test_notification_events_v4.py`, `tests/test_unified_notifications.py` (PostgreSQL desechable en 55439; envíos simulados), y `NOTIFICATIONS_ONLY=1 node scripts/cms-admin-smoke.mjs` desde frontend con Nuxt/Chrome disponibles. No usar una base real para las pruebas de integración.

El intérprete flexible usa la versión activa del agente general de datos (`WEATHER_ANALYSIS`) y su proveedor/modelo; si no existe, usa el proveedor/modelo predeterminado. Conserva trazabilidad de la interpretación en `AIAgentRun`. La versión 3 evalúa únicamente expresiones validadas, sin ejecutar código del modelo. Las reglas anteriores (versiones 1 y 2) conservan su evaluación hasta que se edite su prompt. La duración se mide hasta la última lectura disponible, diferenciando «más de» de «al menos», y las ventanas requieren cobertura de datos.

## Ciclos automáticos de alertas y simulaciones

La migración `0016_notification_cycles` añade el período histórico, modo completo y bitácora de ejecuciones. Las alertas que usaban la anterior frecuencia fija de cinco minutos adoptan dos horas. El monitor antiguo deja de evaluar directamente: el scheduler encola ciclos duraderos `NOTIFICATION_EVENT_CYCLE`, con identificador de ejecución y protección frente a solapamientos.

Cada ciclo sincroniza las estaciones API para el intervalo necesario y agrega sus lecturas. Con análisis completo habilitado actualiza las parcelas activas de su ámbito usando `build_plot_analysis` y los mismos agentes del botón del productor, en orden: agro/RAG, datos, satélite, orquestador. Usa el pronóstico actualizado y las imágenes procesadas disponibles; la descarga satelital sigue su sincronización independiente. Los análisis guardados conservan sus identificadores y evidencias. Se informa si un agente o parcela falla, sin inventar resultados.

Se comprueba el prompt en las mediciones a lo largo de la ventana configurada, no únicamente en el instante del ciclo. Se conserva una posición y estado por estación para no volver a notificar los mismos episodios en ventanas superpuestas. El notificador recibe las evidencias fechadas y los análisis de parcelas, y prepara entregas según consentimiento/canales. La frecuencia se mide desde el inicio del ciclo anterior; una alerta no superpone dos ciclos. Los errores de ciclo quedan registrados y respetan esa frecuencia.

En el formulario de alerta: **Simular ciclo sin enviar** encola el mismo procesamiento, incluso si la alerta está pausada. Guarda análisis y trazas de simulación, muestra el aviso preparado, y no altera los episodios notificados ni crea entregas. **Ejecutar ahora y notificar** requiere alerta y servicio activos, y sí puede enviar si hay episodios nuevos. El historial distingue automático/manual/simulación y permite abrir los resultados después de cerrar la página. API: `POST /notifications/events/{id}/simulate`, `POST /notifications/events/{id}/run`, `GET /notifications/events/{id}/runs`, `GET /notifications/events/{id}/runs/{run_id}`.

## Audiencia por estación, parcelas y contacto

La migración `0017_station_parcel_audience` crea la relación explícita `subscription_plots`. No se infiere pertenencia por nombre, documento, correo, `plot.station_id` ni relaciones antiguas de influencia. Se pausan las alertas sin estación para que el administrador elija su ámbito y se cancelan entregas antiguas sin evidencia de parcelas vinculadas.

En **Libreta de direcciones → Vincular parcelas**, administración asigna una o varias parcelas a un contacto confirmado, sin cambiar su consentimiento ni canales. La tabla muestra cobertura actual y porcentaje por estación. Un contacto sin parcelas vinculadas no recibe estas alertas. En la inscripción pública, el navegador firmado permite seleccionar únicamente sus propias parcelas guardadas; se aplican vínculos y cambios de preferencias después de confirmar el correo. Pedir una actualización de un contacto activo no suspende ni altera su suscripción mientras espera confirmación. Una baja invalida los cambios pendientes.

Cada alerta requiere `station_id`. Se calcula la intersección geométrica real con el polígono de cobertura de esa estación o su radio configurado. Los análisis automáticos se limitan a parcelas activas cubiertas. Cada mensaje se prepara exclusivamente con las parcelas y análisis del destinatario; los contactos no reciben datos de otras parcelas. La entrega vuelve a validar consentimiento, vínculo y cobertura actual. Borrar una parcela, moverla fuera de cobertura o retirar el vínculo cancela mensajes pendientes que la incluyan.

Se deduplican episodios por contacto y canal. Si una persona vincula su parcela o Telegram mientras continúa un episodio, puede recibirlo una vez sin repetirlo a los contactos ya avisados. La simulación muestra destinatarios previstos, canales y parcelas; no crea entregas. Pruebas específicas: `tests/test_notification_audience.py`.
