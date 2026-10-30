"""Per-alert automatic 2h/72h cycles and simulations with no external sends."""
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.api.endpoints import subscriptions
from app.db.models import BackgroundJob, NotificationEvent, NotificationEventDelivery, NotificationEventRun, WeatherReading, Plot
from app.services import notification_cycles as cycles
from app.services import notification_events as engine
from app.services.jobs import handle_job
from app.services.producer_notifications import get_notification_integration
from test_unified_notifications import context, prepare, subscriber, PROMPT
from test_plot_integration import DATABASE, POLYGON, setup as integration_setup

pytestmark=pytest.mark.skipif(not DATABASE,reason='Requires disposable PostgreSQL')


async def test_alert_defaults_and_saved_config(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    assert event.every_minutes==120 and event.lookback_hours==72 and event.run_full_analysis
    changed=await client.put(f'/notifications/events/{event.id}',json={'name':event.name,'prompt':PROMPT,'active':True,
        'station_id':str(event.station_id),'every_minutes':360,'lookback_hours':96,'run_full_analysis':True})
    assert changed.status_code==200,changed.text
    assert changed.json()['every_minutes']==360 and changed.json()['lookback_hours']==96
    too_short=await client.put(f'/notifications/events/{event.id}',json={'name':event.name,'prompt':PROMPT,'station_id':str(event.station_id),'lookback_hours':1,'run_full_analysis':False})
    assert too_short.status_code==422
    assert engine.is_due(event,datetime.now(UTC))
    event.last_checked_at=datetime.now(UTC)
    assert not engine.is_due(event,datetime.now(UTC)+timedelta(hours=5))
    assert engine.is_due(event,datetime.now(UTC)+timedelta(hours=6,seconds=1))


async def test_scheduler_queues_only_due_alert_and_prevents_overlap(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    outcome=await engine.process_scheduled_events(db)
    assert outcome['cycles_queued']==1
    run=await db.scalar(select(NotificationEventRun))
    assert run.status=='QUEUED' and run.mode=='AUTOMATIC'
    assert (await engine.process_scheduled_events(db))['cycles_queued']==0
    busy=await client.post(f'/notifications/events/{event.id}/simulate')
    assert busy.status_code==409 # Never return an actual sending run as a simulation.
    run.status='COMPLETED';event.last_checked_at=datetime.now(UTC);await db.commit()
    assert (await engine.process_scheduled_events(db))['cycles_queued']==0


async def test_historical_episode_with_current_false_is_found_once(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    now=datetime.now(UTC)
    # A completed qualifying episode exists earlier in the 72h window, while
    # the most recent three hours no longer satisfy humidity.
    await db.execute(update(WeatherReading).where(WeatherReading.sampled_at>=now-timedelta(hours=3)).values(humidity_pct=50))
    await db.commit()
    result=await cycles.review_event_window(db,event,now)
    assert result['period']['hours']==72 and result['stations'][0]['matched']
    assert result['stations'][0]['current_state']=='FALSE'
    assert len(result['stations'][0]['episodes'])==1
    event.station_cursors=result['cursors'];await db.commit()
    repeated=await cycles.review_event_window(db,event,now+timedelta(minutes=1))
    assert not repeated['stations'][0]['matched'] and not repeated['stations'][0]['episodes']
    simulated=await cycles.review_event_window(db,event,now,simulation=True)
    assert simulated['stations'][0]['matched']


async def test_simulation_runs_full_chain_but_never_consumes_or_delivers(context,monkeypatch):
    db,client,event,generated=await prepare(context,monkeypatch)
    await subscriber(db)
    performed=[]
    async def weather(db,stations,start,end):
        performed.append(('weather',start,end));return [{'status':'COMPLETED'}]
    async def full(db,event,now):
        performed.append(('analysis',event.lookback_hours));return [{'plot_id':'plot','plot':'Prueba','analysis_id':'analysis','status':'COMPLETED','agents':{}}]
    monkeypatch.setattr(cycles,'refresh_cycle_weather',weather)
    monkeypatch.setattr(cycles,'analyze_cycle_parcels',full)
    response=await client.post(f'/notifications/events/{event.id}/simulate')
    assert response.status_code==200,response.text
    run_id=UUID(response.json()['run_id'])
    job=await db.scalar(select(BackgroundJob).where(BackgroundJob.job_type=='NOTIFICATION_EVENT_CYCLE'))
    result=await handle_job(db,job)
    assert result['simulation'] and result['queued']==0 and result['parcel_analyses']
    assert result['preview_content']['markdown'] and result['period']['hours']==72
    assert performed[0][0]=='weather' and performed[1]==('analysis',72)
    assert event.last_checked_at is None and event.trigger_number==0 and not event.station_cursors
    assert not (await db.scalars(select(NotificationEventDelivery))).all()
    run=(await client.get(f'/notifications/events/{event.id}/runs/{run_id}')).json()
    assert run['status']=='COMPLETED' and run['mode']=='SIMULATION'
    assert len((await client.get(f'/notifications/events/{event.id}/runs')).json()['items'])==1


async def test_automatic_cycle_analyzes_and_queues_only_new_episodes(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    await subscriber(db)
    async def weather(*args): return []
    async def full(*args): return [{'analysis_id':'saved-analysis','plot':'Parcela','status':'COMPLETED'}]
    monkeypatch.setattr(cycles,'refresh_cycle_weather',weather)
    monkeypatch.setattr(cycles,'analyze_cycle_parcels',full)
    run,_=await cycles.queue_event_cycle(db,event.id)
    first=await cycles.execute_event_cycle(db,run.id)
    assert first['queued']==1 and not first['simulation'] and event.last_checked_at
    logs=list((await db.scalars(select(NotificationEventDelivery))).all())
    assert len(logs)==1 and event.station_cursors
    run,_=await cycles.queue_event_cycle(db,event.id,mode='MANUAL',force=True)
    second=await cycles.execute_event_cycle(db,run.id)
    assert second['queued']==0 and len((await db.scalars(select(NotificationEventDelivery))).all())==1


async def test_full_pipeline_uses_same_four_agents_and_window(context,monkeypatch):
    db,user,station,client=context
    from app.db.models import Crop, PlotAnalysis
    crop=await db.scalar(select(Crop))
    plot=Plot(name='Parcela de ciclo',geojson=POLYGON,crop_id=crop.id,station_id=station.id,
              planting_date=datetime.now(UTC).date()-timedelta(days=60),active=True)
    db.add(plot);await db.commit()
    event=NotificationEvent(name='Ciclo',prompt=PROMPT,compiled_rule=engine.compile_event_prompt(PROMPT),lookback_hours=72,station_id=station.id)
    calls=[]
    async def build(db,plot,**kwargs):
        assert kwargs['window_hours']==72 and kwargs['refresh_forecast']
        analysis=PlotAnalysis(plot_id=plot.id,reference_at=kwargs['reference'],context={})
        db.add(analysis);await db.flush();return analysis
    async def interpret(db,analysis,purpose,**kwargs):
        assert kwargs['window_hours']==72
        calls.append(purpose);return {'status':'completed','markdown':'Informe'}
    monkeypatch.setattr('app.services.plot_pipeline.build_plot_analysis',build)
    monkeypatch.setattr('app.services.plot_agents.interpret_plot',interpret)
    result=await cycles.analyze_cycle_parcels(db,event,datetime.now(UTC))
    assert calls==['RAG_CROP','WEATHER_ANALYSIS','SATELLITE_ANALYSIS','ORCHESTRATOR']
    assert result[0]['analysis_id'] and result[0]['status']=='COMPLETED'


async def test_service_disabled_and_alert_paused_never_create_sending_cycles(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    config=await get_notification_integration(db)
    config.active=False;await db.commit()
    assert (await engine.process_scheduled_events(db))['cycles_queued']==0
    simulation,_=await cycles.queue_event_cycle(db,event.id,mode='SIMULATION',force=True)
    assert simulation and simulation.mode=='SIMULATION'
    simulation.status='COMPLETED';event.active=False;await db.commit()
    sending,queued=await cycles.queue_event_cycle(db,event.id,mode='MANUAL',force=True)
    assert sending is None and not queued


async def test_failed_cycle_records_error_and_respects_next_cadence(context,monkeypatch):
    db,client,event,_=await prepare(context,monkeypatch)
    async def failure(*args): raise RuntimeError('No se pudo preparar el clima')
    monkeypatch.setattr(cycles,'refresh_cycle_weather',failure)
    run,_=await cycles.queue_event_cycle(db,event.id)
    result=await cycles.execute_event_cycle(db,run.id)
    assert result['status']=='ERROR'
    assert event.last_checked_at and event.last_status=='CYCLE_ERROR'
    assert run.status=='ERROR' and 'clima' in run.error
    assert not (await db.scalars(select(NotificationEventDelivery))).all()
