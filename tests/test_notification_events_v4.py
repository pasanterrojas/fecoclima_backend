"""Pruebas de condiciones y de separación entre datos faltantes y alertas reales."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.services.notification_events import compile_event_prompt, evaluate_readings, is_due, manage_token, check_manage_token

PROMPT = 'Cuando se cumpla humedad mayor a 85 % por más de 10 horas y una temperatura entre 15 y 25 grados, entonces notifica a la libreta de direcciones.'


def test_natural_language_compiles_every_clause():
    rule=compile_event_prompt(PROMPT)
    assert rule['operator']=='ALL'
    assert rule['clauses']==[{'type':'humidity_above','threshold':85.0,'hours':10.0},{'type':'temperature_between','min':15.0,'max':25.0,'hours':10.0}]


@pytest.mark.parametrize('prompt',[
    'Cuando el satélite detecte hongos y la planta esté enferma, avisar',
    'Humedad mayor a 85% por 10 horas y activar un webhook secreto',
    'Humedad relativa mayor a 108% por 10 horas',
])
def test_unsupported_or_unsafe_rules_are_rejected(prompt):
    with pytest.raises(ValueError): compile_event_prompt(prompt)


def series(now, hours=11, humidity=90, temp=19, gap_at=None):
    output=[]
    for i in range(hours*6+1):
        if i==gap_at: continue
        output.append(SimpleNamespace(sampled_at=now-timedelta(minutes=10*i),humidity_pct=humidity,temperature_c=temp,rain_mm=1))
    return output


def test_ten_hour_sustained_humidity_requires_actual_history():
    now=datetime(2026,10,19,22,tzinfo=UTC)
    rule=compile_event_prompt(PROMPT)
    assert evaluate_readings(rule,series(now),now,10)['state']=='TRUE'
    assert evaluate_readings(rule,series(now,8),now,10)['state']=='UNKNOWN'
    assert evaluate_readings(rule,series(now,11,humidity=70),now,10)['state']=='FALSE'
    assert evaluate_readings(rule,series(now,11,temp=29),now,10)['state']=='FALSE'
    stale=series(now-timedelta(hours=2))
    assert evaluate_readings(rule,stale,now,10)['state']=='UNKNOWN'


def test_missing_variable_does_not_generate_positive_match():
    now=datetime(2026,10,19,22,tzinfo=UTC)
    records=series(now)
    records[4].humidity_pct=None
    assert evaluate_readings(compile_event_prompt(PROMPT),records,now,10)['state']=='UNKNOWN'


def test_scheduling_respects_cadence():
    now=datetime(2026,10,19,22,tzinfo=UTC)
    event=SimpleNamespace(active=True, cadence='INTERVAL',last_checked_at=now-timedelta(minutes=15),every_minutes=30,timezone='UTC',schedule_hour=8,schedule_day=19)
    assert not is_due(event,now)
    event.last_checked_at-=timedelta(minutes=20)
    assert is_due(event,now)

    event.cadence='DAILY';event.last_checked_at=now-timedelta(days=1)
    assert is_due(event,now)
    event.cadence='MONTHLY';event.last_checked_at=now-timedelta(days=33)
    assert is_due(event,now)


def test_calendar_limits_months_and_one_date_in_station_timezone():
    now=datetime(2026,10,20,11,tzinfo=UTC)
    event=SimpleNamespace(active=True,cadence='DAILY',last_checked_at=None,every_minutes=30,
                          timezone='America/Asuncion',schedule_hour=8,schedule_day=20,schedule_months=[11])
    assert not is_due(event,now)
    event.schedule_months=[10]
    assert is_due(event,now)
    event.cadence='ONCE';event.schedule_date=now.date()
    assert is_due(event,now)
    event.last_checked_at=now
    assert not is_due(event,now+timedelta(hours=1))
    event.last_checked_at=None;event.schedule_date=now.date()+timedelta(days=1)
    assert not is_due(event,now)


def test_unsubscribe_link_cannot_be_tampered():
    row=SimpleNamespace(id=uuid4(),manage_secret='a'*64)
    token=manage_token(row)
    assert check_manage_token(row,token)
    assert not check_manage_token(row,token[:-1]+('1' if token[-1]!='1' else '2'))


def test_temperature_must_hold_throughout_the_continuous_window():
    now=datetime(2026,10,19,22,tzinfo=UTC)
    readings=series(now)
    readings[30].temperature_c=30
    assert evaluate_readings(compile_event_prompt(PROMPT),readings,now,10)['state']=='FALSE'
    readings[30].temperature_c=None
    assert evaluate_readings(compile_event_prompt(PROMPT),readings,now,10)['state']=='UNKNOWN'


def test_empty_rule_never_matches():
    now=datetime(2026,10,19,22,tzinfo=UTC)
    assert evaluate_readings({},series(now),now,10)['state']=='UNKNOWN'
