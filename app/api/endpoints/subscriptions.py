"""Suscripciones voluntarias, libreta de direcciones y reglas del notificador."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy import func, select, or_, delete

from app.api.deps import AdminUser, DbSession
from app.db.models import (Crop, EmailSubscription, NotificationEvent,
                           NotificationEventDelivery, NotificationRequestLog, Station, WeatherReading, Plot, SubscriptionPlot)
from app.services.notification_events import (
    _message, check_manage_token, digest,
    evaluate_event, evaluate_readings, manage_token, public_url, send_mail,
)
from app.services.notification_rules import compile_notification_prompt
from app.services.serialization import model_dict
from app.services.audit import write_audit
from app.services.jobs import enqueue_unique

router = APIRouter(prefix='/notifications', tags=['Suscripciones y eventos'])


class SubscriptionInput(BaseModel):
    email: EmailStr
    crop_id: UUID | None = None
    station_id: UUID | None = None
    plot_ids: list[UUID] = Field(default_factory=list,max_length=100)
    consent: bool
    email_enabled: bool = True
    telegram_enabled: bool = False
    telegram_username: str | None = Field(default=None, max_length=40)

    @model_validator(mode='after')
    def channels(self):
        if not self.email_enabled and not self.telegram_enabled:
            raise ValueError('Seleccione al menos un canal de notificación.')
        if self.telegram_username:
            import re
            self.telegram_username = self.telegram_username.strip().lstrip('@')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', self.telegram_username):
                raise ValueError('Indique un usuario de Telegram válido, por ejemplo @productor.')
        return self
    website: str = Field(default='', max_length=200)  # Campo trampa anti-bot


class TokenInput(BaseModel):
    token: str = Field(min_length=35, max_length=250)


class EventInput(BaseModel):
    name: str = Field(min_length=3, max_length=180)
    prompt: str = Field(min_length=15, max_length=2000)
    active: bool = False
    # Optional scopes retain existing records without requiring public subscribers
    # to pick a crop or station. New admin alerts default to all active stations.
    crop_id: UUID | None = None
    station_id: UUID
    timezone: str = Field(default='America/Costa_Rica', max_length=80)
    cadence: Literal['INTERVAL'] = 'INTERVAL'
    every_minutes: int = Field(default=120, ge=15, le=10080)
    lookback_hours: int = Field(default=72, ge=1, le=720)
    run_full_analysis: bool = True

    @model_validator(mode="after")
    def analysis_window(self):
        if self.run_full_analysis and self.lookback_hours not in (60, 72, 96, 120):
            raise ValueError("El análisis completo admite ventanas de 60, 72, 96 o 120 horas.")
        return self
    channel: Literal['BOTH'] = 'BOTH'


class SubscriptionStatusInput(BaseModel):
    active: bool


def _render_subscription(row:EmailSubscription,crop:str,station:str) -> dict:
    return {'id':str(row.id),'email':row.email,'crop_id':str(row.crop_id),'crop':crop,
            'station_id':str(row.station_id),'station':station,'status':row.status,
            'confirmed_at':row.confirmed_at,'unsubscribed_at':row.unsubscribed_at,
            'created_at':row.created_at,'last_notified_at':row.last_notified_at,
            'telegram_active':row.telegram_active,'email_enabled':row.email_enabled,
            'telegram_enabled':row.telegram_enabled,'telegram_username':row.telegram_username,
            'consent_at':row.consent_at,'consent_version':row.consent_version}


@router.get('/subscriptions/options')
async def subscription_options(db:DbSession) -> dict:
    crops=list((await db.scalars(select(Crop).where(Crop.active.is_(True),Crop.deleted_at.is_(None)).order_by(Crop.name))).all())
    stations=list((await db.scalars(select(Station).where(Station.active.is_(True),Station.deleted_at.is_(None)).order_by(Station.name))).all())
    return {'crops':[{'id':str(c.id),'name':c.name} for c in crops],
            'stations':[{'id':str(s.id),'name':s.name,'latitude':s.latitude,'longitude':s.longitude,'locality':s.locality} for s in stations]}


@router.get('/subscriptions/parcel-options')
async def public_subscription_parcels(request:Request,db:DbSession)->dict:
    from app.services.parcel_browser import browser_actor
    actor=browser_actor(request)
    plots=(await db.scalars(select(Plot).where(Plot.browser_session_id==actor.browser_id,
        Plot.created_by.is_(None),Plot.active.is_(True),Plot.deleted_at.is_(None),Plot.crop_id.is_not(None)))).all()
    return {'items':[{'id':str(p.id),'name':p.name} for p in plots]}


@router.post('/subscriptions/request',status_code=202)
async def subscribe(payload:SubscriptionInput,request:Request,db:DbSession) -> dict:
    # Nunca revelar si un correo pertenece o no a la libreta.
    accepted={'message':'Si los datos son válidos, recibirá un correo para confirmar su suscripción.'}
    if payload.website or not payload.consent:
        if not payload.consent and not payload.website: raise HTTPException(422,detail='Debe autorizar expresamente el envío de notificaciones.')
        return accepted
    email=str(payload.email).lower().strip()
    now=datetime.now(UTC)
    ip=request.client.host if request.client else 'unknown'
    ip_digest=digest('subscribe-ip:'+ip)
    email_digest=digest('subscribe-email:'+email)
    hour=now-timedelta(hours=1)
    day=now-timedelta(days=1)
    ip_count=await db.scalar(select(func.count()).select_from(NotificationRequestLog).where(NotificationRequestLog.ip_digest==ip_digest, NotificationRequestLog.created_at>=hour))
    email_count=await db.scalar(select(func.count()).select_from(NotificationRequestLog).where(NotificationRequestLog.email_digest==email_digest, NotificationRequestLog.created_at>=day))
    if (ip_count or 0)>=30 or (email_count or 0)>=3:
        return accepted
    db.add(NotificationRequestLog(ip_digest=ip_digest,email_digest=email_digest))
    crop=await db.get(Crop,payload.crop_id) if payload.crop_id else None
    station=await db.get(Station,payload.station_id) if payload.station_id else None
    if (payload.crop_id and (not crop or not crop.active or crop.deleted_at)) or (payload.station_id and (not station or not station.active or station.deleted_at)):
        await db.commit()
        return accepted
    row=await db.scalar(select(EmailSubscription).where(EmailSubscription.email==email,EmailSubscription.crop_id==payload.crop_id,EmailSubscription.station_id==payload.station_id))
    if payload.plot_ids:
        from app.services.parcel_browser import browser_actor
        actor=browser_actor(request)
        owned=set((await db.scalars(select(Plot.id).where(Plot.id.in_(payload.plot_ids),
            Plot.browser_session_id==actor.browser_id,Plot.created_by.is_(None),Plot.active.is_(True),
            Plot.deleted_at.is_(None),Plot.crop_id.is_not(None)))).all())
        if owned!=set(payload.plot_ids):
            raise HTTPException(404,detail='Alguna parcela no está disponible en este navegador.')
    token=secrets.token_urlsafe(32)
    if not row:
        row=EmailSubscription(email=email,crop_id=payload.crop_id,station_id=payload.station_id,manage_secret=secrets.token_hex(32))
        db.add(row)
    changes={'email_enabled':payload.email_enabled,'telegram_enabled':payload.telegram_enabled,
             'telegram_username':payload.telegram_username,'plot_ids':[str(i) for i in set(payload.plot_ids)],
             'browser_id':str(actor.browser_id) if payload.plot_ids else None,
             'consent_at':now.isoformat(),'consent_version':'parcel-alerts-v2','replace_plots':row.status!='ACTIVE'}
    row.pending_changes=changes
    if row.status!='ACTIVE':
        row.email_enabled=payload.email_enabled;row.telegram_enabled=payload.telegram_enabled
        row.telegram_username=payload.telegram_username;row.telegram_active=False
        row.consent_at=now;row.consent_version='parcel-alerts-v2'
        row.status='PENDING'
    row.confirm_hash=hashlib.sha256(token.encode()).hexdigest()
    row.confirm_expires_at=now+timedelta(hours=24)
    await db.commit()
    link=public_url('?confirmar='+token)
    msg=_message(email,'Confirme su suscripción · FECOCLIMA IA','Confirme que desea recibir alertas',
                 'Solicitó inscribirse en la libreta de direcciones de FECOCLIMA IA para recibir avisos y alertas por los canales elegidos. Confirme en las próximas 24 horas. Si no fue usted, ignore este mensaje.',
                 action_url=link,action_text='Confirmar correo')
    try:
        await send_mail(msg)
    except Exception:
        # No exponer errores de infraestructura ni información de la libreta al público.
        raise HTTPException(status_code=503,detail='No se pudo enviar el mensaje de confirmación. Consulte al administrador.') from None
    return accepted


@router.post('/subscriptions/confirm')
async def confirm_subscription(payload:TokenInput,db:DbSession) -> dict:
    token_hash=hashlib.sha256(payload.token.encode()).hexdigest()
    row=await db.scalar(select(EmailSubscription).where(EmailSubscription.confirm_hash==token_hash))
    if not row or not row.confirm_expires_at or row.confirm_expires_at<datetime.now(UTC) or row.status not in ('PENDING','ACTIVE'):
        raise HTTPException(400,detail='Enlace inválido o vencido. Solicite una nueva confirmación.')
    changes=row.pending_changes or {}
    if changes:
        row.email_enabled=bool(changes.get('email_enabled',True))
        row.telegram_enabled=bool(changes.get('telegram_enabled',False))
        row.telegram_username=changes.get('telegram_username')
        if not row.telegram_enabled:row.telegram_active=False
        row.consent_at=datetime.fromisoformat(changes['consent_at'])
        row.consent_version=changes['consent_version']
        if changes.get('replace_plots'):
            await db.execute(delete(SubscriptionPlot).where(SubscriptionPlot.subscription_id==row.id))
        existing=set((await db.scalars(select(SubscriptionPlot.plot_id).where(SubscriptionPlot.subscription_id==row.id))).all())
        ids=[UUID(i) for i in changes.get('plot_ids',[])]
        active=set((await db.scalars(select(Plot.id).where(Plot.id.in_(ids),Plot.active.is_(True),Plot.deleted_at.is_(None),
            Plot.created_by.is_(None),Plot.browser_session_id==UUID(changes['browser_id'])))).all()) if ids and changes.get('browser_id') else set()
        for plot_id in active-existing:db.add(SubscriptionPlot(subscription_id=row.id,plot_id=plot_id))
        row.pending_changes={}
    row.status='ACTIVE';row.confirm_hash=None;row.confirm_expires_at=None
    row.confirmed_at=datetime.now(UTC);row.unsubscribed_at=None
    await db.commit()
    from app.services.producer_notifications import get_notification_integration, telegram_bot_token
    integration=await get_notification_integration(db)
    config=integration.non_sensitive_config or {} if integration else {}
    username=str(config.get('telegram_bot_username') or '').lstrip('@')
    bot_ready=bool(integration and telegram_bot_token(integration) and username)
    telegram_code='fc_'+row.id.hex+'_'+digest(f'tg:{row.id}:{row.manage_secret}')[:24]
    telegram_url=f'https://t.me/{username}?start={telegram_code}' if bot_ready and row.telegram_enabled else None
    return {'message':'Suscripción confirmada. Recibirá avisos por los canales autorizados. Para Telegram, abra el enlace y pulse Iniciar.','telegram_url':telegram_url}


@router.get('/subscriptions/manage')
async def preview_unsubscribe(token:str,db:DbSession) -> dict:
    try:
        row_id=UUID(token.split('.',1)[0])
    except (ValueError,IndexError):
        raise HTTPException(404,detail='Enlace no válido') from None
    row=await db.get(EmailSubscription,row_id)
    if not row or not check_manage_token(row,token):
        raise HTTPException(404,detail='Enlace no válido')
    crop=await db.get(Crop,row.crop_id) if row.crop_id else None;station=await db.get(Station,row.station_id) if row.station_id else None
    return {'email':row.email,'crop':crop.name if crop else '', 'station':station.name if station else '', 'active':row.status=='ACTIVE'}


@router.post('/subscriptions/unsubscribe')
async def unsubscribe(payload:TokenInput,db:DbSession) -> dict:
    try:
        row_id=UUID(payload.token.split('.',1)[0])
    except (ValueError,IndexError):
        raise HTTPException(404,detail='Enlace no válido') from None
    row=await db.get(EmailSubscription,row_id)
    if not row or not check_manage_token(row,payload.token):
        raise HTTPException(404,detail='Enlace no válido')
    changed=row.status!='UNSUBSCRIBED'
    if changed:
        row.status='UNSUBSCRIBED';row.unsubscribed_at=datetime.now(UTC)
        row.telegram_active=False
        row.confirm_hash=None;row.confirm_expires_at=None;row.pending_changes={}
        await db.commit()
    try:
        if changed: await send_mail(_message(row.email,'Baja confirmada · FECOCLIMA IA','Su baja fue procesada',
                                 'Dejamos de enviarle notificaciones para el cultivo y la estación seleccionados. Si desea volver a recibirlas, deberá registrarse y confirmar su correo nuevamente.'))
    except Exception:
        # Un fallo SMTP nunca revierte el consentimiento revocado.
        pass
    return {'message':'Baja confirmada. No recibirá nuevas alertas de FECOCLIMA IA.'}


@router.get('/address-book')
async def address_book(db:DbSession,_:AdminUser,limit:int=100,offset:int=0) -> dict:
    rows=list((await db.scalars(select(EmailSubscription).order_by(EmailSubscription.created_at.desc()).offset(max(0,offset)).limit(min(200,max(1,limit))))).all())
    crops={c.id:c.name for c in (await db.scalars(select(Crop))).all()}
    stations={s.id:s.name for s in (await db.scalars(select(Station))).all()}
    total=await db.scalar(select(func.count()).select_from(EmailSubscription))
    from app.services.notification_audience import subscription_parcels
    linked=await subscription_parcels(db,[r.id for r in rows])
    return {'items':[{**_render_subscription(r,crops.get(r.crop_id,'Todos los cultivos'),stations.get(r.station_id,'Todas las estaciones')),'plots':linked.get(r.id,[])} for r in rows],'total':total}


class SubscriptionParcelsInput(BaseModel):
    plot_ids:list[UUID]=Field(max_length=200)


@router.get('/address-book/parcel-options')
async def address_book_parcel_options(db:DbSession,_:AdminUser)->dict:
    plots=(await db.scalars(select(Plot).where(Plot.active.is_(True),Plot.deleted_at.is_(None),Plot.crop_id.is_not(None)).order_by(Plot.name))).all()
    return {'items':[{'id':str(p.id),'name':p.name} for p in plots]}


@router.put('/address-book/{subscription_id}/parcels')
async def assign_subscription_parcels(subscription_id:UUID,payload:SubscriptionParcelsInput,request:Request,db:DbSession,actor:AdminUser)->dict:
    row=await db.get(EmailSubscription,subscription_id)
    if not row:raise HTTPException(404,detail='Contacto no encontrado.')
    if row.status not in ('ACTIVE','DISABLED') or not row.confirmed_at:
        raise HTTPException(409,detail='Confirme primero la suscripción; no se pueden vincular contactos dados de baja.')
    ids=set(payload.plot_ids)
    active=set((await db.scalars(select(Plot.id).where(Plot.id.in_(ids),Plot.active.is_(True),Plot.deleted_at.is_(None),Plot.crop_id.is_not(None)))).all()) if ids else set()
    if active!=ids:raise HTTPException(422,detail='Seleccione únicamente parcelas activas.')
    old=list((await db.scalars(select(SubscriptionPlot.plot_id).where(SubscriptionPlot.subscription_id==row.id))).all())
    await db.execute(delete(SubscriptionPlot).where(SubscriptionPlot.subscription_id==row.id))
    for plot_id in ids:db.add(SubscriptionPlot(subscription_id=row.id,plot_id=plot_id))
    await write_audit(db,action='ADDRESS_BOOK_PARCELS',user=actor,request=request,entity_type='email_subscription',entity_id=str(row.id),before={'plot_ids':[str(i) for i in old]},after={'plot_ids':[str(i) for i in ids]})
    await db.commit()
    return {'message':'Parcelas vinculadas. Solo recibirá avisos de estaciones que cubran esas parcelas.'}


@router.patch('/address-book/{subscription_id}')
async def edit_subscription(subscription_id:UUID,payload:SubscriptionStatusInput,request:Request,db:DbSession,actor:AdminUser)->dict:
    row=await db.get(EmailSubscription,subscription_id)
    if not row: raise HTTPException(404,detail='Suscripción no encontrada.')
    if payload.active:
        if row.status != 'DISABLED' or not row.confirmed_at:
            raise HTTPException(409,detail='Solo puede reactivar una suscripción confirmada que usted haya desactivado. Las bajas voluntarias requieren un nuevo consentimiento.')
        row.status='ACTIVE';row.unsubscribed_at=None
        message='Suscripción reactivada.'
    else:
        if row.status in ('UNSUBSCRIBED','DISABLED'): return {'message':'La suscripción ya se encuentra inactiva.'}
        row.status='DISABLED';row.unsubscribed_at=datetime.now(UTC)
        row.telegram_active=False
        message='Suscripción desactivada.'
    if not payload.active:
        row.confirm_hash=None;row.confirm_expires_at=None;row.pending_changes={}
    await write_audit(db,action='ADDRESS_BOOK_STATUS',user=actor,request=request,entity_type='email_subscription',entity_id=str(row.id),before={'status':'ACTIVE' if not payload.active else 'DISABLED'},after={'status':row.status})
    await db.commit()
    if not payload.active and row.confirmed_at:
        try:
            await send_mail(_message(row.email,'Actualización de suscripción · FECOCLIMA IA',
                                     'Suscripción desactivada',
                                     'El administrador desactivó las notificaciones para una de sus estaciones y cultivos. No recibirá nuevos mensajes de esta suscripción.',
                                     unsubscribe_url=public_url('?baja='+manage_token(row))))
        except Exception:
            return {'message':message+' No se pudo enviar el correo informativo; revise SMTP.', 'mail_sent':False}
    return {'message':message,'mail_sent':True}


def _event_dict(item:NotificationEvent) -> dict:
    result = model_dict(item)
    result['next_run_at'] = (item.last_checked_at + timedelta(minutes=item.every_minutes)) if item.active and item.last_checked_at else None
    return result


@router.post('/events/parse')
async def parse_event(payload:EventInput,db:DbSession,_:AdminUser)->dict:
    try: rule=await compile_notification_prompt(db, payload.prompt)
    except ValueError as exc: raise HTTPException(422,detail=str(exc)) from exc
    await db.commit()
    return {'compiled_rule':rule,'message':'Condiciones reconocidas. Se comprobarán mediante mediciones reales.'}


@router.get('/events')
async def list_events(db:DbSession,_:AdminUser)->dict:
    rows=list((await db.scalars(select(NotificationEvent).order_by(NotificationEvent.created_at.desc()).limit(200))).all())
    return {'items':[_event_dict(r) for r in rows]}


async def _save_event(db:DbSession,payload:EventInput,row:NotificationEvent|None=None)->NotificationEvent:
    try:
        # Reuse the stored interpretation for name/status changes. Interpret
        # a new or edited prompt once, never at every monitoring interval.
        rule = row.compiled_rule if row and row.prompt == payload.prompt and row.compiled_rule else await compile_notification_prompt(db, payload.prompt)
        ZoneInfo(payload.timezone)
    except (ValueError,ZoneInfoNotFoundError) as exc:
        raise HTTPException(422,detail=str(exc)) from exc
    for model, value in ((Crop, payload.crop_id), (Station, payload.station_id)):
        if value:
            scoped = await db.get(model, value)
            if not scoped or (payload.active and (scoped.deleted_at or not scoped.active)):
                raise HTTPException(422, detail='El cultivo y la estación seleccionados deben estar activos.')
    if rule.get('max_hours',0) > payload.lookback_hours:
        raise HTTPException(422, detail='El período de análisis debe cubrir la duración solicitada en el prompt.')
    if row is None: row=NotificationEvent();db.add(row)
    identity_fields = ('prompt','crop_id','station_id','channel','cadence','timezone','schedule_date','schedule_hour','schedule_day','schedule_months','lookback_hours')
    previous = tuple(getattr(row, key) for key in identity_fields) if row.id else None
    for key,value in payload.model_dump().items(): setattr(row,key,value)
    row.compiled_rule=rule
    row.schedule_months=[];row.schedule_date=None
    if previous and previous != tuple(getattr(row, key) for key in identity_fields):
        row.last_match=False;row.last_checked_at=None;row.station_states={};row.station_cursors={}
    await db.flush(); await db.refresh(row)
    return row


@router.post('/events')
async def add_event(payload:EventInput,request:Request,db:DbSession,actor:AdminUser)->dict:
    row=await _save_event(db,payload)
    await write_audit(db,action='NOTIFICATION_EVENT_CREATED',user=actor,request=request,entity_type='notification_event',entity_id=str(row.id),after={'name':row.name,'active':row.active,'compiled_rule':row.compiled_rule})
    await db.commit()
    return _event_dict(row)


@router.put('/events/{event_id}')
async def update_event(event_id:UUID,payload:EventInput,request:Request,db:DbSession,actor:AdminUser)->dict:
    row=await db.get(NotificationEvent,event_id)
    if not row: raise HTTPException(404,detail='Evento no encontrado.')
    previous={'name':row.name,'active':row.active,'compiled_rule':row.compiled_rule}
    updated=await _save_event(db,payload,row)
    await write_audit(db,action='NOTIFICATION_EVENT_UPDATED',user=actor,request=request,entity_type='notification_event',entity_id=str(row.id),before=previous,after={'name':updated.name,'active':updated.active,'compiled_rule':updated.compiled_rule})
    await db.commit()
    return _event_dict(updated)


@router.delete('/events/{event_id}')
async def delete_event(event_id:UUID,request:Request,db:DbSession,actor:AdminUser)->dict:
    row=await db.scalar(select(NotificationEvent).where(NotificationEvent.id==event_id).with_for_update())
    if not row: raise HTTPException(404,detail='Evento no encontrado.')
    from app.db.models import NotificationEventRun
    running=await db.scalar(select(NotificationEventRun.id).where(
        NotificationEventRun.event_id==event_id,NotificationEventRun.status=='RUNNING').limit(1))
    if running:
        raise HTTPException(409,detail='La alerta tiene una ejecución en curso. Espere a que termine antes de eliminarla.')
    await write_audit(db,action='NOTIFICATION_EVENT_DELETED',user=actor,request=request,
                      entity_type='notification_event',entity_id=str(row.id),
                      before={'name':row.name,'code':row.code,'active':row.active})
    await db.delete(row)
    await db.commit()
    return {'message':'Alerta eliminada. Se eliminaron su historial y los avisos pendientes; se conservan las parcelas y los contactos.'}


@router.post('/events/{event_id}/test')
async def test_event(event_id:UUID,db:DbSession,_:AdminUser)->dict:
    event=await db.get(NotificationEvent,event_id)
    if not event:raise HTTPException(404,detail='Evento no encontrado.')
    from app.services.notification_events import preview_event
    return await preview_event(db, event)


@router.post('/events/{event_id}/run')
async def run_event_now(event_id:UUID,db:DbSession,_:AdminUser)->dict:
    event=await db.get(NotificationEvent,event_id)
    if not event or not event.active:
        raise HTTPException(409,detail='Primero guarde y active el evento.')
    from app.services.notification_cycles import queue_event_cycle
    run,queued=await queue_event_cycle(db,event_id,mode='MANUAL',force=True)
    if not run:
        raise HTTPException(409,detail='El servicio está deshabilitado o hay otra ejecución en curso. Revise Configuración y el historial.')
    return {'queued':queued,'run_id':str(run.id),'message':'Ejecución completa en cola; se enviará si se detectan episodios nuevos.'}


@router.post('/events/{event_id}/simulate')
async def simulate_event_cycle(event_id:UUID,db:DbSession,_:AdminUser)->dict:
    from app.services.notification_cycles import queue_event_cycle
    if not await db.get(NotificationEvent,event_id):
        raise HTTPException(404,detail='Alerta no encontrada.')
    run,queued=await queue_event_cycle(db,event_id,mode='SIMULATION',force=True)
    if not run:
        raise HTTPException(409,detail='La alerta está ocupada. Intente de nuevo.')
    return {'queued':queued,'run_id':str(run.id),'message':'Simulación en cola. No se enviarán avisos.'}


@router.get('/events/{event_id}/runs')
async def event_cycle_runs(event_id:UUID,db:DbSession,_:AdminUser)->dict:
    from app.db.models import NotificationEventRun
    rows=(await db.scalars(select(NotificationEventRun).where(NotificationEventRun.event_id==event_id)
                           .order_by(NotificationEventRun.created_at.desc()).limit(30))).all()
    return {'items':[model_dict(r) for r in rows]}


@router.get('/events/{event_id}/runs/{run_id}')
async def event_cycle_run(event_id:UUID,run_id:UUID,db:DbSession,_:AdminUser)->dict:
    from app.db.models import NotificationEventRun
    row=await db.get(NotificationEventRun,run_id)
    if not row or row.event_id!=event_id:
        raise HTTPException(404,detail='Ejecución no encontrada.')
    return model_dict(row)


@router.get('/events/{event_id}/deliveries')
async def delivery_logs(event_id:UUID,db:DbSession,_:AdminUser)->dict:
    return {'items':[model_dict(r) for r in (await db.scalars(select(NotificationEventDelivery).where(NotificationEventDelivery.event_id==event_id).order_by(NotificationEventDelivery.created_at.desc()).limit(100))).all()]}
