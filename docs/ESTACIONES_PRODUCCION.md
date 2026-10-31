# Estaciones incluidas para producción

La migración `0019_production_stations` agrega las estaciones reales activas de la configuración del 31/10/2026: Raúl Peña, Capitán Miranda, CIR-San Pedro, Edelira, La Paz (Itapúa), María Auxiliadora, Pirapó, Santa Inés, Santa Rita, Vacay y Yatytay. El catálogo inmutable es `app/data/stations_2026_10_31.json` y su SHA-256 se valida antes de importarlo.

Se incluyen GUID, nombre/código, URL de consulta cuando está configurada, coordenadas, altitud, ubicación, huso horario, intervalo de muestreo, polígono/radio, color, variables declaradas, configuración de hoja y suelo, y modalidades de lluvia/ET. Se excluyen la estación demo, contraseñas, claves de proveedores, usuarios, lecturas históricas y resultados de análisis.

La migración busca coincidencias por código o GUID sin distinguir mayúsculas/minúsculas. Las estaciones existentes se conservan completas, con sus IDs y cambios del admin. Tampoco se recrean estaciones inactivas o eliminadas. Las faltantes se crean con IDs nuevos. Ejecutarla nuevamente no produce duplicados. La reversión no borra estaciones, lecturas ni vínculos que ya puedan estar en uso.

## Aplicar después de actualizar el código

`git pull` por sí solo no ejecuta código ni modifica PostgreSQL. Desde `backend`, con los cambios disponibles en el remoto:

```bash
git pull
# Construir todos los servicios con la nueva migración y el catálogo.
docker compose build
# Ejecuta alembic upgrade head y la inicialización habitual del proyecto.
docker compose run --rm migrate
docker compose up -d --no-build
```

El flujo `migrate` existente aplica la importación una sola vez. En despliegues sin Docker, ejecutar `alembic upgrade head` con el mismo código y la conexión correcta antes de reiniciar API, worker y scheduler. No se debe importar un dump sobre la base existente para agregar estaciones.

Las nuevas estaciones estarán visibles al recargar el mapa. Las lecturas se obtendrán con las sincronizaciones habituales y «Consultar todas las estaciones»; los datos locales históricos no viajan por Git. La clave FECOCLIMA y demás secretos permanecen en el entorno del servidor. Las estaciones que producción ya tiene conservan sus opciones anteriores; esta migración no fuerza cambios sobre ellas.
