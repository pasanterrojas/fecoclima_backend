"""Suscripciones voluntarias, libreta de direcciones y reglas del notificador."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, select, or_, delete

from app.api.deps import AdminUser, DbSession
from app.db.models import (Crop, EmailSubscription, NotificationEvent,
                           NotificationEventDelivery, NotificationRequestLog, Station, WeatherReading)
from app.services.notification_events import (
    _message, check_manage_token, compile_event_prompt, digest,
    evaluate_event, evaluate_readings, manage_token, public_url, send_mail,
)
from app.services.serialization import model_dict
from app.services.audit import write_audit
from app.services.jobs import enqueue_unique

router = APIRouter(prefix='/notifications', tags=['Suscripciones y eventos'])


class SubscriptionInput(BaseModel):
    email: EmailStr
    crop_id: UUID
    station_id: UUID
    consent: bool
    website: str = Field(default='', max_length=200)  # Campo trampa anti-bot


class TokenInput(BaseModel):
    token: str = Field(min_length=35, max_length=250)


class EventInput(BaseModel):
    name: str = Field(min_length=3,max_length=180)
    prompt: str = Field(min_length=15,max_length=2000)
    crop_id: UUID
    station_id: UUID
    timezone: str = Field(default='America/Asuncion',max_length=80)
    cadence: Literal['INTERVAL','DAILY','MONTHLY','ONCE'] = 'INTERVAL'
    every_minutes: int = Field(default=30,ge=5,le=10080)
    schedule_hour: int = Field(default=8,ge=0,le=23)
    schedule_day: int = Field(default=1,ge=1,le=28)
    schedule_months: list[int] = Field(default_factory=list)
    schedule_date: date | None = None
    channel: Literal['EMAIL','TELEGRAM','BOTH'] = 'EMAIL'

    @field_validator('schedule_months')
    @classmethod
    def valid_months(cls, value: list[int]) -> list[int]:
        if any(month < 1 or month > 12 for month in value):
            raise ValueError('Seleccione meses entre enero y diciembre.')
        return sorted(set(value))

    @model_validator(mode='after')
    def valid_calendar(self):
        if self.cadence == 'ONCE':
            if self.schedule_date is None:
                raise ValueError('Seleccione la fecha de la evaluación única.')
            if self.schedule_months and self.schedule_date.month not in self.schedule_months:
                raise ValueError('La fecha debe pertenecer a un mes habilitado.')
        return self
    active: bool = False


class SubscriptionStatusInput(BaseModel):
    active: bool


def _render_subscription(row:EmailSubscription,crop:str,station:str) -> dict:
    return {'id':str(row.id),'email':row.email,'crop_id':str(row.crop_id),'crop':crop,
            'station_id':str(row.station_id),'station':station,'status':row.status,
            'confirmed_at':row.confirmed_at,'unsubscribed_at':row.unsubscribed_at,
            'created_at':row.created_at,'last_notified_at':row.last_notified_at,
            'telegram_active':row.telegram_active}


@router.get('/subscriptions/options')
async def subscription_options(db:DbSession) -> dict:
    crops=list((await db.scalars(select(Crop).where(Crop.active.is_(True),Crop.deleted_at.is_(None)).order_by(Crop.name))).all())
    stations=list((await db.scalars(select(Station).where(Station.active.is_(True),Station.deleted_at.is_(None)).order_by(Station.name))).all())
    return {'crops':[{'id':str(c.id),'name':c.name} for c in crops],
            'stations':[{'id':str(s.id),'name':s.name,'latitude':s.latitude,'longitude':s.longitude,'locality':s.locality} for s in stations]}


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
    crop=await db.get(Crop,payload.crop_id)
    station=await db.get(Station,payload.station_id)
    if not crop or not crop.active or crop.deleted_at or not station or not station.active or station.deleted_at:
        await db.commit()
        return accepted
    row=await db.scalar(select(EmailSubscription).where(EmailSubscription.email==email,EmailSubscription.crop_id==crop.id,EmailSubscription.station_id==station.id))
    if row and row.status=='ACTIVE':
        await db.commit()
        return accepted
    token=secrets.token_urlsafe(32)
    if not row:
        row=EmailSubscription(email=email,crop_id=crop.id,station_id=station.id,manage_secret=secrets.token_hex(32))
        db.add(row)
    row.status='PENDING';row.confirm_hash=hashlib.sha256(token.encode()).hexdigest()
    row.confirm_expires_at=now+timedelta(hours=24)
    await db.commit()
    link=public_url('?confirmar='+token)
    msg=_message(email,'Confirme su suscripción · FECOCLIMA IA','Confirme que desea recibir alertas',
                 f'Solicitó notificaciones para {crop.name} en la estación {station.name}. Confirme en las próximas 24 horas. Si no fue usted, ignore este mensaje.',
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
    if not row or not row.confirm_expires_at or row.confirm_expires_at<datetime.now(UTC) or row.status!='PENDING':
        raise HTTPException(400,detail='Enlace inválido o vencido. Solicite una nueva confirmación.')
    row.status='ACTIVE';row.confirm_hash=None;row.confirm_expires_at=None
    row.confirmed_at=datetime.now(UTC);row.unsubscribed_at=None
    await db.commit()
    from app.services.producer_notifications import get_notification_integration, telegram_bot_token
    integration=await get_notification_integration(db)
    config=integration.non_sensitive_config or {} if integration else {}
    username=str(config.get('telegram_bot_username') or '').lstrip('@')
    bot_ready=bool(integration and telegram_bot_token(integration) and username)
    telegram_code='fc_'+row.id.hex+'_'+digest(f'tg:{row.id}:{row.manage_secret}')[:24]
    telegram_url=f'https://t.me/{username}?start={telegram_code}' if bot_ready else None
    return {'message':'Suscripción confirmada. Ya puede recibir alertas de su cultivo y estación.','telegram_url':telegram_url}


@router.get('/subscriptions/manage')
async def preview_unsubscribe(token:str,db:DbSession) -> dict:
    try:
        row_id=UUID(token.split('.',1)[0])
    except (ValueError,IndexError):
        raise HTTPException(404,detail='Enlace no válido') from None
    row=await db.get(EmailSubscription,row_id)
    if not row or not check_manage_token(row,token):
        raise HTTPException(404,detail='Enlace no válido')
    crop=await db.get(Crop,row.crop_id);station=await db.get(Station,row.station_id)
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
        row.confirm_hash=None;row.confirm_expires_at=None
        await db.commit()
    try:
        if changed: await send_mail(_message(row.email,'Baja confirmada · FECOCLIMA IA','Su baja fue procesada',
                                 'Dejamos de enviarle notificaciones para el cultivo y la estación seleccionados. Si desea volver a recibirlas, deberá registrarse y confirmar su correo nuevamente.'))
    except Exception:
        # Un fallo SMTP nunca revierte el consentimiento revocado.
        pass
    return {'message':'Baja confirmada. No recibirá nuevas alertas para esa estación y cultivo.'}


@router.get('/address-book')
async def address_book(db:DbSession,_:AdminUser,limit:int=100,offset:int=0) -> dict:
    rows=list((await db.scalars(select(EmailSubscription).order_by(EmailSubscription.created_at.desc()).offset(max(0,offset)).limit(min(200,max(1,limit))))).all())
    crops={c.id:c.name for c in (await db.scalars(select(Crop))).all()}
    stations={s.id:s.name for s in (await db.scalars(select(Station))).all()}
    total=await db.scalar(select(func.count()).select_from(EmailSubscription))
    return {'items':[_render_subscription(r,crops.get(r.crop_id,'Cultivo eliminado'),stations.get(r.station_id,'Estación eliminada')) for r in rows],'total':total}


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
    return model_dict(item)


@router.post('/events/parse')
async def parse_event(payload:EventInput,db:DbSession,_:AdminUser)->dict:
    try: rule=compile_event_prompt(payload.prompt)
    except ValueError as exc: raise HTTPException(422,detail=str(exc)) from exc
    return {'compiled_rule':rule,'message':'Condiciones reconocidas. Se comprobarán mediante mediciones reales.'}


@router.get('/events')
async def list_events(db:DbSession,_:AdminUser)->dict:
    rows=list((await db.scalars(select(NotificationEvent).order_by(NotificationEvent.created_at.desc()).limit(200))).all())
    return {'items':[_event_dict(r) for r in rows]}


async def _save_event(db:DbSession,payload:EventInput,row:NotificationEvent|None=None)->NotificationEvent:
    try:
        rule=compile_event_prompt(payload.prompt)
        ZoneInfo(payload.timezone)
    except (ValueError,ZoneInfoNotFoundError) as exc:
        raise HTTPException(422,detail=str(exc)) from exc
    crop=await db.get(Crop,payload.crop_id);station=await db.get(Station,payload.station_id)
    if not crop or crop.deleted_at or not crop.active or not station or station.deleted_at or not station.active:
        raise HTTPException(422,detail='El cultivo y la estación deben estar activos.')
    if payload.active and payload.channel in ('TELEGRAM','BOTH'):
        from app.services.producer_notifications import get_notification_integration, telegram_bot_token
        integration=await get_notification_integration(db)
        if not telegram_bot_token(integration):
            raise HTTPException(409,detail='Para activar Telegram debe configurar antes el bot desde Boletines diarios.')
    if row is None: row=NotificationEvent();db.add(row)
    identity_fields = ('prompt','crop_id','station_id','channel','cadence','timezone','schedule_date','schedule_hour','schedule_day','schedule_months','every_minutes')
    previous = tuple(getattr(row, key) for key in identity_fields) if row.id else None
    for key,value in payload.model_dump().items(): setattr(row,key,value)
    row.compiled_rule=rule
    if previous and previous != tuple(getattr(row, key) for key in identity_fields):
        row.last_match=False;row.last_checked_at=None
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
    row=await db.get(NotificationEvent,event_id)
    if not row: raise HTTPException(404,detail='Evento no encontrado.')
    row.active=False
    await write_audit(db,action='NOTIFICATION_EVENT_PAUSED',user=actor,request=request,entity_type='notification_event',entity_id=str(row.id),after={'active':False})
    await db.commit()
    return {'message':'Evento desactivado. Se conservan sus registros de entrega.'}


@router.post('/events/{event_id}/test')
async def test_event(event_id:UUID,db:DbSession,_:AdminUser)->dict:
    event=await db.get(NotificationEvent,event_id)
    if not event:raise HTTPException(404,detail='Evento no encontrado.')
    station=await db.get(Station,event.station_id)
    now=datetime.now(UTC)
    from datetime import timedelta
    since=now-timedelta(hours=max(1,event.compiled_rule.get('max_hours',0))+3)
    readings=list((await db.scalars(select(WeatherReading).where(WeatherReading.station_id==event.station_id,WeatherReading.sampled_at>=since).order_by(WeatherReading.sampled_at))).all())
    return evaluate_readings(event.compiled_rule,readings,now,station.expected_interval_minutes)


@router.post('/events/{event_id}/run')
async def run_event_now(event_id:UUID,db:DbSession,_:AdminUser)->dict:
    event=await db.get(NotificationEvent,event_id)
    if not event or not event.active:
        raise HTTPException(409,detail='Primero guarde y active el evento.')
    job=await enqueue_unique(db,'NOTIFICATION_EVENT_SINGLE',{'event_id':str(event_id)},priority=18)
    return {'queued':bool(job),'message':'Evento enviado a la cola de evaluación. Solo notificará si pasa de no cumplido a cumplido.'}


@router.get('/events/{event_id}/deliveries')
async def delivery_logs(event_id:UUID,db:DbSession,_:AdminUser)->dict:
    return {'items':[model_dict(r) for r in (await db.scalars(select(NotificationEventDelivery).where(NotificationEventDelivery.event_id==event_id).order_by(NotificationEventDelivery.created_at.desc()).limit(100))).all()]}
