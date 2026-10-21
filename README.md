# FECOCLIMA IA — Backend

Backend independiente y dockerizado para monitoreo agrometeorológico, fenología, alertas determinísticas, importación/sincronización de estaciones, pronósticos, información satelital, RAG, agentes de IA y reportes PDF.

## Inteligencia por parcela

El portal ahora parte del polígono, el cultivo y la fecha de siembra. Ofrece vistas Productor/Técnico, ventanas determinísticas de 72/96/120 horas, cobertura de varias estaciones, Sentinel-1/Sentinel-2, agentes OpenAI/Edge por widget, chat persistente con fotos, avisos y PDF con plantillas HTML.

Consulte [la guía de arquitectura, configuración, API y migración](docs/PARCEL_INTELLIGENCE.md). Para activar esta versión se requieren la migración `0006_plot_intelligence`, el seed de agentes/roles y las nuevas dependencias del backend. La consulta anterior por estación se conserva en `/fecoclima-ia/clima`.

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
