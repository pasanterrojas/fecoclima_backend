"""Interpret only measured centibar tension; never treat percentages as tension."""
import math

BANDS = [
    (0,10,'Suelo saturado','Habitual justo después de lluvia abundante o riego copioso.','#48a6d5'),
    (10,30,'Capacidad de campo','Agua disponible en un rango favorable sin saturar las raíces.','#49ba73'),
    (30,60,'Rango normal de riego','Momento habitual para valorar el inicio del riego según cultivo y suelo.','#f2cb49'),
    (60,100,'Suelo seco','Puede indicar estrés hídrico, salvo cultivos tolerantes o suelos muy arcillosos.','#f49a40'),
    (100,200,'Severamente seco','La extracción de agua es difícil y aumenta el riesgo de marchitez.','#dc5953'),
]


def soil_tension(value: float | None, unit: str) -> dict | None:
    if value is None or unit != 'cb' or not math.isfinite(float(value)) or not 0 <= value <= 200:
        return None
    index = next((i for i, (_, end, *_) in enumerate(BANDS) if value < end), len(BANDS)-1)
    return {'value':value, 'unit':'cb', 'position_pct':round(value / 2, 3), 'band_index':index,
            'label':BANDS[index][2], 'description':BANDS[index][3],
            'bands':[{'min':start,'max':end,'label':label,'description':description,'color':color} for start,end,label,description,color in BANDS]}
