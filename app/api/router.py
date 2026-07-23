from fastapi import APIRouter

from app.api.endpoints import (
    admin,
    alerts,
    auth,
    backups,
    exports,
    forecasts,
    health,
    imports,
    integrations,
    jobs,
    knowledge,
    phenology,
    public,
    reports,
    resources,
    rules,
    satellite,
    stations,
    users,
)

api_router = APIRouter()
for router in [
    health.router,
    auth.router,
    public.router,
    users.router,
    stations.router,
    resources.router,
    phenology.router,
    rules.router,
    alerts.router,
    forecasts.router,
    satellite.router,
    knowledge.router,
    integrations.router,
    imports.router,
    exports.router,
    reports.router,
    backups.router,
    jobs.router,
    admin.router,
]:
    api_router.include_router(router)
