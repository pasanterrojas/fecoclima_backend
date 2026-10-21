"""Condiciones meteorológicas verificables y despachos consentidos.

El lenguaje natural es una interfaz de entrada; NUNCA delegar una decisión de
alarma al modelo generativo. Expresiones no entendidas se rechazan.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from html import escape
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import aiosmtplib
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (Crop, EmailSubscription, NotificationEvent,
                           NotificationEventDelivery, NotificationRequestLog,
                           Station, WeatherReading)


def ascii_text(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))


def compile_event_prompt(prompt: str) -> dict[str, Any]:
    """Compila reglas conservadoras, documentadas y trazables."""
    text = re.sub(r'\s+', ' ', ascii_text(prompt)).strip()
    if len(text) < 15 or len(text) > 2000:
        raise ValueError('Describa la condición en una frase de 15 a 2000 caracteres.')
    clauses: list[dict] = []
    span = r'\s*(?:de|a|que|al|el|la)?\s*'
    h = re.search(r'hum(?:edad)?(?:\s+relativa)?\s+(?:sea\s+)?(?:mayor|superior)\s+(?:a|al|de)\s*(\d+(?:[.,]\d+)?)\s*%\s*(?:por\s+(?:mas\s+de|al\s+menos|durante)\s*(\d+(?:[.,]\d+)?)\s*horas?)?', text)
    if h:
        threshold = float(h.group(1).replace(',', '.'))
        hours = float(h.group(2).replace(',', '.')) if h.group(2) else 0
        if not 0 < threshold <= 100 or not 0 <= hours <= 120:
            raise ValueError('Humedad válida: entre 0 y 100 %; duración: máximo 120 horas.')
        clauses.append({'type':'humidity_above', 'threshold':threshold, 'hours':hours})
        text = text.replace(h.group(0), ' ')
    t = re.search(r'temperatura\s+(?:este\s+)?entre\s+(-?\d+(?:[.,]\d+)?)\s*(?:y|a)\s*(-?\d+(?:[.,]\d+)?)\s*(?:°\s*c|grados(?:\s+celsius)?)?',text)
    if t:
        low, high = (float(v.replace(',', '.')) for v in t.groups())
        if low >= high or low < -80 or high > 70:
            raise ValueError('Indique un rango de temperatura coherente en °C.')
        clauses.append({'type':'temperature_between', 'min':low, 'max':high})
        text = text.replace(t.group(0), ' ')
    rain = re.search(r'(?:lluvia|precipitacion)\s+(?:acumulada\s+)?(?:mayor|superior)\s+(?:a|de)\s*(\d+(?:[.,]\d+)?)\s*mm\s+(?:en|durante)\s+(?:las\s+)?(?:ultimas\s+)?(\d+)\s*horas?',text)
    if rain:
        mm, hours = float(rain.group(1).replace(',', '.')), int(rain.group(2))
        if mm <= 0 or hours < 1 or hours > 120:
            raise ValueError('Indique un total de lluvia positivo en una ventana de 1 a 120 horas.')
        clauses.append({'type':'rain_sum_above','mm':mm,'hours':hours})
        text = text.replace(rain.group(0),' ')
    # Only these connector phrases are accepted; no silent ignoring of unrelated rules.
    leftovers = re.sub(r'\b(cuando|si|se|cumpla|cumplen|y|ademas|entonces|notifica|notificar|avisar|avisa|enviar|envia|un|una|alerta|aviso|a|la|libreta|de|direcciones|los|productores|que|sea|es|el|este|por|correo|telegram|durante|mas|de|horas|hora|grados|celsius|°|c|,|\.|:|;)\b', ' ',text)
    leftovers = re.sub(r'[\s,;:.!¡¿?°]+','',leftovers)
    if not clauses or leftovers:
        raise ValueError('No se pudo interpretar toda la condición. Use, por ejemplo: "Humedad relativa mayor a 85% por más de 10 horas y temperatura entre 15 y 25 grados". No se ejecutarán instrucciones ambiguas.')
    return {'version':1, 'operator':'ALL', 'clauses':clauses, 'max_hours':max([float(c.get('hours') or 0) for c in clauses] + [0])}


def _fresh(reading: WeatherReading, now: datetime, tolerance_minutes: int) -> bool:
    return abs((now - reading.sampled_at).total_seconds()) <= tolerance_minutes * 60


def evaluate_readings(rule: dict, readings: list[WeatherReading], now: datetime, interval_minutes: int) -> dict:
    """Retorna TRUE/FALSE/UNKNOWN; UNKNOWN nunca activa envíos."""
    valid = sorted((r for r in readings if r.sampled_at and r.sampled_at <= now), key=lambda x: x.sampled_at)
    tolerance = min(180, max(40, int(interval_minutes or 10) * 3))
    if not valid or not _fresh(valid[-1], now, tolerance):
        return {'state':'UNKNOWN', 'detail':'No hay lecturas meteorológicas recientes para validar el evento.'}
    latest = valid[-1]
    results = []
    for clause in rule.get('clauses', []):
        typ = clause['type']
        if typ == 'temperature_between':
            x = latest.temperature_c
            if x is None:
                return {'state':'UNKNOWN','detail':'La última lectura no incluye temperatura.'}
            ok = clause['min'] <= x <= clause['max']
            results.append((ok, f'Temperatura {x:.1f} °C (requisito {clause["min"]:g}–{clause["max"]:g} °C)'))
        elif typ == 'humidity_above':
            x = latest.humidity_pct
            if x is None:
                return {'state':'UNKNOWN','detail':'La última lectura no incluye humedad relativa.'}
            hours = clause.get('hours') or 0
            if hours:
                cutoff = now - timedelta(hours=hours)
                window = [r for r in valid if r.sampled_at >= cutoff - timedelta(minutes=tolerance)]
                first = next((r for r in reversed(window) if r.sampled_at <= cutoff and r.humidity_pct is not None),None)
                if first is None:
                    return {'state':'UNKNOWN','detail':f'No hay historial suficiente para comprobar {hours:g} horas de humedad continua.'}
                check = [first] + [r for r in window if r.sampled_at > cutoff]
                for a,b in zip(check,check[1:]):
                    if (b.sampled_at-a.sampled_at).total_seconds() > tolerance*60:
                        return {'state':'UNKNOWN','detail':'Hay una interrupción importante de lecturas durante el período solicitado.'}
                if any(r.humidity_pct is None for r in check):
                    return {'state':'UNKNOWN','detail':'Faltan valores de humedad dentro del período solicitado.'}
                ok = all(r.humidity_pct > clause['threshold'] for r in check)
            else:
                ok = x > clause['threshold']
            results.append((ok, f'Humedad {x:.1f} % (objetivo >{clause["threshold"]:g} %'+(f' durante {hours:g} h)' if hours else ')')))
        elif typ == 'rain_sum_above':
            cutoff = now - timedelta(hours=clause['hours'])
            window = [r for r in valid if r.sampled_at >= cutoff]
            if not window or not any(r.rain_mm is not None for r in window):
                return {'state':'UNKNOWN','detail':'La estación no informó lluvia en el período solicitado.'}
            near_start = next((r for r in reversed(valid) if cutoff - timedelta(minutes=tolerance) <= r.sampled_at <= cutoff and r.rain_mm is not None),None)
            if not near_start:
                return {'state':'UNKNOWN','detail':'No hay cobertura de lecturas suficiente para la ventana de lluvia solicitada.'}
            previous = near_start
            for item in window:
                if (item.sampled_at-previous.sampled_at).total_seconds()>tolerance*60:
                    return {'state':'UNKNOWN','detail':'Hay una interrupción importante en las lecturas de lluvia.'}
                previous=item
            if any(r.rain_mm is None for r in window):
                return {'state':'UNKNOWN','detail':'Hay lecturas de lluvia incompletas en el período solicitado.'}
            total = sum(max(0, r.rain_mm) for r in window)
            results.append((total > clause['mm'], f'Precipitación acumulada {total:.1f} mm en {clause["hours"]} h (objetivo >{clause["mm"]:g} mm)'))
        else:
            return {'state':'UNKNOWN','detail':'Versión de condición no reconocida.'}
    return {'state':'TRUE' if all(ok for ok,_ in results) else 'FALSE', 'detail': ' · '.join(f'{"✓" if ok else "×"} {message}' for ok,message in results), 'sampled_at':latest.sampled_at.isoformat()}


def is_due(event: NotificationEvent, now: datetime) -> bool:
    if not event.active: return False
    last = event.last_checked_at
    local = now.astimezone(ZoneInfo(event.timezone))
    months = getattr(event, 'schedule_months', None) or []
    if months and local.month not in months:
        return False
    if event.cadence == 'ONCE':
        return (not last and local.date() == getattr(event, 'schedule_date', None)
                and local.hour >= event.schedule_hour)
    if event.cadence not in {'INTERVAL', 'DAILY', 'MONTHLY'}:
        return False
    if event.cadence == 'INTERVAL':
        return not last or now - last >= timedelta(minutes=event.every_minutes)
    local = now.astimezone(ZoneInfo(event.timezone))
    if local.hour < event.schedule_hour: return False
    if event.cadence == 'MONTHLY' and local.day != event.schedule_day: return False
    if last:
        prev = last.astimezone(ZoneInfo(event.timezone))
        if event.cadence == 'DAILY' and prev.date() == local.date(): return False
        if event.cadence == 'MONTHLY' and (prev.year, prev.month) == (local.year, local.month): return False
    return True


def digest(value: str) -> str:
    return hmac.new(settings.jwt_secret.encode(),value.encode(),hashlib.sha256).hexdigest()


def manage_token(row: EmailSubscription) -> str:
    return f'{row.id}.{digest(f"unsub:{row.id}:{row.manage_secret}")}'


def check_manage_token(row: EmailSubscription, token: str) -> bool:
    return hmac.compare_digest(token, manage_token(row))


def public_url(suffix: str) -> str:
    return settings.frontend_base_url.rstrip('/') + '/suscripciones' + suffix


def _message(address: str, subject: str, title: str, body: str, *, action_url: str | None = None, action_text: str = 'Abrir', unsubscribe_url: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg['From'] = settings.smtp_from
    msg['To'] = address
    msg['Subject'] = subject
    plain = title + '\n\n' + body + ('\n\n'+ action_text + ': '+action_url if action_url else '') + ('\n\nDar de baja: '+unsubscribe_url if unsubscribe_url else '')
    msg.set_content(plain)
    html = f'<div style="max-width:640px;margin:auto;font:16px/1.6 Arial,sans-serif;color:#15334a;padding:25px"><div style="background:#004481;color:white;padding:17px 21px;border-radius:12px">FECOCLIMA IA · Notificaciones</div><h2>{escape(title)}</h2><p>{escape(body).replace(chr(10),"<br>")}</p>'
    if action_url:
        html += f'<p><a style="background:#004481;color:white;padding:12px 18px;display:inline-block;text-decoration:none;border-radius:9px" href="{escape(action_url,quote=True)}">{escape(action_text)}</a></p>'
    html += '<hr style="border:none;border-top:1px solid #d9e6ef;margin:25px 0"><p style="font-size:12px;color:#667b8b">Avisos informativos; no sustituyen la revisión de un profesional.</p>'
    if unsubscribe_url:
        html += f'<p style="font-size:12px"><a href="{escape(unsubscribe_url,quote=True)}">Cancelar suscripción</a></p>'
        # Mail applications can surface the native one-click-unsubscribe affordance.
        msg['List-Unsubscribe'] = f'<{unsubscribe_url}>'
    html += '</div>'
    msg.add_alternative(html,subtype='html')
    return msg


async def send_mail(message: EmailMessage) -> None:
    if not settings.smtp_host or settings.smtp_from == 'no-reply@example.com':
        raise RuntimeError('Configure SMTP_HOST y SMTP_FROM con un remitente real antes de enviar.')
    await aiosmtplib.send(message,hostname=settings.smtp_host,port=settings.smtp_port,username=settings.smtp_username,password=settings.smtp_password,start_tls=settings.smtp_starttls,timeout=30)


async def evaluate_event(db: AsyncSession, event: NotificationEvent, *, force: bool = False, send: bool = True) -> dict:
    now = datetime.now(UTC)
    if not force and not is_due(event,now):
        return {'status':'NOT_DUE'}
    station = await db.get(Station,event.station_id)
    crop = await db.get(Crop,event.crop_id)
    if not station or not station.active or station.deleted_at or not crop or not crop.active or crop.deleted_at:
        event.last_checked_at=now; event.last_status='UNKNOWN';event.last_detail='Estación o cultivo inactivo.'
        await db.commit()
        return {'status':'UNKNOWN','detail':event.last_detail}
    since = now-timedelta(hours=max(1,event.compiled_rule.get('max_hours',0))+3)
    readings=list((await db.scalars(select(WeatherReading).where(WeatherReading.station_id==station.id, WeatherReading.sampled_at>=since).order_by(WeatherReading.sampled_at).limit(30000))).all())
    result=evaluate_readings(event.compiled_rule,readings,now,station.expected_interval_minutes)
    state=result['state']
    was_match=bool(event.last_match)
    event.last_checked_at=now
    event.last_status=state
    event.last_detail=result['detail'][:4000]
    triggered=state=='TRUE' and not was_match
    if state in ('TRUE','FALSE'):
        event.last_match=state=='TRUE'
    if triggered:
        event.trigger_number+=1
        event.last_triggered_at=now
    await db.commit()
    if not triggered or not send:
        return {'status':state,'triggered':triggered,'detail':result['detail'],'recipients':0}
    subscribers=list((await db.scalars(select(EmailSubscription).where(EmailSubscription.station_id==event.station_id, EmailSubscription.crop_id==event.crop_id, EmailSubscription.status=='ACTIVE', EmailSubscription.confirmed_at.is_not(None)))).all())
    delivered=0; failed=0
    telegram_token=None
    if event.channel in ('BOTH','TELEGRAM'):
        from app.services.producer_notifications import get_notification_integration, telegram_bot_token
        integration=await get_notification_integration(db)
        telegram_token=telegram_bot_token(integration)
    for row in subscribers:
        for channel in (['EMAIL','TELEGRAM'] if event.channel=='BOTH' else [event.channel]):
            if channel=='TELEGRAM' and (not row.telegram_active or not row.telegram_chat_id):
                continue
            destination=row.email if channel=='EMAIL' else row.telegram_chat_id
            # Unique trigger counter prevents repeat sends by the same rule.
            existing=await db.scalar(select(NotificationEventDelivery).where(NotificationEventDelivery.event_id==event.id, NotificationEventDelivery.subscription_id==row.id,NotificationEventDelivery.trigger_number==event.trigger_number,NotificationEventDelivery.channel==channel))
            if existing: continue
            log=NotificationEventDelivery(event_id=event.id,subscription_id=row.id,trigger_number=event.trigger_number, channel=channel,destination_hint=destination,status='SENDING')
            db.add(log)
            await db.commit()
            url=public_url('?baja='+manage_token(row))
            text=f'Estación: {station.name}\nCultivo: {crop.name}\n\n{result["detail"]}\n\nRevise el cultivo y confirme las condiciones sobre el terreno.'
            try:
                if channel=='EMAIL':
                    await send_mail(_message(row.email,f'FECOCLIMA IA: {event.name}',f'Condición registrada: {event.name}',text,unsubscribe_url=url))
                else:
                    if not telegram_token: raise RuntimeError('Bot Telegram no configurado')
                    from app.services.producer_notifications import _send_telegram
                    await _send_telegram(telegram_token,row.telegram_chat_id,f'FECOCLIMA IA: {event.name}\n\n{text}\n\nPara dejar de recibir Telegram envíe /stop a este bot.')
                log.status='SENT';log.delivered_at=datetime.now(UTC)
                row.last_notified_at=log.delivered_at
                delivered+=1
            except Exception as exc:
                log.status='ERROR';log.error=str(exc)[:1500];failed+=1
            await db.commit()
    return {'status':state,'triggered':True,'recipients':len(subscribers),'sent':delivered,'errors':failed,'detail':result['detail']}


async def process_scheduled_events(db:AsyncSession) -> dict:
    now=datetime.now(UTC)
    events=list((await db.scalars(select(NotificationEvent).where(NotificationEvent.active.is_(True)).order_by(NotificationEvent.created_at).with_for_update(skip_locked=True))).all())
    checked=0; sent=0; errors=[]
    for e in events:
        if not is_due(e,now): continue
        try:
            outcome=await evaluate_event(db,e)
            checked+=1;sent+=outcome.get('sent',0)
        except Exception as exc:
            await db.rollback()
            errors.append(f'{e.name}: {str(exc)[:150]}')
    return {'checked':checked,'emails_sent':sent,'errors':errors}
