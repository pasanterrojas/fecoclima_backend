from copy import deepcopy
import pytest
from app.services.parcel_identity import geometry_key, producer_label
from app.services.soil_tension import soil_tension
from app.services.alert_engine import ConditionSpec, evaluate_daily_alert


def test_polygon_identity_ignores_ring_start_and_direction():
    polygon = {'type':'Polygon','coordinates':[[[-55,-25],[-54.99,-25],[-54.99,-24.99],[-55,-25]]]}
    reverse = deepcopy(polygon)
    reverse['coordinates'][0] = list(reversed(reverse['coordinates'][0]))
    assert geometry_key(polygon) == geometry_key(reverse)
    assert producer_label('Ana','1234567') == 'Ana · CI 1234567'


@pytest.mark.parametrize('count,expected', [(0,'NINGUNA'),(1,'NINGUNA'),(2,'MODERADA'),(3,'MODERADA'),(4,'MODERADA'),(5,'GRAVE'),(6,'GRAVE')])
def test_traffic_light_for_six_conditions(count, expected):
    specs = [ConditionSpec(str(i),str(i),str(i),'gt',threshold_value=0) for i in range(6)]
    result = evaluate_daily_alert({str(i):1 if i < count else 0 for i in range(6)},specs)
    assert result['level'] == expected


@pytest.mark.parametrize('value,band', [(0,0),(9,0),(10,1),(30,2),(60,3),(69,3),(100,4),(200,4)])
def test_soil_tension_boundaries(value,band):
    assert soil_tension(value,'cb')['band_index'] == band
    assert soil_tension(value,'%') is None
    assert soil_tension(-1,'cb') is None
    assert soil_tension(201,'cb') is None


def test_precise_provider_failures_do_not_leak_provider_body():
    import httpx
    from app.services.ai import AIResponseError, AIProviderError, provider_fallback_reason
    assert provider_fallback_reason(AIResponseError('provider_output_limit','Empty',{'total_tokens':1500})) == 'provider_output_limit'
    assert provider_fallback_reason(httpx.ReadTimeout('private-url')) == 'provider_timeout'
    assert provider_fallback_reason(AIProviderError(404,'private','private response')) == 'provider_model'
    assert provider_fallback_reason(AIProviderError(400,'private','private response')) == 'provider_request'


def test_widget_explanations_are_limited_but_reports_keep_detail():
    from app.services.widget_text import widget_summary, compact_widget_result
    text = '**Revise el cultivo.** ' + 'La humedad sigue elevada en la parcela. ' * 30
    summary = widget_summary(text)
    assert len(summary) <= 300
    assert summary.startswith('Revise el cultivo.')
    assert '**' not in summary
    assert len(widget_summary('🌾' * 1000)) <= 300
    assert len(compact_widget_result({'markdown':text}, 'ORCHESTRATOR')['markdown']) <= 300
    assert compact_widget_result({'markdown':text}, 'PDF_REPORT')['markdown'] == text


def test_soil_sensor_unit_mismatch_explains_partial_evaluation():
    from types import SimpleNamespace
    from app.services.plot_pipeline import evaluate_window
    specs=[ConditionSpec(str(i),str(i),'humidity_avg_pct','gt',threshold_value=70,unit='%') for i in range(5)]
    specs.append(ConditionSpec('soil','Humedad del suelo','soil_moisture_avg_pct','gte',threshold_value=50,unit='%'))
    result=evaluate_window({'rule_values':{'humidity_avg_pct':85,'soil_moisture_avg_pct':69},'units':{'soil_moisture_avg_pct':'cb'},'variable_completeness_pct':{'soil_moisture_avg_pct':100,'humidity_avg_pct':100},'completeness_pct':99,'stale':False},specs,[],{'stage_code':'V1'},SimpleNamespace(configuration={}))
    assert result['level']=='DATOS_INSUFICIENTES'
    missing=result['conditions'][-1]
    assert missing['status']=='missing' and missing['missing_reason']=='incompatible_unit'
    assert 'cb' in missing['explanation'] and '%' in missing['explanation']
    assert result['missing_reasons']==[missing['explanation']]
