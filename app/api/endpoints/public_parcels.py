"""Open portal API: full parcel workflows scoped to a signed browser, never a CMS account."""
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import DbSession
from app.core.config import settings
from app.core.security import create_token, decode_token
from app.schemas.plots import CompileRulePayload, ParcelAnalysisPayload, ParcelPayload, PlotAlertPayload, PlotInterpretPayload
from app.services.parcel_browser import BrowserParcelActor, COOKIE_NAME, TOKEN_TYPE, browser_actor, public_payload
from . import parcels

router = APIRouter(prefix="/public/parcels", tags=["Portal público por navegador"])
PublicActor = Annotated[BrowserParcelActor, Depends(browser_actor)]


@router.post("/browser-session")
async def open_browser_session(request: Request, response: Response) -> dict:
    from app.api.endpoints.agent_public import _check_rate_limit
    _check_rate_limit(request, limit=30)
    # The custom header prevents cross-site form submissions; its initial UUID
    # is only a bootstrap marker, never proof of ownership or a chosen identity.
    try:
        UUID(request.headers.get("X-Parcel-Session", ""))
    except ValueError:
        raise HTTPException(422, "Recargue la página para preparar su navegador") from None
    raw = request.headers.get("X-Parcel-Access") or request.cookies.get(COOKIE_NAME)
    try:
        identifier = UUID(decode_token(raw or "", TOKEN_TYPE)["sub"])
    except (ValueError, KeyError, TypeError):
        identifier = uuid4()
    token = create_token(str(identifier), TOKEN_TYPE, timedelta(days=180))
    response.set_cookie(COOKIE_NAME, token, httponly=True, max_age=180 * 86400,
                        secure=settings.cookie_secure, samesite="lax", path=settings.cookie_path,
                        domain=settings.cookie_domain)
    response.headers["Cache-Control"] = "no-store"
    return {"browser_id": str(identifier), "access_token": token, "limit": 3}


@router.get('')
async def list_parcels(request: Request, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.list_parcels(request=request, db=db, actor=actor))


@router.post('')
async def create_parcel(payload: ParcelPayload, request: Request, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.create_parcel(payload=payload, request=request, db=db, actor=actor))


@router.put('/{plot_id}')
async def update_parcel(plot_id: UUID, payload: ParcelPayload, request: Request, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.update_parcel(plot_id=plot_id, payload=payload, request=request, db=db, actor=actor))


@router.delete('/{plot_id}')
async def delete_parcel(plot_id: UUID, request: Request, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.delete_parcel(plot_id=plot_id, request=request, db=db, actor=actor))


@router.get('/{plot_id}/influences')
async def parcel_influences(plot_id: UUID, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.parcel_influences(plot_id=plot_id, db=db, actor=actor))


@router.post('/{plot_id}/analyses')
async def analyze_parcel(plot_id: UUID, request: Request, db: DbSession, actor: PublicActor, payload: ParcelAnalysisPayload | None=None) -> dict:
    return public_payload(await parcels.analyze_parcel(plot_id=plot_id, request=request, db=db, actor=actor, payload=payload))


@router.get('/{plot_id}/analyses/latest')
async def latest_analysis(plot_id: UUID, db: DbSession, actor: PublicActor) -> dict | None:
    return public_payload(await parcels.latest_analysis(plot_id=plot_id, db=db, actor=actor))


@router.post('/{plot_id}/refresh')
async def refresh_parcel(plot_id: UUID, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.refresh_parcel(plot_id=plot_id, db=db, actor=actor))


@router.get('/{plot_id}/refresh-status')
async def parcel_refresh_status(plot_id: UUID, db: DbSession, actor: PublicActor, job_ids: list[UUID]=Query(default=[])) -> dict:
    return public_payload(await parcels.parcel_refresh_status(plot_id=plot_id, db=db, actor=actor, job_ids=job_ids))


@router.get('/{plot_id}/satellite-status')
async def parcel_satellite_status(plot_id: UUID, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.parcel_satellite_status(plot_id=plot_id, db=db, actor=actor))


@router.get('/{plot_id}/satellite/history')
async def parcel_satellite_history(plot_id: UUID, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.parcel_satellite_history(plot_id=plot_id, db=db, actor=actor))


@router.post('/analyses/{analysis_id}/satellite/sync')
async def sync_analysis_satellite(analysis_id: UUID, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.sync_analysis_satellite(analysis_id=analysis_id, db=db, actor=actor))


@router.post('/analyses/{analysis_id}/interpret')
async def interpret_parcel(analysis_id: UUID, payload: PlotInterpretPayload, request: Request, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.interpret_parcel(analysis_id=analysis_id, payload=payload, request=request, db=db, actor=actor))


@router.get('/analyses/{analysis_id}/chat')
async def read_chat(analysis_id: UUID, request: Request, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.read_chat(analysis_id=analysis_id, request=request, db=db, actor=actor))


@router.post('/analyses/{analysis_id}/chat')
async def parcel_chat(analysis_id: UUID, request: Request, db: DbSession, actor: PublicActor, message: str=Form('', max_length=2500), view_mode: Literal['producer', 'technical']=Form('producer'), window_hours: int=Form(72, ge=60, le=120), images: Annotated[list[UploadFile] | None, File()]=None, attachments: Annotated[list[UploadFile] | None, File()]=None) -> dict:
    return public_payload(await parcels.parcel_chat(analysis_id=analysis_id, request=request, db=db, actor=actor, message=message, view_mode=view_mode, window_hours=window_hours, images=images, attachments=attachments))


@router.get('/analyses/{analysis_id}/chat/media/{filename}')
async def chat_media(analysis_id: UUID, filename: str, request: Request, db: DbSession, actor: PublicActor) -> FileResponse:
    return public_payload(await parcels.chat_media(analysis_id=analysis_id, filename=filename, request=request, db=db, actor=actor))


@router.post('/analyses/{analysis_id}/reports')
async def create_plot_report(analysis_id: UUID, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.create_plot_report(analysis_id=analysis_id, db=db, actor=actor))


@router.get('/reports/{report_id}')
async def plot_report_status(report_id: UUID, db: DbSession, actor: PublicActor) -> dict:
    return public_payload(await parcels.plot_report_status(report_id=report_id, db=db, actor=actor))


@router.get('/reports/{report_id}/file')
async def plot_report_file(report_id: UUID, db: DbSession, actor: PublicActor) -> FileResponse:
    return public_payload(await parcels.plot_report_file(report_id=report_id, db=db, actor=actor))


@router.get('/{plot_id}/satellite/{observation_id}/{layer}')
async def parcel_satellite_file(plot_id: UUID, observation_id: UUID, layer: str, db: DbSession, actor: PublicActor) -> FileResponse:
    return public_payload(await parcels.parcel_satellite_file(plot_id=plot_id, observation_id=observation_id, layer=layer, db=db, actor=actor))

