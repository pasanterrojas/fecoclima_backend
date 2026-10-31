# Validación de la versión 2026-10-31

Estado: preparada para revisión y commit; no desplegada en producción. Backend y frontend son repositorios separados. No se ha creado ningún commit ni añadido archivos al índice.

| Comprobación | Resultado |
| --- | --- |
| Backend, PostgreSQL desechable en 55439 | 553 pruebas aprobadas; ninguna omitida |
| Migraciones desde base vacía | Hasta `0018_station_predictions`, incluida segunda ejecución sin cambios |
| Huellas de configuración | Exportación y verificación aprobadas |
| Herramienta de sellado del host | Referencias nuevas incluidas, secretos ignorados o rechazados y cambios detectados |
| Dependencias backend | Imagen construida con `requirements.lock`; `pip check` aprobado |
| Frontend | Instalación con lockfile congelado, typecheck y build aprobados |
| Chrome sin WebGL, build de producción | Mi parcela, modo de estaciones, visor regional, dibujo/edición y admin aprobados; cero excepciones JavaScript |
| Postman local, API real | Health y Ready: siete aserciones aprobadas, sin publicación de resultados |
| Servicios locales | API, worker y scheduler con la misma imagen; API saludable |
| Respaldo | Dump, archivos persistentes, configuración, fuentes, imagen Docker y frontend compilado; SHA-256 verificados |
| Restauración real | Dump restaurado en `release_restore` de PostgreSQL desechable, sin tocar la base de la aplicación |
| Comparación tras restaurar | Configuración, prompts, reglas y documentos coinciden con el original |
| Cuatro originales del cliente | Incluidos en `app/data/client_sources`; SHA-256 coincide con las referencias revisadas |

Se corrigió el fallo de la migración 0018 al instalar sobre una base nueva: la migración inicial ya crea la tabla de predicciones con el modelo actual. Se preserva la tabla existente y su historial. Se añadió una prueba de regresión.

Se amplió `.dockerignore` para excluir variantes `.env.*`, se fijaron las dependencias transitivas del backend y se incorporaron al repositorio los originales y las guías actuales. El archivo de ejemplo documenta la tarea diaria de predicción y no carga datos de demostración por defecto. No se modificaron las credenciales reales.

El checkpoint privado de esta revisión es `storage/backups/release-20261031T163942Z`, ignorado por Git: carpeta 0700 y archivos sensibles 0600. Incluye 44 agentes y 52 versiones de agentes, además de reglas, vínculos documentales e integraciones. El marcador `VERIFIED_RESTORE.json` registra la prueba de restauración. No subir esta carpeta a Git ni publicarla.

Imagen probada: `sha256:65d309dba25112987e339c91a6e8375f3e4455caa979f00df339e5df49c68f8b`, conservada dentro del checkpoint para promoverla sin reconstruir. Los manifiestos `release/source-manifest.json` de ambos repositorios identifican el contenido final que debe incluirse en los commits.

Las pruebas de interfaz usan API simulada; comprueban interacción, estados, sensores ausentes y recuperación de fallos. Las pruebas backend ejecutan la lógica e integración de base de datos, con proveedores externos simulados. Las siete comprobaciones de salud usan el API local real. No se enviaron correos ni Telegram, ni se hicieron análisis pagados. Las credenciales, dominios, HTTPS y conectividad con proveedores se deben comprobar en el destino según `RELEASE_2026_10_31.md`.

Quedan 15 avisos de deprecación de dependencias durante pytest; no provocaron fallos. Esta revisión no promete ausencia absoluta de defectos ni respuestas de IA idénticas frente a datos o proveedores que cambien.
