"""Geographic routing, personal message isolation and verified parcel enrollment."""
from datetime import UTC,datetime,timedelta
from uuid import UUID,uuid4
import json,re

import pytest
from sqlalchemy import select

from app.api.endpoints import subscriptions
from app.core.security import create_token
from app.db.models import Crop,Plot,SubscriptionPlot,EmailSubscription,NotificationEventDelivery,Station
from app.services import notification_events as engine,notification_cycles as cycles
from app.services.notification_audience import station_parcels,event_audience
from app.services.parcel_browser import TOKEN_TYPE
from test_unified_notifications import context,prepare,subscriber,PROMPT
from test_plot_integration import DATABASE,POLYGON,setup as integration_setup

pytestmark=pytest.mark.skipif(not DATABASE,reason='Requires disposable PostgreSQL')


async def plot(db,name,station,*,outside=False,browser=None):
    crop=await db.scalar(select(Crop))
    geometry=json.loads(json.dumps(POLYGON))
    if outside:
        for pair in geometry['coordinates'][0]:pair[0]+=1
    row=Plot(name=name,geojson=geometry,crop_id=crop.id,station_id=station.id,active=True,
             browser_session_id=browser,planting_date=datetime.now(UTC).date()-timedelta(days=60))
    db.add(row);await db.commit();return row


async def link(db,row,*plots):
    for p in plots:db.add(SubscriptionPlot(subscription_id=row.id,plot_id=p.id))
    await db.commit()


def browser_headers(identifier):
    token=create_token(str(identifier),TOKEN_TYPE,timedelta(hours=1))
    return {'X-Parcel-Session':str(identifier),'X-Parcel-Access':token}


async def test_station_required_for_new_alert_and_no_contact_broadcast(context):
    db,user,station,client=context
    response=await client.post('/notifications/events',json={'name':'Sin estación','prompt':PROMPT,'active':True})
    assert response.status_code==422


async def test_only_linked_covered_parcels_receive_even_if_primary_station_is_stale(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    station=await db.get(Station,event.station_id)
    inside=await plot(db,'Dentro',station)
    outside=await plot(db,'Fuera pero con station_id antiguo',station,outside=True)
    covered=await subscriber(db,link_plot=False)
    unrelated=await subscriber(db,link_plot=False)
    unlinked=await subscriber(db,link_plot=False)
    await link(db,covered,inside);await link(db,unrelated,outside)
    targets=await event_audience(db,event)
    assert [a['subscription'].id for a in targets]==[covered.id]
    result=await engine.evaluate_event(db,event,force=True)
    assert result['queued']==1
    log=await db.scalar(select(NotificationEventDelivery))
    assert log.subscription_id==covered.id and log.payload['plot_ids']==[str(inside.id)]
    assert unrelated.id!=log.subscription_id and unlinked.id!=log.subscription_id


async def test_each_contact_sees_only_own_parcels_and_analysis(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    from app.db.models import Station
    station=await db.get(Station,event.station_id)
    a=await plot(db,'Parcela privada A',station);b=await plot(db,'Parcela privada B',station)
    first=await subscriber(db,link_plot=False);second=await subscriber(db,link_plot=False)
    await link(db,first,a);await link(db,second,b)
    event._cycle_analyses=[{'plot_id':str(a.id),'markdown':'Información A'},{'plot_id':str(b.id),'markdown':'Información B'}]
    seen=[]
    async def personal(db,event,matched):
        names=[p['plot'] for p in event._cycle_plots]
        analyses=list(event._cycle_analyses)
        seen.append((names,analyses))
        return {'markdown':' / '.join(names),'subject':'Aviso personal'}
    monkeypatch.setattr(engine,'notifier_content',personal)
    assert (await engine.evaluate_event(db,event,force=True))['queued']==2
    logs=list((await db.scalars(select(NotificationEventDelivery))).all())
    assert next(l for l in logs if l.subscription_id==first.id).payload['markdown']=='Parcela privada A'
    assert next(l for l in logs if l.subscription_id==second.id).payload['markdown']=='Parcela privada B'
    assert len(seen)==2 and all(len(analyses)==1 for _,analyses in seen)


async def test_coverage_and_link_revocation_are_checked_again_before_sending(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    row=await subscriber(db)
    assert (await engine.evaluate_event(db,event,force=True))['queued']==1
    parcel=await db.scalar(select(Plot))
    parcel.deleted_at=datetime.now(UTC);await db.commit()
    async def forbidden(*args):raise AssertionError('Must not send for deleted parcels')
    monkeypatch.setattr(engine,'send_mail',forbidden)
    assert (await engine.dispatch_event_deliveries(db))['sent']==0
    assert (await db.scalar(select(NotificationEventDelivery))).status=='CANCELLED'


async def test_new_contact_gets_ongoing_episode_without_repeating_existing_contacts(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    first=await subscriber(db)
    result=await cycles.review_event_window(db,event,datetime.now(UTC))
    assert (await engine.evaluate_event(db,event,force=True,prepared_result=result))['queued']==1
    second=await subscriber(db)
    repeated=await cycles.review_event_window(db,event,datetime.now(UTC))
    assert (await engine.evaluate_event(db,event,force=True,prepared_result=repeated))['queued']==1
    logs=list((await db.scalars(select(NotificationEventDelivery))).all())
    assert [l.subscription_id for l in logs].count(first.id)==1
    assert [l.subscription_id for l in logs].count(second.id)==1
    again=await cycles.review_event_window(db,event,datetime.now(UTC))
    assert (await engine.evaluate_event(db,event,force=True,prepared_result=again))['queued']==0


async def test_admin_assigns_only_confirmed_contacts_and_can_remove_links(context,monkeypatch):
    db,user,station,client=context
    parcel=await plot(db,'Asignación',station)
    pending=await subscriber(db,status='PENDING',link_plot=False)
    assert (await client.put(f'/notifications/address-book/{pending.id}/parcels',json={'plot_ids':[str(parcel.id)]})).status_code==409
    row=await subscriber(db,link_plot=False)
    response=await client.put(f'/notifications/address-book/{row.id}/parcels',json={'plot_ids':[str(parcel.id)]})
    assert response.status_code==200,response.text
    book=(await client.get('/notifications/address-book')).json()['items']
    linked=next(r for r in book if r['id']==str(row.id))['plots']
    assert linked[0]['id']==str(parcel.id) and linked[0]['stations'][0]['id']==str(station.id)
    assert (await client.put(f'/notifications/address-book/{row.id}/parcels',json={'plot_ids':[]})).status_code==200
    assert not (await db.scalars(select(SubscriptionPlot).where(SubscriptionPlot.subscription_id==row.id))).all()


async def test_public_enrollment_requires_browser_ownership_and_email_confirmation(context,monkeypatch):
    db,user,station,client=context
    identifier=uuid4();other=uuid4()
    mine=await plot(db,'Mía',station,browser=identifier)
    foreign=await plot(db,'Ajena',station,browser=other)
    sent=[]
    async def capture(msg):sent.append(msg)
    monkeypatch.setattr(subscriptions,'send_mail',capture)
    options=await client.get('/notifications/subscriptions/parcel-options',headers=browser_headers(identifier))
    assert [p['id'] for p in options.json()['items']]==[str(mine.id)]
    payload={'email':'vinculo@example.org','consent':True,'plot_ids':[str(foreign.id)]}
    assert (await client.post('/notifications/subscriptions/request',json=payload,headers=browser_headers(identifier))).status_code==404
    payload['plot_ids']=[str(mine.id)]
    assert (await client.post('/notifications/subscriptions/request',json=payload)).status_code==401
    response=await client.post('/notifications/subscriptions/request',json=payload,headers=browser_headers(identifier))
    assert response.status_code==202,response.text
    row=await db.scalar(select(EmailSubscription).where(EmailSubscription.email==payload['email']))
    assert not (await db.scalars(select(SubscriptionPlot).where(SubscriptionPlot.subscription_id==row.id))).all()
    token=re.search(r'confirmar=([A-Za-z0-9_-]+)',sent[-1].get_body(preferencelist=('plain',)).get_content()).group(1)
    assert (await client.post('/notifications/subscriptions/confirm',json={'token':token})).status_code==200
    assert (await db.scalar(select(SubscriptionPlot.plot_id).where(SubscriptionPlot.subscription_id==row.id)))==mine.id


async def test_existing_contact_changes_are_pending_until_new_confirmation(context,monkeypatch):
    db,user,station,client=context
    identifier=uuid4();parcel=await plot(db,'Nueva parcela',station,browser=identifier)
    row=await subscriber(db,link_plot=False)
    sent=[]
    async def capture(msg):sent.append(msg)
    monkeypatch.setattr(subscriptions,'send_mail',capture)
    response=await client.post('/notifications/subscriptions/request',json={'email':row.email,'consent':True,
        'plot_ids':[str(parcel.id)],'email_enabled':False,'telegram_enabled':True},headers=browser_headers(identifier))
    assert response.status_code==202 and row.status=='ACTIVE' and row.email_enabled
    assert not (await db.scalars(select(SubscriptionPlot).where(SubscriptionPlot.subscription_id==row.id))).all()
    token=re.search(r'confirmar=([A-Za-z0-9_-]+)',sent[-1].get_body(preferencelist=('plain',)).get_content()).group(1)
    assert (await client.post('/notifications/subscriptions/confirm',json={'token':token})).status_code==200
    assert not row.email_enabled and row.telegram_enabled
    assert (await db.scalar(select(SubscriptionPlot.plot_id).where(SubscriptionPlot.subscription_id==row.id)))==parcel.id


async def test_moving_station_coverage_cancels_pending_parcel_message(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    await subscriber(db)
    assert (await engine.evaluate_event(db,event,force=True))['queued']==1
    station=await db.get(Station,event.station_id)
    station.longitude+=1;await db.commit()
    assert (await engine.dispatch_event_deliveries(db))['sent']==0
    assert (await db.scalar(select(NotificationEventDelivery))).status=='CANCELLED'


async def test_later_telegram_link_receives_ongoing_episode_once_per_channel(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    row=await subscriber(db,email=True,telegram=True,linked=False)
    first=await cycles.review_event_window(db,event,datetime.now(UTC))
    assert (await engine.evaluate_event(db,event,force=True,prepared_result=first))['queued']==1
    row.telegram_active=True;row.telegram_chat_id='123';await db.commit()
    next_cycle=await cycles.review_event_window(db,event,datetime.now(UTC))
    assert (await engine.evaluate_event(db,event,force=True,prepared_result=next_cycle))['queued']==1
    logs=list((await db.scalars(select(NotificationEventDelivery))).all())
    assert sorted(l.channel for l in logs)==['EMAIL','TELEGRAM']


async def test_parcel_owner_changed_before_confirmation_is_not_linked(context,monkeypatch):
    db,user,station,client=context
    identifier=uuid4();parcel=await plot(db,'Cambia navegador',station,browser=identifier)
    sent=[]
    async def capture(msg):sent.append(msg)
    monkeypatch.setattr(subscriptions,'send_mail',capture)
    response=await client.post('/notifications/subscriptions/request',json={'email':'cambio@example.org','consent':True,
        'plot_ids':[str(parcel.id)]},headers=browser_headers(identifier))
    assert response.status_code==202
    parcel.browser_session_id=uuid4();await db.commit()
    token=re.search(r'confirmar=([A-Za-z0-9_-]+)',sent[-1].get_body(preferencelist=('plain',)).get_content()).group(1)
    assert (await client.post('/notifications/subscriptions/confirm',json={'token':token})).status_code==200
    assert not (await db.scalars(select(SubscriptionPlot))).all()


async def test_partial_station_overlap_is_visible_and_eligible(context):
    db,user,station,client=context
    parcel=await plot(db,'Cobertura parcial',station)
    geometry=json.loads(json.dumps(parcel.geojson))
    for coordinate in geometry['coordinates'][0]:coordinate[0]+=0.02
    parcel.geojson=geometry;await db.commit()
    covered=await station_parcels(db,station.id)
    match=next(p for p in covered if p['plot_id']==str(parcel.id))
    assert 0<match['coverage_pct']<100
