"""Verified subscriber ↔ parcel links and live station coverage for delivery routing."""
from __future__ import annotations
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import EmailSubscription,Plot,Station,SubscriptionPlot
from app.services.geospatial import calculate_influences


async def station_parcels(db:AsyncSession,station_id:UUID|None,*,crop_id:UUID|None=None)->list[dict]:
    if not station_id:return []
    station=await db.get(Station,station_id,populate_existing=True)
    if not station or not station.active or station.deleted_at:return []
    query=select(Plot).where(Plot.active.is_(True),Plot.deleted_at.is_(None),Plot.geojson.is_not(None),Plot.crop_id.is_not(None))
    if crop_id:query=query.where(Plot.crop_id==crop_id)
    result=[]
    for plot in (await db.scalars(query)).all():
        try:coverage=calculate_influences(plot.geojson,[station])
        except ValueError:continue
        if coverage:
            result.append({'plot_id':str(plot.id),'plot':plot.name,'coverage_pct':coverage[0]['coverage_pct']})
    return result


async def event_audience(db:AsyncSession,event)->list[dict]:
    covered=await station_parcels(db,event.station_id,crop_id=event.crop_id)
    plots={UUID(p['plot_id']):p for p in covered}
    if not plots:return []
    rows=(await db.execute(select(EmailSubscription,SubscriptionPlot.plot_id).join(
        SubscriptionPlot,SubscriptionPlot.subscription_id==EmailSubscription.id).where(
        SubscriptionPlot.plot_id.in_(list(plots)),EmailSubscription.status=='ACTIVE',EmailSubscription.confirmed_at.is_not(None)
    ).execution_options(populate_existing=True))).all()
    audience={}
    for subscriber,plot_id in rows:
        item=audience.setdefault(subscriber.id,{'subscription':subscriber,'plots':[]})
        item['plots'].append(plots[plot_id])
    return list(audience.values())


async def subscription_parcels(db:AsyncSession,subscription_ids:list[UUID])->dict:
    if not subscription_ids:return {}
    rows=(await db.execute(select(SubscriptionPlot.subscription_id,Plot).join(Plot,Plot.id==SubscriptionPlot.plot_id)
        .where(SubscriptionPlot.subscription_id.in_(subscription_ids),Plot.active.is_(True),Plot.deleted_at.is_(None)))).all()
    stations=list((await db.scalars(select(Station).where(Station.active.is_(True),Station.deleted_at.is_(None)))).all())
    cache={}
    result={}
    for subscription_id,plot in rows:
        if plot.id not in cache:
            try:coverage=calculate_influences(plot.geojson,stations) if plot.geojson else []
            except ValueError:coverage=[]
            cache[plot.id]=[{'id':c['station_id'],'name':c['name'],'coverage_pct':c['coverage_pct']} for c in coverage]
        result.setdefault(subscription_id,[]).append({'id':str(plot.id),'name':plot.name,'stations':cache[plot.id]})
    return result
