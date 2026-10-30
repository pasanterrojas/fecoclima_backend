"""Consent, prompt-driven alerts and durable per-channel deliveries; no external sends."""
from datetime import UTC, datetime, timedelta
import hashlib
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.api.deps import get_current_user, require_csrf
from app.api.endpoints import notifications, subscriptions
from app.db.models import SubscriptionPlot, Plot, Crop, Station, EmailSubscription, NotificationEvent, NotificationEventDelivery, Role, WeatherReading, AIAgentRun
from app.db.session import get_db
from app.services import notification_events as engine
from app.services.producer_notifications import get_notification_integration
from test_plot_integration import DATABASE, setup as integration_setup

pytestmark = pytest.mark.skipif(not DATABASE, reason='Requires disposable FECOCLIMA_TEST_DB_URL')
PROMPT = 'Cuando la humedad relativa sea mayor a 85% durante 10 horas seguidas y la temperatura entre 15 y 25 es propenso a enfermedad de roya'


@pytest.fixture
async def context(integration_setup, monkeypatch):
    db, user, crop, station, _client = integration_setup
    role = await db.get(Role, user.role_id)
    role.code = 'administrador'
    await db.execute(update(WeatherReading).values(temperature_c=19))
    await db.commit()
    app = FastAPI()
    app.include_router(subscriptions.router)
    app.include_router(notifications.router)
    async def database(): yield db
    async def actor(): return user
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = actor
    app.dependency_overrides[require_csrf] = lambda: None
    async def forbidden(*args, **kwargs):
        raise AssertionError('External delivery is forbidden in tests')
    async def legacy_compile(db, prompt): return engine.compile_event_prompt(prompt)
    monkeypatch.setattr(subscriptions, 'compile_notification_prompt', legacy_compile)
    monkeypatch.setattr('aiosmtplib.send', forbidden)
    monkeypatch.setattr('app.services.producer_notifications.telegram_request', forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test.local') as client:
        yield db, user, station, client


async def subscriber(db, *, email=True, telegram=False, linked=False, status='ACTIVE', link_plot=True):
    row = EmailSubscription(email=uuid4().hex+'@example.org', manage_secret=uuid4().hex,
        status=status, confirmed_at=datetime.now(UTC) if status!='PENDING' else None,
        email_enabled=email, telegram_enabled=telegram, telegram_active=linked,
        telegram_chat_id=str(uuid4()) if linked else None, consent_at=datetime.now(UTC), consent_version='notifications-v1')
    db.add(row); await db.flush()
    if link_plot:
        plot=await db.scalar(select(Plot).where(Plot.name=='Parcela de suscripciones'))
        if not plot:
            from test_plot_integration import POLYGON
            crop=await db.scalar(select(Crop));station=await db.scalar(select(Station))
            plot=Plot(name='Parcela de suscripciones',geojson=POLYGON,crop_id=crop.id,station_id=station.id,
                      planting_date=datetime.now(UTC).date()-timedelta(days=60),active=True)
            db.add(plot);await db.flush()
        db.add(SubscriptionPlot(subscription_id=row.id,plot_id=plot.id))
    await db.commit()
    return row


async def prepare(context, monkeypatch):
    db, user, station, client = context
    config = await get_notification_integration(db, create=True)
    config.active = True
    config.non_sensitive_config = {'active':True,'email_enabled':True,'telegram_enabled':True}
    await db.commit()
    response = await client.post('/notifications/events', json={'name':'Riesgo de roya','prompt':PROMPT,'active':True,'station_id':str(station.id)})
    assert response.status_code == 200, response.text
    event = await db.get(NotificationEvent, UUID(response.json()['id']))
    generated = []
    async def content(db, event, matched):
        generated.append(matched)
        return {'markdown':'## Riesgo de roya\nCondiciones observadas. Revisar en campo.', 'subject':event.name,'stations':matched}
    monkeypatch.setattr(engine, 'notifier_content', content)
    return db, client, event, generated


async def test_subscription_records_consent_and_selected_channels(context, monkeypatch):
    db, user, station, client = context
    mail = []
    async def capture(message): mail.append(message)
    monkeypatch.setattr(subscriptions, 'send_mail', capture)
    payload={'email':'productor@example.org','telegram_username':'@productor','email_enabled':False,'telegram_enabled':True,'consent':True}
    bad=await client.post('/notifications/subscriptions/request',json={**payload,'consent':False})
    assert bad.status_code==422
    assert (await client.post('/notifications/subscriptions/request',json={**payload,'telegram_enabled':False})).status_code==422
    assert (await client.post('/notifications/subscriptions/request',json=payload)).status_code==202
    row=await db.scalar(select(EmailSubscription).where(EmailSubscription.email==payload['email']))
    assert row.station_id is None and row.crop_id is None
    assert row.consent_at and row.consent_version=='parcel-alerts-v2'
    assert row.telegram_username=='productor' and row.telegram_enabled and not row.email_enabled
    assert row.status=='PENDING' and not row.telegram_active
    import re
    token=re.search(r'confirmar=([A-Za-z0-9_-]+)',mail[0].get_body(preferencelist=('plain',)).get_content()).group(1)
    assert (await client.post('/notifications/subscriptions/confirm',json={'token':token})).status_code==200
    assert row.status=='ACTIVE' and row.confirmed_at and not row.telegram_active
    cancelled=await client.post('/notifications/subscriptions/unsubscribe',json={'token':engine.manage_token(row)})
    assert cancelled.status_code==200 and row.status=='UNSUBSCRIBED'
    assert (await client.patch('/notifications/address-book/'+str(row.id),json={'active':True})).status_code==409


async def test_alert_code_and_preview_never_consume_a_trigger(context, monkeypatch):
    db, client, event, generated=await prepare(context,monkeypatch)
    assert event.code.startswith('ALT-') and event.station_id is not None
    assert event.compiled_rule['max_hours']==10
    assert event.compiled_rule['risk_description']=='enfermedad de roya'
    response=await client.post(f'/notifications/events/{event.id}/test')
    assert response.status_code==200 and response.json()['state']=='TRUE'
    assert event.trigger_number==0 and not event.station_states and event.last_checked_at is None
    code=event.code
    changed=await client.put(f'/notifications/events/{event.id}',json={'name':'Roya nueva','prompt':PROMPT,'active':False,'code':'FORGED','station_id':str(event.station_id)})
    assert changed.status_code==200 and changed.json()['code']==code and not event.active
    assert (await engine.evaluate_event(db,event,force=True))['status']=='SKIPPED'


async def test_one_episode_queues_only_confirmed_authorized_channels(context, monkeypatch):
    db, client, event, generated=await prepare(context,monkeypatch)
    await subscriber(db,email=True,telegram=True,linked=True)
    await subscriber(db,email=False,telegram=True,linked=False)
    await subscriber(db,email=False,telegram=True,linked=True)
    await subscriber(db,email=True,status='PENDING')
    await subscriber(db,email=True,status='UNSUBSCRIBED')
    await subscriber(db,email=True,telegram=False,linked=True)
    outcome=await engine.evaluate_event(db,event,force=True)
    assert outcome['queued']==4 and event.trigger_number==1 and len(generated)==1
    assert not (await engine.evaluate_event(db,event,force=True))['triggered']
    logs=list((await db.scalars(select(NotificationEventDelivery))).all())
    assert len(logs)==4 and sorted(r.channel for r in logs)==['EMAIL','EMAIL','TELEGRAM','TELEGRAM']
    assert all(r.payload['markdown'] for r in logs)
    # A real false reading rearms this station and a new match begins a new episode.
    latest=await db.scalar(select(WeatherReading).order_by(WeatherReading.sampled_at.desc()).limit(1))
    latest.humidity_pct=70;await db.commit()
    assert (await engine.evaluate_event(db,event,force=True))['status']=='FALSE'
    latest.humidity_pct=90;await db.commit()
    assert (await engine.evaluate_event(db,event,force=True))['queued']==4
    assert event.trigger_number==2


async def test_notifier_failure_does_not_consume_match_and_retries_later(context, monkeypatch):
    db, client, event, generated=await prepare(context,monkeypatch)
    await subscriber(db)
    async def failed(*args): raise ValueError('Proveedor no disponible')
    monkeypatch.setattr(engine,'notifier_content',failed)
    assert (await engine.evaluate_event(db,event,force=True))['status']=='CONTENT_ERROR'
    assert event.trigger_number==0 and not event.station_states
    async def restored(db,event,matched): return {'markdown':'Aviso con datos','subject':'Roya'}
    monkeypatch.setattr(engine,'notifier_content',restored)
    assert (await engine.evaluate_event(db,event,force=True))['queued']==1


async def test_outbox_retries_errors_and_checks_revoked_consent(context, monkeypatch):
    db, client, event, generated=await prepare(context,monkeypatch)
    recipient=await subscriber(db)
    await engine.evaluate_event(db,event,force=True)
    async def failed(*args): raise RuntimeError('SMTP temporalmente desconectado')
    monkeypatch.setattr(engine,'send_mail',failed)
    outcome=await engine.dispatch_event_deliveries(db)
    assert outcome['errors']==1
    log=await db.scalar(select(NotificationEventDelivery))
    assert log.status=='ERROR' and log.attempts==1 and log.next_attempt_at>datetime.now(UTC)
    assert (await engine.dispatch_event_deliveries(db))['errors']==0
    log.next_attempt_at=datetime.now(UTC)-timedelta(seconds=1);await db.commit()
    sent=[]
    async def capture(msg): sent.append(msg)
    monkeypatch.setattr(engine,'send_mail',capture)
    assert (await engine.dispatch_event_deliveries(db))['sent']==1
    assert log.status=='SENT' and log.attempts==2 and 'List-Unsubscribe' in sent[0]
    assert (await engine.dispatch_event_deliveries(db))['sent']==0
    # A queued delivery must be cancelled if its subscriber leaves before dispatch.
    db.add(NotificationEventDelivery(event_id=event.id,subscription_id=recipient.id,channel='EMAIL',destination_hint=recipient.email,trigger_number=2,status='QUEUED',payload={'markdown':'Aviso','subject':'Roya'}))
    recipient.status='UNSUBSCRIBED';await db.commit()
    assert (await engine.dispatch_event_deliveries(db))['sent']==0
    assert (await db.scalar(select(NotificationEventDelivery).where(NotificationEventDelivery.trigger_number==2))).status=='CANCELLED'


async def test_configuration_publishes_notifier_without_legacy_schedules(context,monkeypatch):
    db,user,station,client=context
    control=(await client.get('/notifications/control')).json()
    payload={k:control[k] for k in ['active','email_enabled','telegram_enabled','notifier_prompt']}
    payload.update(active=True,email_enabled=True)
    saved=await client.put('/notifications/control',json=payload)
    assert saved.status_code==200, saved.text
    assert saved.json()['notifier_agent_id']
    prompt_calls=[]
    async def ai(prompt, context, question, model, **kwargs):
        prompt_calls.append((prompt,context))
        return {'markdown':'## Aviso\nRevisar condiciones observadas.','fallback':False}
    monkeypatch.setattr('app.services.ai.generate_markdown_response',ai)
    event=NotificationEvent(name='Roya',code='ALT-TEST',prompt=PROMPT,compiled_rule=engine.compile_event_prompt(PROMPT))
    content=await engine.notifier_content(db,event,[{'station':'Estación central','detail':'Humedad >85% durante 10h'}])
    assert content['markdown'].startswith('## Aviso') and payload['notifier_prompt'] in prompt_calls[0][0]
    run=await db.get(AIAgentRun,UUID(content['agent_run_id']))
    assert run.status=='COMPLETED' and run.agent_version_id and run.input_payload['alert_code']=='ALT-TEST'
    assert (await client.post('/notifications/run-now')).status_code==410


async def test_telegram_link_requires_confirmed_consent_and_dispatch_uses_preferences(context,monkeypatch):
    from app.services.producer_notifications import process_telegram_update
    db,client,event,generated=await prepare(context,monkeypatch)
    row=await subscriber(db,email=False,telegram=True)
    config=await get_notification_integration(db)
    code='fc_'+row.id.hex+'_'+engine.digest(f'tg:{row.id}:{row.manage_secret}')[:24]
    calls=[]
    async def capture(token,chat,text): calls.append((chat,text))
    monkeypatch.setattr('app.services.producer_notifications._send_telegram',capture)
    monkeypatch.setattr('app.services.producer_notifications.telegram_bot_token',lambda _: 'fake-token')
    def start(argument):
        return {'update_id':1,'message':{'text':'/start '+argument,'chat':{'id':123,'type':'private','username':'verified_user'}}}
    await process_telegram_update(db,config,'fake-token',start('fc_invalid'))
    assert not row.telegram_active and 'inscríbase' in calls[-1][1]
    result=await process_telegram_update(db,config,'fake-token',start(code))
    assert result['action']=='SUBSCRIBED' and row.telegram_active and row.telegram_chat_id=='123'
    assert row.telegram_username=='verified_user'
    assert (await engine.evaluate_event(db,event,force=True))['queued']==1
    assert (await engine.dispatch_event_deliveries(db))['sent']==1
    assert 'Cancelar suscripción:' in calls[-1][1]
    log=await db.scalar(select(NotificationEventDelivery))
    assert log.channel=='TELEGRAM' and log.status=='SENT'
    await process_telegram_update(db,config,'fake-token',{'update_id':2,'message':{'text':'/stop','chat':{'id':123,'type':'private'}}})
    assert not row.telegram_active


async def test_flexible_prompt_api_compiles_once_and_preserves_rule_on_toggle(context,monkeypatch):
    import json
    from app.services.notification_rules import compile_notification_prompt
    db,user,station,client=context
    monkeypatch.setattr(subscriptions,'compile_notification_prompt',compile_notification_prompt)
    prompt='Cuando la temperatura sea mayor a 10 grados celcius por mas de una hora, avisame.'
    calls=[]
    async def ai(system,context,question,model,**kwargs):
        calls.append(context)
        assert context['prompt_to_interpret']==prompt
        assert kwargs['preserve_structured_output']
        return {'markdown':json.dumps({'supported':True,'expression':{'kind':'group','operator':'ALL','clauses':[{'kind':'condition','variable':'temperature_c','comparison':'gt','value':10,'aggregation':'continuous','hours':1,'duration_comparison':'more_than'}]}}),'model':'mock','usage':{}}
    monkeypatch.setattr('app.services.ai.generate_markdown_response',ai)
    parsed=await client.post('/notifications/events/parse',json={'name':'Temperatura sostenida','station_id':str(station.id),'prompt':prompt})
    assert parsed.status_code==200,parsed.text
    created=await client.post('/notifications/events',json={'name':'Temperatura sostenida','station_id':str(station.id),'prompt':prompt,'active':True})
    assert created.status_code==200,created.text
    saved=created.json()
    assert saved['compiled_rule']['version']==3 and 'más de 1 h' in saved['compiled_rule']['summary']
    tested=await client.post(f"/notifications/events/{saved['id']}/test")
    assert tested.status_code==200 and tested.json()['state']=='TRUE'
    toggled=await client.put(f"/notifications/events/{saved['id']}",json={'name':'Nombre editado','station_id':str(station.id),'prompt':prompt,'active':False})
    assert toggled.status_code==200 and toggled.json()['compiled_rule']==saved['compiled_rule']
    assert len(calls)==1


async def test_flexible_prompt_failure_never_activates_partial_rule(context,monkeypatch):
    from app.services.notification_rules import compile_notification_prompt
    db,user,station,client=context
    monkeypatch.setattr(subscriptions,'compile_notification_prompt',compile_notification_prompt)
    async def ai(*args,**kwargs):
        return {'markdown':'{"supported":false,"reason":"Indique el umbral de viento y la unidad.","expression":null}'}
    monkeypatch.setattr('app.services.ai.generate_markdown_response',ai)
    response=await client.post('/notifications/events',json={'name':'Viento','station_id':str(station.id),'prompt':'Cuando haya viento muy fuerte avísame','active':True})
    assert response.status_code==422 and 'umbral' in response.json()['detail']
    assert not (await db.scalars(select(NotificationEvent))).all()


async def test_delete_alert_removes_history_but_preserves_contacts_and_parcels(context, monkeypatch):
    from app.db.models import NotificationEventRun
    db, client, event, _ = await prepare(context, monkeypatch)
    contact = await subscriber(db)
    plot_ids = list((await db.scalars(select(SubscriptionPlot.plot_id).where(SubscriptionPlot.subscription_id==contact.id))).all())
    run = NotificationEventRun(event_id=event.id, mode='AUTOMATIC', status='QUEUED', result={})
    delivery = NotificationEventDelivery(event_id=event.id, subscription_id=contact.id, channel='EMAIL',
        destination_hint=contact.email, trigger_number=1, status='QUEUED', payload={})
    db.add_all([run, delivery]); await db.commit()
    event_id, run_id, delivery_id = event.id, run.id, delivery.id
    response = await client.delete(f'/notifications/events/{event_id}')
    assert response.status_code == 200, response.text
    assert await db.scalar(select(NotificationEvent.id).where(NotificationEvent.id==event_id)) is None
    assert await db.scalar(select(NotificationEventRun.id).where(NotificationEventRun.id==run_id)) is None
    assert await db.scalar(select(NotificationEventDelivery.id).where(NotificationEventDelivery.id==delivery_id)) is None
    assert await db.get(EmailSubscription, contact.id) is not None
    assert set((await db.scalars(select(Plot.id).where(Plot.id.in_(plot_ids)))).all()) == set(plot_ids)
    assert (await client.delete(f'/notifications/events/{event_id}')).status_code == 404


async def test_delete_alert_rejects_running_cycle(context, monkeypatch):
    from app.db.models import NotificationEventRun
    db, client, event, _ = await prepare(context, monkeypatch)
    db.add(NotificationEventRun(event_id=event.id, mode='AUTOMATIC', status='RUNNING', result={}))
    await db.commit()
    response = await client.delete(f'/notifications/events/{event.id}')
    assert response.status_code == 409
    assert await db.scalar(select(NotificationEvent.id).where(NotificationEvent.id==event.id)) == event.id
