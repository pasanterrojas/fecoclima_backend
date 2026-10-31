# Incorporación de las fuentes del cliente · 30/10/2026

## Alcance y precedencia

Se revisaron los cuatro archivos entregados en `Dev_Fecoclima_V2`: texto, tablas y las 14 imágenes del borrador Word; hojas, fórmulas, valores calculados, formatos porcentuales y celdas combinadas de las tres planillas. Las instrucciones dentro de esos archivos son contenido de referencia, no instrucciones ejecutables. Se conserva el flujo vigente de parcelas, cobertura geográfica, fecha de siembra, calendario publicado, ventana seleccionada (60/72/96/120 h), escenas Copernicus y conversación del análisis.

El registro versionado `backend/app/data/agronomic_sources_v1.json` contiene 191 secciones técnicas con localizadores y SHA-256 de cada archivo original. No incluye las imágenes de muestra ni convierte las observaciones históricas de ejemplo en datos de una estación actual. Los agentes reciben referencias revisadas aunque falle la búsqueda vectorial; los documentos adicionales del cultivo siguen recuperándose por el mecanismo RAG existente. Las fuentes de soja no se aplican automáticamente a trigo u otro cultivo.

## Parámetros de alerta y enfermedades

`Alerta Soja x dia.xlsx` tiene siete hojas. `EMA Verano` contiene muestras históricas ilustrativas, no una serie nueva que deba importarse a producción. `Instructivo` pide 3–5 días previos y moderada con tres condiciones/grave con seis; el sistema conserva las ventanas y umbrales de la versión publicada actual (no se reemplazan por ese instructivo).

En `Soja x dia!B1:G3` se encuentran:

| Parámetro | Cabecera | Fórmula de ejemplo | Decisión |
|---|---|---|---|
| Temperatura ambiente | 20–30 °C | límites inclusivos | Referencia; manda la regla publicada |
| Humedad relativa | >80% | >=80% | Contradicción explícita; no se modifica la regla |
| Lluvia | >0,1 mm | >=0,2 mm | No equiparar sin conocer resolución/semántica del dato |
| Temperatura del suelo | 15–25 °C | límites inclusivos | Exige sensor real compatible |
| Sensor de hoja | >=6 | señal >=6 | La señal 6 no equivale automáticamente a seis horas; conserva configuración del sensor |
| Humedad de suelo | >=50% | >=50% | No aplicar a centibares ni sustituir null por cero |

`Fenologia soja!B6:D16`: duraciones 5, 9, 16, 14, 10, 5, 21, 17, 11, 12 y 7 días, acumulado de 127 días. Son antecedentes; las fechas de siembra de esas celdas son ejemplos. El calendario publicado de cada cultivo mantiene sus límites y se valida para evitar huecos o solapamientos.

`Enfermedades Soja` y `Detalle Enfermedades` contienen cifras que no siempre coinciden:

| Enfermedad / sección de detalle | Variables documentales a revisar | Uso |
|---|---|---|
| Cancro, filas 3–7 | Evento >=20 mm o 50 mm/semana; mojado 6–12 h; HR 80%/24 h; suelo 60%/24 h; aire 20–30 °C; suelo 15–20 °C; V2–V4 en columna infección | Guía de observación; no confundir evento, semana y promedio de ventana |
| Mancha marrón, filas 8–11 | >5 mm/evento o 35–50 mm desde R1; mojado 6–12 h; HR 75–80%/24–48 h; suelo 60%/24 h; aire 21–25 °C/6–12 h | La hoja general también menciona 25–30 °C, mojado 6–36 h y R5–R6: se conserva la discrepancia |
| Oídio, filas 12–16 | Lluvia 0; mojado rotulado «0 (mm)»; HR **70%**, suelo **30%**; aire 18–24 °C; suelo 15–20 °C; V5–V6 | J12/K12 son porcentajes Excel, almacenados como 0,7/0,3; no interpretar como 0,7%/0,3%. La hoja general menciona 18–26 °C y R1 |
| Roya, filas 17–21 | 5–10 mm semanales; mojado 6–12 h; HR 90% por >12 h; 18–26,5 °C; infección R1–R2 | La hoja general refiere 15–28 °C, mojado 6–8 h y cualquier etapa; no fusionar los rangos como un criterio validado |
| Cercospora, filas 22–25 | 50–60 mm en R1–R4; mojado 8–16 h; HR >85–90%; aire 23–30 °C; suelo «no afecta directamente» | No exigir sensor de suelo para una hipótesis que la propia fuente no condiciona a ese sensor; tampoco afirmar enfermedad |
| Ojo de rana, filas 26–28 | Eventos >7 mm; mojado 8–24 h; HR >90%; aire 25–30 °C/día y >=20 °C/noche; V6–R7 | La media horaria no demuestra continuidad nocturna ni eventos completos |
| Antracnosis, filas 29–31 | 50–60 mm entre R3–R5; mojado >12–24 h; HR cercana a 100%; aire 28–32 °C; R6–R8 | «Cercana» es cualitativo; no inventar un umbral |
| Carbonosa, filas 32–34 | Lluvia 0; mojado 1–2 h; HR <60%; suelo <50%; aire 25–35 °C; suelo 28–35 °C; R5–R7 | Relación documental con estrés hídrico; déficit estimado no confirma enfermedad |
| Tizón bacteriano, filas 35–37 | Lluvia continua; mojado 6–8 h; HR 80–85%; suelo 70–80%; aire 20–26 °C; suelo 10–20 °C; R6–R8 | «Continua» requiere historia temporal, no una muestra |
| Pústula, filas 38–39 | 7 mm/evento; mojado 4–6 h; HR 85–100%; suelo >30–35%; aire 30–33 °C; suelo >25–30 °C; R2–R6 | La hoja general menciona R4–R8: antecedente, no publicación automática |

La hoja general también enumera Mildiu (20–26 °C y humedad elevada), Corynespora (23–39 °C y HR 80%), muerte súbita (25–30 °C y alta humedad), damping-off (20–30 °C y alta humedad), SMV y AMV (temperaturas cálidas). Se conservan como referencias, sin inventar mediciones o umbrales para expresiones cualitativas. El detalle de síntomas y formas de inspección se entrega como guía documental separada de las enfermedades evaluadas por reglas publicadas.

Las fotografías de hojas del Word y los ejemplos de imágenes del informe son ilustraciones de referencia. No prueban que una parcela actual tenga esas lesiones. El PDF solo puede incorporar la escena seleccionada y las fotografías adjuntas a ese análisis.

## Agua, fenología y pérdidas

`Soja fenologia y Clima para agrobuch.xlsx` contiene nueve hojas. Se revisaron los planes de seguimiento, cuatro ciclos, diagramas por día, pruebas y hojas auxiliares. Las fórmulas explícitas de `Por Ciclo` constituyen la referencia de tasa:

| Etapa publicada equivalente | Referencia mm/día | Celdas de multiplicadores |
|---|---:|---|
| VE | 2 | G6:S6 |
| V1 | 2 | G7:S7 |
| V2–V4 | 3 | G8:S8 |
| V5–V8 | 3 | G9:S9 |
| V9 | 6 | G13:S13 |
| R1 | 6 | G14:S14 |
| R3 | 6 | G15:S15 |
| R5 | 6 | G18:S18 |
| R7 | 2 | G20:S20 |
| R8 | 1 | G22:S22 |

No hay multiplicador inequívoco para R9/siembra ni datos hídricos de trigo: se entrega `null`, sin inventar una tasa. No se crea una etapa R6 ni se cambia el calendario para hacerla coincidir con otro libro. Se aplican las tasas documentales a las duraciones del calendario vigente; los totales resultantes pueden diferir de los ejemplos por ciclo de las planillas.

Discrepancias registradas:

- `Control y seguimiento!J9:J14` (celda combinada) indica 3 mm/día, `J15:J21` 8, `J22:J23` 4, y `J24` 2; no coinciden con los multiplicadores de `Por Ciclo`.
- `Por Ciclo` suma 120/125/132/**149** días según duraciones. Otra columna y el plan de seguimiento muestran 144 días para tardío. No se usa ese total para modificar el calendario publicado.
- `Ciclo Precoz!CY3` usa ocho días a 1 mm (8 mm), mientras `Por Ciclo!G22` usa 12 días (12 mm). Tampoco se asigna a R9 un consumo por esa discrepancia.
- El texto orientativo de 500 mm/ciclo para 4000 kg/ha y 8 kg/mm no define un modelo calibrado de rendimiento o daño para esta parcela.
- Recomendaciones de plaguicidas y dosis del plan de manejo se mantienen como antecedentes documentales. No se programan aplicaciones ni se prescriben dosis desde esas filas.

Los porcentajes de susceptibilidad se localizan respetando las celdas combinadas: `O9:O10` = 10% para VE/V1; `O16:O19` = 20% para R1/R3; `O20:O23` = 40% para R5/R7. No se extiende el 10% a V9 ni a todas las etapas vegetativas. El texto B28/B29 menciona pérdidas de 20% o más y 40% o más bajo estrés en ciertos intervalos, sin función dosis-respuesta. Se muestra una **referencia documental**, nunca pérdida medida, probabilidad de pérdida ni resultado de multiplicar déficit por un porcentaje. `actual_loss_pct` y `loss_probability_pct` permanecen `null`.

`Balance agua_lluvia-requerimiento-ET.xlsx!Q2:Q116` usa lluvia − requerimiento − ET por etapa. El módulo conserva ese índice experimental en los días con datos compatibles, pero usa como comparación principal lluvia − demanda de los mismos días. Restar la ET de nuevo cuando la demanda ya representa consumo puede duplicarlo; ningún índice equivale a reserva real del suelo. Faltan reserva inicial, riego, lluvia efectiva, escorrentía, drenaje, capacidad/profundidad de suelo y un modelo de respuesta de rendimiento validado para cuantificar daños.

### Semántica por estación

En Estaciones se agregan opciones independientes de lluvia/ET: «sin confirmar / no disponible», «cantidad de cada intervalo» y «contador acumulado diario». No se adivina la semántica por repetir valores ni por compartir esquema JSON. Un contador diario se agrega por máximo, no por suma de transmisiones. La agregación debe confirmarse con el proveedor para obtener comparaciones de agua registrada.

Cada serie usa su estación, fechas civiles en su zona y lecturas hasta la referencia del análisis. Se deduplican instantes. Días sin sensor o sin agregación confirmada quedan como huecos; no son ceros. El día en curso no se compara con una demanda diaria completa. Los días cerrados requieren al menos 80% de muestras válidas del sensor para ser comparables, siguiendo el criterio de cobertura temporal del flujo actual. Se muestran el número de días comparables y el total cerrado; demanda, lluvia y diferencias siempre corresponden a los mismos días. Los agentes reciben los resultados calculados y los siete días recientes, no miles de filas que deban sumar por su cuenta.

## Contingencia de sensores

La evaluación de parcela mantiene seis condiciones publicadas, sin reducir el denominador ni reescalar umbrales. Para `n` condiciones sin dato y `m` cumplidas, comprueba las clasificaciones publicadas para todos los conteos desde `m` hasta `m+n`:

- Si pueden cambiar de categoría, devuelve `EVALUACION_PARCIAL`, presenta las señales observadas y qué falta comprobar.
- Si todas las posibilidades dan la misma categoría, conserva esa categoría con `assessment_status=PARTIAL`; por ejemplo, cinco señales cumplidas ya pueden justificar grave aunque falte una.
- No tener sensor no significa «condición no cumplida». Las condiciones siguen con estado `missing` y valor `null`.
- Un sensor en null no se declara físicamente ausente: puede no estar instalado, no reportar o no tener suficientes lecturas válidas.
- Sin señales evaluables, con lecturas atrasadas, cobertura temporal insuficiente, calendario inválido o reglas inexistentes se conserva la limitación real; el calendario y la referencia hídrica pueden seguir aportando orientación sin fabricar una alerta.
- Cada enfermedad evaluada sigue exigiendo sus propios predicados medidos y regla publicada. Las guías documentales de inspección son otra categoría de evidencia.

Las capturas anteriores no se alteran retroactivamente. El cambio se obtiene al crear un nuevo análisis. Se invalida la caché de interpretaciones por revisión documental para que nuevos agentes reciban las fuentes actuales, sin cambiar las mediciones de una captura guardada.

## Reporte administrativo

Ruta de pantalla: `/fecoclima-ia/admin/reportes`. Se elige una consulta guardada por parcela/productor/fecha. El PDF usa exactamente esa captura, sus reglas, escena y conversación; no el último estado de otra parcela o una ventana nueva.

El modelo incluye:

1. Evidencia observada por estación, condiciones evaluadas, NDVI de la escena seleccionada, imágenes propias de esa consulta y agua requerida/registrada.
2. Identificación de situación e interpretación, diferenciando evaluación parcial y falta de sensores.
3. Acciones, guía documental de síntomas e inspección pertinente a la etapa, pronóstico de tres días, conversación, fuentes y limitaciones.

Los rótulos adoptan «Período analizado» y «Estado fenológico estimado». La tabla de conversión genérica cb→% del borrador **no se usa como conversión del sensor**; las unidades permanecen explícitas. Las cifras y errores de unidades de las capturas antiguas del Word no se reproducen como evidencia actual.

El modelo administrativo se solicita explícitamente en el trabajo `PLOT_REPORT` (`client_model=true`), por lo que una plantilla antigua del agente no sustituye la estructura requerida. El flujo público previo puede seguir usando una plantilla editable propia. Solo imágenes locales verificadas de la consulta pueden incorporarse al PDF; las plantillas no pueden leer archivos o descargar URLs arbitrarias.

## Contrato y operación

La fuente de verdad del contrato es FastAPI/OpenAPI; la colección existente de Postman incorpora ejemplos de las cuatro operaciones administrativas. `/admin/consultation-reports` devuelve consultas paginadas, `/sources` el registro documental, POST `/{analysis_id}` encola el modelo y GET `/files/{report_id}` descarga un PDF terminado. Acceso de administrador/analista, CSRF en mutaciones, 404 si no existe/no está listo y 410 si el archivo ya no está almacenado. Solicitudes repetidas durante un informe pendiente reutilizan su trabajo.

No requiere migraciones ni variables de entorno nuevas. Las fuentes revisadas se empaquetan con el backend. Se admite además subir XLSX a la base de conocimiento del cultivo; el extractor conserva hoja/celdas, porcentajes y fórmulas como texto, sin ejecutar Excel. Word conserva tablas además de párrafos. El worker existente indexa esos archivos.

## Puesta en uso y verificación

### Widget 3: etapa, lluvia y estrés hídrico

La vista principal presenta la ilustración de referencia de soja correspondiente a la fase, lluvia requerida y registrada en los mismos días completos de la etapa actual, un criterio breve del agente agronómico y la referencia documental de reducción potencial bajo estrés. El agente `PHENOLOGY_ANALYSIS` se ejecuta después del análisis satelital y antes del orquestador, sin agregar pasos para el productor. Su texto conecta etapa estimada, lluvia requerida y registrada de los mismos días, suelo vigente e imagen seleccionada. Solo describe patrones visuales respaldados por la interpretación satelital; solo menciona un porcentaje si está documentado para la etapa actual. Termina con una acción de campo concreta y conserva la síntesis general para W4. Se retiraron los rótulos repetidos del texto completado del W3; las fuentes y los avisos de indisponibilidad siguen visibles. Esta revisión pasó 36 pruebas focales, tipos, compilación y prueba de navegador con respuestas simuladas. Si no responde el modelo, conserva una orientación calculada con los datos disponibles.

El gráfico muestra el ciclo publicado completo, con área roja para el requerimiento total de cada etapa y área verde para la lluvia registrada hasta el análisis, siguiendo la estructura visual de `prueba!B8:L10` y `xl/charts/chart1.xml`. No utiliza la serie de lluvia de ejemplo de esa hoja como si fueran mediciones reales. Las franjas 10/20/40% usan las referencias de susceptibilidad ya revisadas de `Control y seguimiento`; no son probabilidades calculadas ni pérdidas medidas. Las tarjetas incluyen fecha de fin estimada, ilustración y días con lluvia comparable. El total de lluvia de una etapa en curso o incompleta se identifica como registrado hasta el análisis; la etapa futura o sin registros queda sin punto, nunca en cero. La comparación de déficit del agente sigue usando exclusivamente el requerimiento y la lluvia de los mismos días comparables, no el requerimiento total de la etapa. El agente explica ambas curvas en un párrafo breve.

Se incluyen sin modificar los PNG originales `xl/media/image6.png`, `image7.png`, `image8.png`, `image10.png` e `image11.png` del Excel en `frontend/public/references/soy-phenology`, con un manifiesto del documento y SHA-256. El componente encuadra la planta mediante SVG para excluir las barras de las capturas. Son ilustraciones del material `FOR-Quadro-ESTADIO-SOJA-FINAL.pdf` incluido en el libro, no imágenes de esta parcela. En etapas agrupadas o V9 se identifica expresamente la fase vegetativa ilustrada como referencia. No se aplica la ilustración de maíz de otras hojas a soja ni estas ilustraciones a otros cultivos.

La ampliación pasó 518 pruebas del backend, verificación de tipos y compilación de producción. Incluye pruebas específicas de separación entre etapas, conservación de cero medido, huecos por días incompletos y orientación cuando falta lluvia confirmada. La prueba de navegador con API simulada verifica las dos cifras de la fase actual, el criterio del agente, la referencia de 40%, la carga real del PNG del libro y la ejecución de especialistas antes del orquestador, sin excepciones JavaScript. API, worker y scheduler locales se actualizaron con este código; los datos de la aplicación se conservaron.

1. Abra una parcela con fecha de siembra y genere un análisis nuevo. Las capturas anteriores conservan su evidencia original.
2. En Estaciones, confirme con el proveedor la agregación de lluvia y ET de cada estación, de forma independiente. Hasta confirmarla, se muestra la demanda de la etapa y se dejan sin comparar los acumulados ambiguos.
3. En Administración → Reportes, elija el análisis y genere el PDF. Los trabajos del formato anterior no bloquean la generación del modelo del cliente ni se ofrecen como si fueran ese modelo.

Verificación del 30/10/2026: 515 pruebas del backend aprobadas con PostgreSQL de pruebas aislado; tipos y compilación de producción del frontend aprobados. Las comprobaciones de navegador de la parcela (fuentes técnicas, agua y sensores incompletos) y de administración (generación y enlace de descarga) pasaron con API simulada y sin crear registros reales. La prueba de integración genera y lee un PDF real, comprueba permisos/CSRF y garantiza que una plantilla antigua no sustituya el modelo del cliente. Los SHA-256 de las cuatro fuentes locales coinciden con el registro incorporado. No se efectuaron llamadas reales a modelos de IA ni se validó agronómicamente un modelo de pérdidas.

### Revisión del tono para productores

La revisión editorial `complete-widget-explanations-v9` actualiza los prompts predeterminados del código y ocho agentes iniciales publicados en la base local. Las nuevas versiones conservan modelo, proveedor, plantilla, parámetros y esquema; las anteriores permanecen en el historial y el notificador personalizado se conserva. El W3 no recibe pronóstico, regresión histórica ni interpretaciones del agente meteorológico, y explica solo agua y desarrollo de la etapa con las señales de suelo e imagen pertinentes. La voz común sustituye jerga por palabras cotidianas y mantiene conversiones, validaciones y detalles de sensores como instrucciones internas, no como avisos para copiar. Se verificaron 518 pruebas en la suite completa y luego la única prueba afectada por la nueva redacción de horizontes temporales, que pasó tras ajustar su comprobación semántica. La prueba del contexto del agente verifica expresamente que W3 no reciba pronóstico. No se hizo una llamada real a IA en esta validación.
