"""General comparisons, windows and boolean logic; no disease-specific thresholds."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.notification_rules import validate_interpretation, evaluate_expression_rule


def condition(variable='temperature_c', comparison='gt', value=10, **kwargs):
    return {'kind':'condition','variable':variable,'comparison':comparison,'value':value,
            'aggregation':'latest','hours':0,**kwargs}


def rule(*clauses, operator='ALL'):
    return validate_interpretation({'supported':True,'expression':{'kind':'group','operator':operator,'clauses':list(clauses)}})


def readings(now, hours=3, **values):
    return [SimpleNamespace(sampled_at=now-timedelta(minutes=10*i),temperature_c=15,humidity_pct=90,
                            rain_mm=1,wind_speed_ms=7,soil_moisture_pct=35,**values) for i in range(hours*6+1)]


@pytest.mark.parametrize(('comparison','value','upper','expected'),[
    ('gt',10,None,'TRUE'),('gt',15,None,'FALSE'),('gte',15,None,'TRUE'),
    ('lt',20,None,'TRUE'),('lt',15,None,'FALSE'),('lte',15,None,'TRUE'),
    ('eq',15,None,'TRUE'),('ne',15,None,'FALSE'),
    ('between',10,20,'TRUE'),('outside',10,20,'FALSE'),('outside',20,30,'TRUE'),
])
def test_generic_comparisons(comparison,value,upper,expected):
    now=datetime(2026,10,22,12,tzinfo=UTC)
    compiled=rule(condition(comparison=comparison,value=value,upper=upper))
    assert evaluate_expression_rule(compiled,readings(now),now,10)['state']==expected


def test_user_temperature_condition_is_strictly_longer_than_one_hour():
    now=datetime(2026,10,22,12,tzinfo=UTC)
    compiled=rule(condition(aggregation='continuous',hours=1,duration_comparison='more_than'))
    assert compiled['max_hours']==1 and 'más de 1 h' in compiled['summary']
    assert evaluate_expression_rule(compiled,readings(now,1),now,10)['state']=='UNKNOWN'
    data=readings(now,2)
    assert evaluate_expression_rule(compiled,data,now,10)['state']=='TRUE'
    # Include the reading exactly at the cutoff, even with a strict duration.
    data[6].temperature_c=8
    assert evaluate_expression_rule(compiled,data,now,10)['state']=='FALSE'
    data[6].temperature_c=None
    assert evaluate_expression_rule(compiled,data,now,10)['state']=='UNKNOWN'


def test_duration_anchored_to_last_observation_not_current_wall_clock():
    now=datetime(2026,10,22,12,tzinfo=UTC)
    compiled=rule(condition(aggregation='continuous',hours=1,duration_comparison='more_than'))
    assert evaluate_expression_rule(compiled,readings(now-timedelta(minutes=10),1),now,10)['state']=='UNKNOWN'


def test_or_and_unknown_follow_three_valued_logic():
    now=datetime(2026,10,22,12,tzinfo=UTC)
    known=condition(value=10)
    missing=condition(variable='pressure_hpa',comparison='lt',value=1000)
    assert evaluate_expression_rule(rule(known,missing,operator='ANY'),readings(now),now,10)['state']=='TRUE'
    assert evaluate_expression_rule(rule(known,missing),readings(now),now,10)['state']=='UNKNOWN'
    false=condition(value=30)
    assert evaluate_expression_rule(rule(false,missing),readings(now),now,10)['state']=='FALSE'
    nested={'kind':'group','operator':'ANY','clauses':[false,known]}
    assert evaluate_expression_rule(rule(nested,condition(variable='soil_moisture_pct',comparison='lt',value=40)),readings(now),now,10)['state']=='TRUE'


@pytest.mark.parametrize(('aggregation','variable','threshold','expected'),[
    ('sum','rain_mm',5,'TRUE'),('sum','rain_mm',6,'FALSE'),
    ('mean','temperature_c',14,'TRUE'),('min','temperature_c',15,'FALSE'),
    ('max','wind_speed_ms',6,'TRUE'),('change','temperature_c',0,'FALSE'),
    ('percent_change','temperature_c',0,'FALSE'),
])
def test_aggregations_of_different_metrics(aggregation,variable,threshold,expected):
    now=datetime(2026,10,22,12,tzinfo=UTC)
    compiled=rule(condition(variable=variable,value=threshold,aggregation=aggregation,hours=1))
    assert evaluate_expression_rule(compiled,readings(now),now,10)['state']==expected


def test_missing_or_stale_data_never_matches_and_gaps_are_detected():
    now=datetime(2026,10,22,12,tzinfo=UTC)
    compiled=rule(condition(aggregation='continuous',hours=3))
    assert evaluate_expression_rule(compiled,[],now,10)['state']=='UNKNOWN'
    assert evaluate_expression_rule(compiled,readings(now-timedelta(hours=2)),now,10)['state']=='UNKNOWN'
    data=readings(now,3)
    del data[3:9]
    assert evaluate_expression_rule(compiled,data,now,10)['state']=='UNKNOWN'


@pytest.mark.parametrize('clause',[
    condition(variable='exec'),condition(variable='disease_detected'),
    condition(variable='humidity_pct',value=150),condition(value=float('nan')),
    condition(comparison='between',upper=5),condition(aggregation='continuous'),
    condition(aggregation='sum',hours=1),condition(variable='total_rain_mm',aggregation='sum',hours=1),
    condition(aggregation='mean',hours=1,duration_comparison='more_than'),
    condition(variable='leaf_sensor_value'),
])
def test_model_output_cannot_execute_code_or_invent_metrics(clause):
    with pytest.raises(ValueError): rule(clause)


def test_missing_details_are_reported_without_inventing_thresholds():
    with pytest.raises(ValueError,match='qué temperatura'):
        validate_interpretation({'supported':False,'reason':'Indique qué temperatura considera calor excesivo.','expression':None})
