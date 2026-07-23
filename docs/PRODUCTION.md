# Despliegue en producción

## Backend

1. Copie `api/.env.example` a `api/.env`.
2. Use contraseñas aleatorias y un `JWT_SECRET` de 64 caracteres o más.
3. Configure `FECOCLIMA_API_KEY` como secreto del servidor. No lo incluya en Git, imágenes Docker, logs o frontend.
4. Defina `CORS_ORIGINS` y `TRUSTED_HOSTS` con los dominios reales.
5. Mantenga PostgreSQL y pgAdmin enlazados a `127.0.0.1` o a una red privada.
6. Coloque un proxy inverso (Caddy/Nginx) con HTTPS delante de FastAPI.
7. Ejecute al menos un `api`, un `worker` y un `scheduler`. Solo debe existir un scheduler activo por entorno.
8. Monte `report_files` en almacenamiento persistente o cambie el adaptador a S3 compatible.
9. Programe respaldos de PostgreSQL y pruebas periódicas de restauración.

## Frontend

```bash
cd frontend
pnpm install
# El primer install genera pnpm-lock.yaml; consérvelo en control de versiones.
# En despliegues posteriores use: pnpm install --frozen-lockfile
pnpm build
pm2 start ecosystem.config.cjs
pm2 save
```

Configure el proxy para que `/backend/` apunte a FastAPI y defina:

```env
NUXT_PUBLIC_API_BASE=https://dominio.example/backend/api/v1
```

## Controles mínimos

- `ENVIRONMENT=production`
- `COOKIE_SECURE=true`
- `LOAD_DEMO_DATA=false`
- Rotación de llaves y secretos.
- Monitoreo de `/health`, `/ready`, trabajos fallidos y antigüedad de estaciones.
- Política de retención de archivos importados y reportes.
