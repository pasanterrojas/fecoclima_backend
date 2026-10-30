"""Durable automatic analysis cycles, independent of browser sessions."""
from __future__ import annotations

from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (BackgroundJob, NotificationEvent, NotificationEventRun,
                           Plot, Station, WeatherReading)
from app.services.notification_events import evaluate_readings, is_due


async def queue_event_cycle(db: AsyncSession, event_id: UUID, *, mode='AUTOMATIC', force=False) -> tuple[NotificationEventRun | None, bool]:
    from app.services.jobs import enqueue_job
    event = await db.scalar(select(NotificationEvent).where(NotificationEvent.id == event_id)
                            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if not event or not event.station_id or (mode != 'SIMULATION' and not event.active):
        return None, False
    existing = await db.scalar(select(NotificationEventRun).where(
        NotificationEventRun.event_id == event_id, NotificationEventRun.status.in_(['QUEUED','RUNNING'])).limit(1))
    if existing:
        job=await db.scalar(select(BackgroundJob).where(BackgroundJob.job_type=='NOTIFICATION_EVENT_CYCLE',
            BackgroundJob.payload=={'run_id':str(existing.id)},BackgroundJob.status.in_(['PENDING','RUNNING'])).limit(1))
        if job:
            await db.commit()
            return (None,False) if mode in ('SIMULATION','MANUAL') and existing.mode!=mode else (existing,False)
        existing.status='ERROR';existing.finished_at=datetime.now(UTC)
        existing.error='La ejecución perdió su trabajo de fondo. Se permite iniciar un nuevo ciclo.'
        await db.flush()
    if mode == 'AUTOMATIC' and not force and not is_due(event, datetime.now(UTC)):
        await db.commit()
        return None, False
    if mode != 'SIMULATION':
        from app.services.producer_notifications import get_notification_integration
        integration = await get_notification_integration(db)
        if not integration or not integration.active or not (integration.non_sensitive_config or {}).get('active'):
            await db.commit()
            return None, False
    run = NotificationEventRun(event_id=event_id, mode=mode, status='QUEUED', result={})
    db.add(run); await db.flush()
    # The run and its job are committed together; a worker crash retries this ID.
    await enqueue_job(db, 'NOTIFICATION_EVENT_CYCLE', {'run_id':str(run.id)}, priority=44, max_attempts=3)
    return run, True


async def active_stations(db: AsyncSession, event: NotificationEvent) -> list[Station]:
    if not event.station_id:return []
    query = select(Station).where(Station.active.is_(True),Station.deleted_at.is_(None))
    if event.station_id:
        query = query.where(Station.id == event.station_id)
    return list((await db.scalars(query.order_by(Station.name))).all())


async def refresh_cycle_weather(db: AsyncSession, stations: list[Station], start: datetime, end: datetime) -> list[dict]:
    from app.services.weather import sync_fecoclima_station, aggregate_range
    results=[]
    station_ids=[station.id for station in stations]
    for station_id in station_ids:
        station=await db.get(Station,station_id,populate_existing=True)
        try:
            zone=ZoneInfo(station.timezone or 'UTC')
            first,last=start.astimezone(zone).date(),end.astimezone(zone).date()
            sync=await sync_fecoclima_station(db,station.id,first,last) if station.source_type=='FECOCLIMA_API' else {'source':'stored_readings'}
            days=await aggregate_range(db,station.id,first,last)
            results.append({'station_id':str(station.id),'status':'COMPLETED','aggregated_days':days,'sync':sync})
        except Exception as exc:
            await db.rollback()
            results.append({'station_id':str(station_id),'status':'ERROR','error':str(exc)[:500]})
    return results


async def analyze_cycle_parcels(db: AsyncSession, event: NotificationEvent, now: datetime) -> list[dict]:
    from app.services.plot_pipeline import build_plot_analysis
    from app.services.plot_agents import interpret_plot
    from app.services.notification_audience import station_parcels
    covered=await station_parcels(db,event.station_id,crop_id=event.crop_id)
    ids=[UUID(p['plot_id']) for p in covered]
    plots=[(p.id,p.name) for p in (await db.scalars(select(Plot).where(Plot.id.in_(ids),Plot.planting_date.is_not(None)))).all()] if ids else []
    lookback_hours=event.lookback_hours
    results=[]
    for plot_id,plot_name in plots:
        plot=await db.get(Plot,plot_id,populate_existing=True)
        try:
            analysis=await build_plot_analysis(db,plot,reference=now,window_hours=lookback_hours,refresh_forecast=True)
            analysis_id=analysis.id
            await db.commit()
            agents={}
            # Same analysis chain used by the producer's Analyze IA button.
            for purpose in ('RAG_CROP','WEATHER_ANALYSIS','SATELLITE_ANALYSIS','ORCHESTRATOR'):
                try:
                    response=await interpret_plot(db,analysis,purpose,window_hours=lookback_hours)
                    agents[purpose]={'status':response.get('status'),'markdown':response.get('markdown','')}
                    await db.commit()
                except Exception as exc:
                    await db.rollback()
                    agents[purpose]={'status':'error','error':str(exc)[:500]}
            failed=any(a.get('status') in ('error','fallback') for a in agents.values())
            results.append({'plot_id':str(plot_id),'plot':plot_name,'analysis_id':str(analysis_id),
                            'status':'PARTIAL' if failed else 'COMPLETED','agents':agents})
        except Exception as exc:
            await db.rollback()
            results.append({'plot_id':str(plot_id),'plot':plot_name,'status':'ERROR','error':str(exc)[:500]})
    return results


async def review_event_window(db: AsyncSession, event: NotificationEvent, now: datetime, *, simulation=False) -> dict:
    """Find new episodes throughout the lookback, preserving per-station cursors.

    A historical episode that remains in overlapping 72h windows is sent once.
    Missing readings never reset a known episode or create a positive result.
    """
    window_start=now-timedelta(hours=event.lookback_hours)
    duration=max(0,float(event.compiled_rule.get('max_hours',0)))
    since=window_start-timedelta(hours=duration+3)
    cursors={} if simulation else dict(event.station_cursors or {})
    results=[]
    for station in await active_stations(db,event):
        records=list((await db.scalars(select(WeatherReading).where(
            WeatherReading.station_id==station.id,WeatherReading.sampled_at>=since,WeatherReading.sampled_at<=now
        ).order_by(WeatherReading.sampled_at))).all())
        key=str(station.id)
        previous=dict(cursors.get(key) or {})
        last=previous.get('last_sampled_at')
        last_time=datetime.fromisoformat(last) if last else None
        state=previous.get('state','FALSE')
        times=[r.sampled_at for r in records]
        episodes=[]
        latest=evaluate_readings(event.compiled_rule,records,now,station.expected_interval_minutes)
        tolerance=min(180,max(40,int(station.expected_interval_minutes or 10)*3))
        fresh=bool(records and (now-records[-1].sampled_at).total_seconds()<=tolerance*60)
        if fresh:
            for index,record in enumerate(records):
                if record.sampled_at<window_start or (last_time and record.sampled_at<=last_time):
                    continue
                first=bisect_left(times,record.sampled_at-timedelta(hours=duration+3))
                result=evaluate_readings(event.compiled_rule,records[first:index+1],record.sampled_at,station.expected_interval_minutes)
                current=result['state']
                if current=='TRUE' and state!='TRUE':
                    episode={'observed_at':record.sampled_at.isoformat(),'detail':result['detail']}
                    episodes.append(episode);previous['current_episode']=episode
                if current=='TRUE' and not previous.get('current_episode'):
                    previous['current_episode']={'observed_at':record.sampled_at.isoformat(),'detail':result['detail']}
                if current in ('TRUE','FALSE'):
                    state=current
                    if current=='FALSE':previous.pop('current_episode',None)
                previous['last_sampled_at']=record.sampled_at.isoformat()
            previous['state']=state
            cursors[key]=previous
            ongoing=previous.get('current_episode')
            if state=='TRUE' and ongoing and all(e['observed_at']!=ongoing['observed_at'] for e in episodes):
                episodes.append(ongoing)
        results.append({'station_id':key,'station':station.name,'state':latest['state'],
                        'detail':(' · '.join(e['observed_at']+': '+e['detail'] for e in episodes) if episodes else latest['detail']),
                        'current_state':latest['state'],'current_detail':latest['detail'],
                        'episodes':episodes,'matched':bool(episodes)})
    matched=[r for r in results if r['matched']]
    return {'state':'TRUE' if matched or any(r['state']=='TRUE' for r in results) else 'UNKNOWN' if not results or any(r['state']=='UNKNOWN' for r in results) else 'FALSE',
            'detail':f'{len(matched)} estaciones con episodios nuevos en las últimas {event.lookback_hours} horas.',
            'stations':results,'cursors':cursors,'historical':True,
            'period':{'from':window_start.isoformat(),'to':now.isoformat(),'hours':event.lookback_hours}}


async def execute_event_cycle(db: AsyncSession, run_id: UUID) -> dict:
    from app.services.notification_events import evaluate_event
    run=await db.get(NotificationEventRun,run_id)
    if not run:
        return {'status':'NOT_FOUND'}
    if run.status not in ('QUEUED','RUNNING'):
        return run.result or {'status':run.status}
    event=await db.scalar(select(NotificationEvent).where(NotificationEvent.id==run.event_id)
                          .with_for_update().execution_options(populate_existing=True))
    if not event:
        await db.rollback()
        return {'status':'NOT_FOUND'}
    event_id=run.event_id
    simulation=run.mode=='SIMULATION'
    if not event or (not simulation and not event.active):
        run.status='CANCELLED';run.finished_at=datetime.now(UTC);await db.commit()
        return {'status':'CANCELLED'}
    now=datetime.now(UTC)
    run.status='RUNNING';run.started_at=now;await db.commit()
    try:
        start=now-timedelta(hours=event.lookback_hours+float(event.compiled_rule.get('max_hours',0))+3)
        weather=await refresh_cycle_weather(db,await active_stations(db,event),start,now)
        event=await db.get(NotificationEvent,event_id,populate_existing=True)
        analyses=await analyze_cycle_parcels(db,event,now) if event.run_full_analysis else []
        event=await db.get(NotificationEvent,event_id,populate_existing=True)
        reviewed=await review_event_window(db,event,now,simulation=simulation)
        event._cycle_analyses=analyses
        if simulation:
            from app.services.notification_events import plan_event_deliveries
            matching=[s for s in reviewed['stations'] if s['matched']]
            plans=[];content_error=None
            if matching:
                try:plans=await plan_event_deliveries(db,event,matching,simulation=True)
                except Exception as exc:content_error=str(exc)[:1500]
            outcome={'status':reviewed['state'],'triggered':bool(matching),'queued':0,
                'preview_content':plans[0]['content'] if plans else None,'content_error':content_error,
                'audience':[{'email':p['subscription'].email,'channel':p['channel'],'plots':p['plots']} for p in plans]}
        else:
            outcome=await evaluate_event(db,event,force=True,prepared_result=reviewed)
            if outcome.get('status') in ('TRUE','FALSE','UNKNOWN','CONTENT_ERROR'):
                event.last_checked_at=now
        run=await db.get(NotificationEventRun,run_id,populate_existing=True)
        run.result={**outcome,'period':reviewed['period'],'stations':reviewed['stations'],
                    'weather_sync':weather,'parcel_analyses':analyses,'simulation':simulation,
                    'full_analysis':event.run_full_analysis}
        run.status='ERROR' if outcome.get('status')=='CONTENT_ERROR' else 'COMPLETED'
        run.finished_at=datetime.now(UTC)
        await db.commit()
        return run.result
    except Exception as exc:
        await db.rollback()
        run=await db.get(NotificationEventRun,run_id,populate_existing=True)
        run.status='ERROR';run.error=str(exc)[:1500];run.finished_at=datetime.now(UTC)
        event=await db.get(NotificationEvent,run.event_id)
        if not simulation and event:
            event.last_checked_at=now;event.last_status='CYCLE_ERROR';event.last_detail=run.error
        await db.commit()
        return {'status':'ERROR','detail':run.error}
