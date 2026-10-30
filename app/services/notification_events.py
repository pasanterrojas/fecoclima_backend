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
from sqlalchemy import func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (Crop, EmailSubscription, NotificationEvent,
                           NotificationEventDelivery, NotificationRequestLog,
                           Station, WeatherReading)


def ascii_text(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))


def compile_event_prompt(prompt: str) -> dict[str, Any]:
    """Legacy v1/v2 compiler retained for regression checks, never for new API prompts.

    New alerts use notification_rules.compile_notification_prompt.
    """
    text = re.sub(r'\s+', ' ', ascii_text(prompt)).strip()
    if len(text) < 15 or len(text) > 2000:
        raise ValueError('Describa la condición en una frase de 15 a 2000 caracteres.')
    # The risk description is retained as context, never interpreted as a diagnosis.
    narrative = re.search(r'\s+(?:entonces\s+)?(?:es\s+propens[oa]\s+a|hay\s+riesgo\s+de|riesgo\s+de)\s+(.+)$', text)
    risk = narrative.group(1).strip(' .') if narrative else None
    if narrative:
        text = text[:narrative.start()]
    clauses: list[dict] = []
    span = r'\s*(?:de|a|que|al|el|la)?\s*'
    h = re.search(r'hum(?:edad)?(?:\s+relativa)?\s+(?:sea\s+)?(?:mayor|superior)\s+(?:a|al|de)\s*(\d+(?:[.,]\d+)?)\s*%\s*(?:(?:por\s+(?:mas\s+de|al\s+menos|durante)|durante)\s*(\d+(?:[.,]\d+)?)\s*horas?(?:\s+(?:seguidas|consecutivas))?)?', text)
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
    duration = max([float(c.get('hours') or 0) for c in clauses] + [0])
    # Version 2: simultaneous temperature and humidity throughout the sustained window.
    if duration:
        for clause in clauses:
            if clause['type'] == 'temperature_between':
                clause['hours'] = duration
    return {'version':2, 'operator':'ALL', 'clauses':clauses, 'max_hours':duration, 'risk_description':risk}


def _fresh(reading: WeatherReading, now: datetime, tolerance_minutes: int) -> bool:
    return abs((now - reading.sampled_at).total_seconds()) <= tolerance_minutes * 60


def evaluate_readings(rule: dict, readings: list[WeatherReading], now: datetime, interval_minutes: int) -> dict:
    """Retorna TRUE/FALSE/UNKNOWN; UNKNOWN nunca activa envíos."""
    if rule.get('version') == 3:
        from app.services.notification_rules import evaluate_expression_rule
        return evaluate_expression_rule(rule, readings, now, interval_minutes)
    valid = sorted((r for r in readings if r.sampled_at and r.sampled_at <= now), key=lambda x: x.sampled_at)
    tolerance = min(180, max(40, int(interval_minutes or 10) * 3))
    if not valid or not _fresh(valid[-1], now, tolerance):
        return {'state':'UNKNOWN', 'detail':'No hay lecturas meteorológicas recientes para validar el evento.'}
    if not rule.get('clauses'):
        return {'state':'UNKNOWN', 'detail':'No hay condiciones verificables configuradas.'}
    latest = valid[-1]
    results = []
    for clause in rule.get('clauses', []):
        typ = clause['type']
        if typ == 'temperature_between':
            x = latest.temperature_c
            if x is None:
                return {'state':'UNKNOWN','detail':'La última lectura no incluye temperatura.'}
            hours = clause.get('hours') or 0
            check = [latest]
            if hours:
                cutoff = now - timedelta(hours=hours)
                first = next((r for r in reversed(valid) if cutoff - timedelta(minutes=tolerance) <= r.sampled_at <= cutoff), None)
                if first is None:
                    return {'state':'UNKNOWN','detail':'No hay historial suficiente para comprobar la temperatura durante todo el período.'}
                check = [first] + [r for r in valid if r.sampled_at > cutoff]
                if any(r.temperature_c is None for r in check) or any((b.sampled_at-a.sampled_at).total_seconds() > tolerance*60 for a,b in zip(check,check[1:])):
                    return {'state':'UNKNOWN','detail':'Faltan lecturas continuas de temperatura durante el período solicitado.'}
            ok = all(clause['min'] <= r.temperature_c <= clause['max'] for r in check)
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


async def preview_event(db: AsyncSession, event: NotificationEvent) -> dict:
    if not event.station_id:return {'state':'UNKNOWN','detail':'Seleccione una estación para esta alerta.','stations':[]}
    now = datetime.now(UTC)
    query = select(Station).where(Station.active.is_(True), Station.deleted_at.is_(None))
    if event.station_id:
        query = query.where(Station.id == event.station_id)
    stations = list((await db.scalars(query.order_by(Station.name))).all())
    results = []
    since = now - timedelta(hours=max(1, event.compiled_rule.get('max_hours', 0)) + 3)
    for station in stations:
        readings = list((await db.scalars(select(WeatherReading).where(
            WeatherReading.station_id == station.id, WeatherReading.sampled_at >= since
        ).order_by(WeatherReading.sampled_at).limit(100000))).all())
        result = evaluate_readings(event.compiled_rule, readings, now, station.expected_interval_minutes)
        results.append({'station_id': str(station.id), 'station': station.name, **result})
    state = 'TRUE' if any(r['state'] == 'TRUE' for r in results) else ('FALSE' if results and all(r['state'] == 'FALSE' for r in results) else 'UNKNOWN')
    return {'state': state, 'detail': ' · '.join(f"{r['station']}: {r['detail']}" for r in results) or 'No hay estaciones activas.', 'stations': results}


async def notifier_content(db: AsyncSession, event: NotificationEvent, matched: list[dict]) -> dict:
    """Use the published notifier prompt; keep the decision outside the LLM."""
    from app.db.models import AIAgentRun
    from app.services.agent_runtime import select_agent_version
    from app.services.ai import generate_markdown_response
    agent, version = await select_agent_version(db, None, 'DAILY_DIGEST')
    if not agent or not version:
        raise ValueError('Configure y publique el agente notificador desde Configuración.')
    context = {'agent_purpose': 'NOTIFICATION_ALERT', 'alert_code': event.code,
               'alert_name': event.name, 'condition_prompt': event.prompt,
               'verified_rule': event.compiled_rule, 'observations': matched,
               'evaluated_at': datetime.now(UTC).isoformat(),
               'parcel_analyses':getattr(event,'_cycle_analyses',[]),'parcels':getattr(event,'_cycle_plots',[])}
    run = AIAgentRun(agent_id=agent.id, agent_version_id=version.id, status='RUNNING',
                     model=version.model, input_payload=context, started_at=datetime.now(UTC))
    db.add(run)
    await db.flush()
    try:
        result = await generate_markdown_response(
            version.system_prompt + '\nLa condición ya fue verificada con mediciones. Usa solo el contexto. No cambies los umbrales ni inventes diagnósticos. Devuelve Markdown sin HTML ni datos de destinatarios.',
            context, 'Redacta el aviso de esta alerta siguiendo las instrucciones de contenido del agente. Identifica las estaciones y las fechas de observación.',
            version.model, provider=version.provider or 'OPENAI', temperature=version.temperature,
            max_tokens=min(version.max_tokens or 1600, 3000))
        if result.get('fallback') or not result.get('markdown', '').strip():
            raise ValueError('El proveedor IA del agente notificador no está disponible; se reintentará la evaluación.')
        run.status = 'COMPLETED'
        run.output_payload = {'markdown': result['markdown']}
        return {'markdown': result['markdown'], 'agent_run_id': str(run.id), 'stations': matched,
                'subject': f'FECOCLIMA IA · {event.code} · {event.name}'}
    except Exception as exc:
        run.status = 'ERROR'
        run.error_message = str(exc)[:1500]
        raise
    finally:
        run.finished_at = datetime.now(UTC)


async def plan_event_deliveries(db:AsyncSession,event:NotificationEvent,matched:list[dict],*,simulation=False)->list[dict]:
    from app.services.notification_audience import event_audience
    from app.services.producer_notifications import get_notification_integration
    integration=await get_notification_integration(db)
    config=integration.non_sensitive_config or {} if integration else {}
    targets=await event_audience(db,event)
    plans=[];cache={}
    original_analyses=getattr(event,'_cycle_analyses',[])
    original_plots=getattr(event,'_cycle_plots',[])
    for target in targets:
        row=target['subscription']
        plots=target['plots'];plot_ids=sorted(p['plot_id'] for p in plots)
        for channel in ('EMAIL','TELEGRAM'):
            allowed=(row.email_enabled and (simulation or config.get('email_enabled'))) if channel=='EMAIL' else (row.telegram_enabled and row.telegram_active and row.telegram_chat_id and (simulation or config.get('telegram_enabled')))
            if not allowed:continue
            # A channel linked later can receive an ongoing episode once, without
            # repeating it for contacts/channels already covered by the outbox.
            existing=list((await db.scalars(select(NotificationEventDelivery).where(
                NotificationEventDelivery.event_id==event.id,NotificationEventDelivery.subscription_id==row.id,
                NotificationEventDelivery.channel==channel,NotificationEventDelivery.status.in_(['QUEUED','ERROR','SENT'])))).all()) if not simulation else []
            known={key for delivery in existing for key in (delivery.payload or {}).get('episode_keys',[])}
            stations=[];keys=[]
            for station in matched:
                episodes=[]
                for episode in station.get('episodes',[]):
                    key=station['station_id']+':'+episode['observed_at']
                    if key not in known:episodes.append(episode);keys.append(key)
                if station.get('episodes') and not episodes:continue
                stations.append({**station,'episodes':episodes,'detail':(' · '.join(e['observed_at']+': '+e['detail'] for e in episodes) if episodes else station.get('detail',''))})
            if not stations:continue
            cache_key=(tuple(plot_ids),tuple(sorted(keys)))
            if cache_key not in cache:
                event._cycle_analyses=[analysis for analysis in original_analyses if analysis.get('plot_id') in plot_ids]
                event._cycle_plots=plots
                try:content=await notifier_content(db,event,stations)
                finally:event._cycle_analyses=original_analyses;event._cycle_plots=original_plots
                cache[cache_key]={**content,'plot_ids':plot_ids,'plots':plots,'station_id':str(event.station_id),'episode_keys':sorted(keys)}
            plans.append({'subscription':row,'channel':channel,'content':cache[cache_key],'plots':plots})
    return plans


async def evaluate_event(db: AsyncSession, event: NotificationEvent, *, force: bool = False, send: bool = True, prepared_result: dict | None = None) -> dict:
    # Tests/previews never consume an edge or mutate the alert's monitoring state.
    if not send:
        return await preview_event(db, event)
    # Serialize workers on this alert through queue creation and state persistence.
    event = await db.scalar(select(NotificationEvent).where(NotificationEvent.id == event.id)
                            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if not event or not event.active:
        return {'status': 'SKIPPED'}
    now = datetime.now(UTC)
    if not force and not is_due(event, now):
        await db.commit()
        return {'status': 'NOT_DUE'}
    from app.services.producer_notifications import get_notification_integration
    integration = await get_notification_integration(db)
    if integration:
        await db.refresh(integration)
    config = (integration.non_sensitive_config or {}) if integration else {}
    if not integration or not integration.active or not config.get('active'):
        await db.commit()
        return {'status': 'DISABLED'}
    result = prepared_result if prepared_result is not None else await preview_event(db, event)
    states = dict(event.station_states or {})
    matched = ([r for r in result['stations'] if r.get('matched')] if result.get('historical') else
               [r for r in result['stations'] if r['state'] == 'TRUE' and not states.get(r['station_id'], False)])
    matched=[r for r in matched if r['station_id']==str(event.station_id)]
    plans=[]
    if matched:
        try:
            plans=await plan_event_deliveries(db,event,matched)
        except Exception as exc:
            event.last_checked_at=now;event.last_status='CONTENT_ERROR';event.last_detail=str(exc)[:1500]
            await db.commit()
            return {'status':'CONTENT_ERROR','detail':event.last_detail}
    if plans:
        event.trigger_number+=1;event.last_triggered_at=now
        for plan in plans:
            row=plan['subscription'];channel=plan['channel']
            db.add(NotificationEventDelivery(event_id=event.id,subscription_id=row.id,
                trigger_number=event.trigger_number,channel=channel,
                destination_hint=row.email if channel=='EMAIL' else row.telegram_chat_id,
                status='QUEUED',payload=plan['content']))
    for r in result['stations']:
        if r['state'] in ('TRUE', 'FALSE'):
            states[r['station_id']] = r['state'] == 'TRUE'
    event.station_states = states
    if result.get('historical'):
        event.station_cursors = result['cursors']
    event.last_match = result['state'] == 'TRUE'
    event.last_checked_at = now
    event.last_status = result['state']
    event.last_detail = result['detail'][:4000]
    # Queue and transition commit together. A crash cannot consume an edge without a durable delivery.
    await db.commit()
    return {'status': result['state'], 'triggered': bool(matched), 'queued': len(plans), 'detail': result['detail']}


async def dispatch_event_deliveries(db: AsyncSession) -> dict:
    from app.services.producer_notifications import get_notification_integration, telegram_bot_token, _send_telegram, _email_message, _markdown_to_plain
    sent = errors = 0
    # One row per transaction, holding its lock until the delivery result is stored.
    for _ in range(100):
        now = datetime.now(UTC)
        row = await db.scalar(select(NotificationEventDelivery).where(
            NotificationEventDelivery.status.in_(['QUEUED', 'ERROR']),
            NotificationEventDelivery.attempts < 5,
            or_(NotificationEventDelivery.next_attempt_at.is_(None), NotificationEventDelivery.next_attempt_at <= now),
        ).order_by(NotificationEventDelivery.created_at).limit(1).with_for_update(skip_locked=True))
        if not row:
            break
        event = await db.get(NotificationEvent, row.event_id, populate_existing=True)
        sub = await db.get(EmailSubscription, row.subscription_id, populate_existing=True) if row.subscription_id else None
        integration = await get_notification_integration(db)
        if integration:
            await db.refresh(integration)
        config = integration.non_sensitive_config or {} if integration else {}
        enabled = bool(integration and integration.active and config.get('active') and config.get('email_enabled' if row.channel == 'EMAIL' else 'telegram_enabled'))
        consented = bool(sub and sub.status == 'ACTIVE' and sub.confirmed_at and
            (sub.email_enabled if row.channel == 'EMAIL' else sub.telegram_enabled and sub.telegram_active and sub.telegram_chat_id))
        if event and sub and consented:
            from app.services.notification_audience import event_audience
            current=next((a for a in await event_audience(db,event) if a['subscription'].id==sub.id),None)
            current_ids={p['plot_id'] for p in current['plots']} if current else set()
            saved_ids=set((row.payload or {}).get('plot_ids',[]))
            consented=bool(saved_ids and saved_ids.issubset(current_ids) and str(event.station_id)==(row.payload or {}).get('station_id'))
        if not event or not event.active or not consented:
            row.status = 'CANCELLED'; row.error = 'Alerta desactivada o consentimiento revocado.'
            await db.commit(); continue
        if not enabled:
            # Pause pending sends without consuming attempts while the global channel is off.
            row.next_attempt_at = now + timedelta(minutes=5)
            await db.commit(); continue
        row.attempts += 1
        try:
            content = row.payload or {}
            if not content.get('markdown'):
                raise ValueError('Entrega antigua sin contenido; requiere revisión.')
            unsubscribe_url = public_url('?baja=' + manage_token(sub))
            if row.channel == 'EMAIL':
                msg = _email_message(sub.email, content['subject'], content['markdown'] + '\n\nCancelar suscripción: ' + unsubscribe_url)
                msg['List-Unsubscribe'] = '<' + unsubscribe_url + '>'
                html_part = msg.get_body(preferencelist=('html',))
                if html_part:
                    html_part.set_content(html_part.get_content().replace('</div></body>', f'<p><a href="{escape(unsubscribe_url, quote=True)}">Cancelar suscripción</a></p></div></body>'), subtype='html')
                await send_mail(msg)
            else:
                token = telegram_bot_token(integration)
                if not token:
                    raise ValueError('Configure el bot de Telegram.')
                text = _markdown_to_plain(content['markdown']) + '\n\nCancelar suscripción: ' + unsubscribe_url + '\nPara dejar de recibir Telegram envíe /stop.'
                # Telegram limit 4096; send a complete short notice without splitting into duplicates.
                if len(text) > 4096:
                    text = _markdown_to_plain(content['markdown'])[:3300] + '…\n\nCancelar suscripción: ' + unsubscribe_url + '\nPara dejar de recibir Telegram envíe /stop.'
                await _send_telegram(token, sub.telegram_chat_id, text)
            row.status = 'SENT'; row.delivered_at = datetime.now(UTC); row.error = None
            sub.last_notified_at = row.delivered_at
            sent += 1
        except Exception as exc:
            row.status = 'ERROR'; row.error = str(exc)[:1500]
            row.next_attempt_at = now + timedelta(minutes=min(60, 5 * 2 ** (row.attempts - 1)))
            errors += 1
        await db.commit()
    return {'sent': sent, 'errors': errors}


async def process_scheduled_events(db: AsyncSession) -> dict:
    from app.services.notification_cycles import queue_event_cycle
    ids=list((await db.scalars(select(NotificationEvent.id).where(NotificationEvent.active.is_(True)))).all())
    queued=0;errors=[]
    for event_id in ids:
        try:
            _, created=await queue_event_cycle(db,event_id)
            queued+=int(created)
        except Exception as exc:
            await db.rollback();errors.append(str(exc)[:200])
    dispatch=await dispatch_event_deliveries(db)
    return {'cycles_queued':queued,**dispatch,'evaluation_errors':errors}
