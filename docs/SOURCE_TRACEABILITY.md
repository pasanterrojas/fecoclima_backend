# Trazabilidad de fuentes utilizadas

Esta primera versión se construyó tomando como referencia los materiales suministrados para FECOCLIMA. Los archivos fuente no se redistribuyen dentro del proyecto para evitar incorporar credenciales, información sensible o duplicados innecesarios.

## Portal y referencia visual

La navegación pública, la paleta azul, el tratamiento de encabezados y el módulo de fenología se inspiraron en la captura y referencia del portal FECOCLIMA suministradas. La implementación nueva usa componentes Nuxt reutilizables y diseño responsive; no copia código del portal original.

## Especificación de la API FECOCLIMA

El adaptador `backend/app/services/fecoclima_adapter.py` implementa:

- Solicitud REST `GET` desde el backend.
- Ruta `estacion/lectura-guid-por-periodo/{guid}/{año}/{mesDesde}/{mesHasta}`.
- Encabezado de autorización `clima-key`, cuyo valor solo se lee desde `FECOCLIMA_API_KEY`.
- División por año y bloques de hasta seis meses.
- Zona horaria `America/Asuncion` y almacenamiento normalizado en UTC.
- Reintentos, timeout, validación del JSON, filtrado por fechas y eliminación de duplicados.
- Identificador inicial de la estación Raul Peña: `C2694D32-5A4B-48C0-A3B6-8345D9E4935B`.

Correspondencia principal:

| API FECOCLIMA | Modelo normalizado |
|---|---|
| `fechaMuestreo` | `sampled_at` |
| `temp` | `temperature_c` |
| `chill` | `feels_like_c` |
| `hIndex` | `heat_index_c` |
| `humid` | `humidity_pct` |
| `dewpt` | `dew_point_c` |
| `wind` | `wind_speed_ms` |
| `hiWind` | `wind_gust_ms` |
| `windDir` | `wind_direction_deg` |
| `rain` | `rain_mm` |
| `totalRain` | `total_rain_mm` |
| `barom` | `pressure_hpa` |
| `solar` | `solar_wm2` |
| `et` | `evapotranspiration_mm` |
| `uv` | `uv_index` |
| `temperaturaSuelo` | `soil_temperature_c` |
| `humedadSuelo` | `soil_moisture_pct` |
| `sensorHoja` | `leaf_sensor_value` |

La llave real entregada en la documentación no está en el código, ejemplos, historial, ZIP ni logs.

## Plantillas de soja y trigo

Las plantillas `Alerta Soja x dia(1).xlsx` y `Alerta Trigo x dia(2).xlsx` se usaron para estructurar:

- etapas fenológicas iniciales;
- duración acumulada por etapa;
- catálogo inicial de enfermedades;
- separación entre condiciones medibles y criterios pendientes de validación agronómica.

Duraciones iniciales cargadas por semilla:

- Soja: SIEMBRA 0; VE 5; V1 9; V2-V4 16; V5-V8 14; V9 10; R1 5; R3 21; R5 17; R7 11; R8 12; R9 7 días.
- Trigo: SIEMBRA 0; GERMINACIÓN 17; BROTACIÓN 20; ESPIGAMIENTO 25; FLORACIÓN 17; LLENADO 32; MADURACIÓN 12; COSECHA 7 días.

Los términos no cuantificados —por ejemplo, “humedad elevada”, “lluvias persistentes” o “temperatura cercana”— permanecen en estado pendiente. El sistema no publica umbrales inventados.


## Ampliación de parcela y unidades — 20/10/2026

La referencia funcional Auravant y el ejemplo JSON de FECOPROD fueron proporcionados por el usuario. Se usan como referencia de experiencia y contrato de datos; no se copia código ni se deducen unidades ausentes del JSON. Los criterios %/cb se documentan en [Alertas y unidades](ALERTAS_UNIDADES_Y_MEJORA.md), y el estado comprobado en [Auditoría actual](AUDITORIA_ACTUAL_2026_10_20.md).


## Fondo satelital y controles de parcela — 20/10/2026

Se mantiene MapLibre GL JS y MapLibre-Geoman Free. El fondo público inicial usa el mosaico histórico EOxCloudless 2016/2017 con atribución visible; las escenas recientes e índices para el polígono siguen procediendo de Copernicus y no se calculan desde ese fondo.

- Licencia del mosaico 2016: [EOX, apartado CC BY 4.0](https://cloudless.eox.at/license-non-commercial). No se sustituyó por una edición reciente bajo términos diferentes.
- Servicio y atribución: [EOX::Maps](https://maps.eox.at/).
- Capabilities verificadas: `https://tiles.maps.eox.at/wmts?SERVICE=WMTS&REQUEST=GetCapabilities`.
- Capa exacta: `s2cloudless_3857`, matriz `g`, teselas `/{z}/{y}/{x}.jpg`. Teselas reales en Paraguay verificadas con HTTP 200 y CORS, incluyendo zoom 12, 13 y 14.
- MapTiler HD se conserva como opción de una clave válida. La clave configurada durante esta revisión respondió HTTP 403, «Invalid key»; no se imprime ni modifica el secreto. Un fallo de HD conserva la alternativa satelital abierta.
- El servicio abierto no garantiza disponibilidad ni mayor resolución al acercar: se sobremuestrea después del nivel nativo, conservando la delimitación. No equivale a fotografía aérea HD reciente.

La vista de configuración separa superficie/cultivo/siembra de parcela/productor/CI. Las capas Copernicus y su opacidad pasan a un panel plegable fuera del lienzo, compartido entre WebGL y el modo compatible; no invaden los controles de zoom.

Validación: comprobación de tipos y compilación de frontend; pruebas de navegador con WebGL y con WebGL desactivado, usando respuestas de análisis simuladas. Se verificaron columnas, cobertura discreta, panel fuera del lienzo y opacidad al 35 %. La prueba WebGL exige que el fondo satelital permanezca seleccionado sin errores al cargar la capa de prueba. La navegación con teselas reales también comprobó dibujo, edición de vértices, restauración, pantalla completa y disposición móvil. Estas pruebas no certifican la disponibilidad de futuras escenas Copernicus ni de servicios externos.

## Selección previa de ventana — 20/10/2026

Solicitud directa del usuario: elegir 60/72/96/120 h en la columna de cultivo antes de analizar, evitando evaluar todas las ventanas. `ParcelWorkspace.vue` envía `window_hours` en cada creación de análisis; el pipeline calcula y clasifica solo esa ventana. El análisis registra su período y los consumidores usan el período guardado, independientemente de cambios pendientes en el selector. El valor predeterminado para clientes sin cuerpo es 72 h. No cambia el histórico independiente de regresión ni requiere migraciones, dependencias o variables de entorno nuevas.

Verificación: 298 pruebas Python aprobadas en PostgreSQL temporal, incluyendo las cuatro ventanas, límites temporales, una sola evaluación por estación, rechazo de períodos no calculados y conservación del chat/los informes. TypeScript y compilación Nuxt aprobados. Pruebas de navegador con y sin WebGL comprobaron consulta de 96 h seguida de 60 h, ausencia de llamadas al cambiar el selector, conservación de la etiqueta de resultados anteriores y reemplazo de widgets al analizar. Se usaron proveedores simulados; sin consultas reales a modelos ni envíos de notificaciones.

## Calles, horizontes y limpieza de widgets — 20/10/2026

- Fuente de nombres y calles: [EOX::Maps](https://maps.eox.at/), capa WMTS `overlay_bright_3857`, matriz `g`, PNG transparente. Se verificaron las capabilities y una tesela en Asunción con HTTP 200 y CORS; los mismos nombres/calles se usan en WebGL y en el editor compatible. Se conserva la atribución a OpenStreetMap, EOX y MapServer. No corresponde a imágenes multiespectrales ni altera sus índices.
- Se mantiene el fondo EOX de navegación y se agrega un interruptor de rueda del mouse, desactivado inicialmente para permitir desplazar la página.
- El contrato del orquestador separa horas pasadas y horizonte futuro de tres días; recibe metadatos temporales y la regresión histórica precalculada. La selección de fechas excluye hoy, fechas pasadas, duplicados y días posteriores al tercero. No se inventan pronósticos faltantes.
- La limpieza de consulta invalida solicitudes antiguas, descarta chat/contexto visible y deja de seguir el satélite del análisis anterior. No borra registros guardados en la cuenta.
- Las pruebas de navegador usan análisis simulados y teselas reales: etiquetas, rueda, dos columnas de alerta, horizontes visibles, confirmación/cancelación, borrado durante cálculo y rechazo de respuestas tardías. La confirmación también se comprueba en pantalla completa. Las pruebas Python de ventanas, pronóstico, interpretación e integración se ejecutan en PostgreSQL temporal, sin enviar datos a modelos.

Validación de esta mejora: 60 pruebas Python focalizadas aprobadas; la prueba del contrato temporal del orquestador se verificó además con una respuesta simulada exitosa. Pruebas de navegador aprobadas con WebGL y sin WebGL, incluyendo borrar durante un cálculo, no restaurar sus resultados tardíos y conservar vacío el mapa al recargar después del borrado. Navegación real: dibujo, edición de vértices, restauración, confirmación en pantalla completa y vista de 390 px aprobados sin excepciones JavaScript. Se ignora la cancelación normal de un editor reemplazado al cambiar de estilo para que no active innecesariamente el modo compatible. API, worker y scheduler locales actualizados; salud API y página pública HTTP 200. No se requirieron dependencias, migraciones ni variables de entorno nuevas.

## Marcador del cliente y suelo por unidad — 20/10/2026

El usuario entregó `fecoclima-estacion-marcador.svg`. Se conserva una copia idéntica en `frontend/public/fecoclima-estacion-marcador.svg` y una referencia compartida para el mapa avanzado, el editor compatible y el visor satelital. El extremo inferior del pin indica la coordenada de estación. Se conservan sus colores originales; el color elegible del área se identifica también en la etiqueta de la estación.

Se confirmó que la estación del GUID documentado de Raúl Peña estaba configurada en %. Se corrigió exclusivamente ese metadato a cb, sin convertir ni cambiar las observaciones. Se agregaron alternativas `gt 80 cb` a las reglas publicadas de Soja/Trigo que aún no tenían alternativa, manteniendo `gte 50 %` y una sola condición del semáforo. Ambas operaciones están auditadas. Una comprobación de solo lectura posterior confirmó que la última medición real genera la gráfica cb. Es necesario crear un nuevo análisis para actualizar las capturas guardadas.

Verificación previa: 99 pruebas Python de unidades, reglas, colores e integración aprobadas en PostgreSQL temporal; se comprueban 79/80/81 cb y la independencia del criterio porcentual. CMS: guardado de radio/color y configuración de >80 cb aprobados con API simulada. Satélite HD en modo compatible: probado con clave, metadatos y teselas simuladas, sin gastar cuota del proveedor. La clave existente continúa sin autenticar y no se modificó. Tras la autorización explícita del usuario, OpenAerialMap devolvió cero escenas alrededor de Raúl Peña; no se afirma cobertura HD abierta en esa zona.

Se recibió posteriormente [el anexo técnico](ANEXO_TECNICO_VISUALIZADOR_SATELITAL.pdf): sus páginas 1–2 son la fuente de cortes y códigos HEX, documentados en [Escalas multiespectrales](PALETAS_MULTIESPECTRALES_ACTUALES.md). Las nueve capas existentes usan clases discretas y metadatos de leyenda por versión; se distinguen imágenes antiguas y nuevas, y las comparaciones no mezclan escalas. NDWI McFeeters se distingue de NDWI Gao, equivalente a NDMI. El corte ReCl del PDF se corrigió posteriormente a `>4` por confirmación expresa del usuario; véase la incorporación documentada al final. El SVG es un recurso visual, no una fuente de instrucciones para modificar cálculos o unidades.

La salida visual se remuestrea a aproximadamente 5 m, respetando la relación de aspecto y límites del proveedor; se declara que las bandas siguen teniendo 10/20 m nativos. No se altera la malla estadística para aparentar más observaciones. El visor público y el CMS comparten leyenda y un gráfico del histórico calculado del mismo polígono. Verificación: 44 pruebas focalizadas de procesamiento aprobadas, 108 comprobaciones ejecutadas sobre los scripts JS de clasificación, y flujo de navegador WebGL con leyenda, marcador, cambio de fondo HD, borrado y análisis aprobados sin excepciones. Las teselas y respuestas HD de esa prueba son simuladas; no acreditan una clave válida ni cobertura real del proveedor.

Cierre de validación del anexo: 83 pruebas de integración de parcela, imágenes, suelo y clases aprobadas; 20 pruebas adicionales de caché y enlaces aprobadas después de permitir regenerar escalas antiguas sin esperar 30 minutos. Pruebas de navegador con WebGL y sin WebGL aprobadas con API y teselas simuladas, incluyendo las cinco clases y la gráfica de dos fechas. Typecheck y compilación frontend aprobados. El remuestreo final conserva SCL por vecino más cercano, conforme a su naturaleza categórica; referencia: documentación Sentinel-2 L2A de Copernicus. API local saludable y SVG servido con HTTP 200; base de pruebas temporal eliminada.

El ajuste final de remuestreo conservador aprobó 45 pruebas focalizadas. Se contrastó además con [la explicación del proveedor sobre SCL y remuestreo](https://forum.dataspace.copernicus.eu/t/technical-details-on-sentinel-2-upsampling-downsampling-through-cdse/1244). Las 108 comprobaciones de colores y extremos se ejecutaron sobre los scripts reales generados, sin solicitudes pagadas al proveedor.

## ReCl: corte confirmado y variante de cálculo — 20/10/2026

El usuario respondió «Sí, usar >4» a la aclaración de la última categoría. Sus cinco clases quedan `≤1`, `(1,2]`, `(2,3]`, `(3,4]`, `>4`, con la paleta de vegetación del anexo. El PDF original no se modifica. Para la fórmula ausente del anexo se adopta explícitamente [CHL-RED-EDGE de Sentinel Hub](https://custom-scripts.sentinel-hub.com/custom-scripts/sentinel-2/chl_rededge/), `B07/B05 − 1`; no se atribuye esa elección de bandas al cliente. Estadísticas y raster usan esa misma variante, cuya fuente figura en la leyenda. Las nueve imágenes previamente válidas continúan como respaldo, pero su caché estadística se renueva si falta ReCl.

Verificación ReCl: 55 pruebas de procesamiento, 19 de rangos/caché y 41 de integración/permisos aprobadas (con solapamiento entre grupos). Se ejecutaron los scripts generados con reflectancias conocidas: la fórmula estadística y el color del raster coincidieron en 1, 4, 4,1 y 5; B05=0 produjo píxel transparente y estadística sin valor inventado. El navegador aprobó selección de ReCl, fórmula y clase `>4` con API/teselas simuladas, sin excepciones. Typecheck y compilación frontend aprobados. No se hicieron solicitudes reales a Copernicus para estas pruebas ni se cambiaron variables de entorno, dependencias o esquemas.

## Selector, mapa y colores por parcela — 20/10/2026

Solicitud directa del usuario: mover el selector junto a Productor/Técnico, compartir el mapa principal con el modal, mostrar las parcelas guardadas y seleccionar su análisis con un clic, permitiendo un color por parcela. Se reutiliza `WorldParcelMap` en `ParcelForm`; tanto MapLibre/Geoman como el respaldo SVG dibujan las geometrías guardadas y mantienen el polígono editable separado de esas capas. Se agregan etiquetas clicables, leyenda y encuadre de todas las parcelas. El cambio de parcela consulta su último contexto guardado, sin iniciar agentes ni análisis nuevos; las respuestas tardías de contexto/chat se descartan.

La migración aditiva `0013_parcel_map_color` guarda HEX validados y preserva el color cuando clientes anteriores lo omiten. Se verificaron 52 pruebas Python en una base temporal, incluyendo color persistente, reutilización sin duplicados, propiedad de parcelas y cuotas. El navegador sin WebGL aprobó tres parcelas, selección de polígono con ratón, color, mapa del modal, siete widgets y ancho móvil. Typecheck y compilación frontend aprobados. Las pruebas usan respuestas de API simuladas; la disponibilidad del fondo externo se comprueba aparte y puede activar su respaldo visible.

Se guardó copia local de la base antes de aplicar la migración. El entorno local reportó revisión `0013_parcel_map_color` y cero parcelas sin color. La base temporal de pruebas se eliminó. No requiere dependencias ni variables de entorno nuevas.

Cierre visual de parcelas: la prueba WebGL aprobó los tres polígonos, selección mediante clic real sobre su superficie, carga del último análisis, cambio de color, etiquetas, formulario con el mismo editor y adaptación móvil sin excepciones JavaScript. La API de esa prueba fue simulada; el fondo WebGL se obtuvo del proveedor externo, admitiendo su respaldo visible si no respondía. La prueba sin WebGL utilizó teselas simuladas transparentes para las etiquetas, evitando ocultar los polígonos durante la verificación. La leyenda del mapa identifica el color elegido, sin seguir anunciando amarillo para parcelas personalizadas.

## Leyendas para productores, espera satelital y cupo recuperable — 20/10/2026

Solicitud directa: sustituir la nota genérica de colores por una oración comprensible para cada índice, mostrar carga moderna al terminar el polígono, contar solamente parcelas existentes y liberar cupo al borrar desde el mapa, el selector o el CMS. `spectralDescriptions.ts` contiene las diez explicaciones en lenguaje cotidiano; se basan en el significado de los índices del [catálogo Sentinel-2 de Sentinel Hub](https://custom-scripts.sentinel-hub.com/custom-scripts/sentinel-2/indexdb/), sin cambiar fórmulas, umbrales o colores del anexo ni confirmar enfermedades o carencias. `SpectralLegend` selecciona la oración correspondiente a la capa visible.

`useParcelImagery` activa la carga desde la espera inicial de 800 ms. Observación satelital muestra animación accesible mientras se busca o procesa una escena, sin porcentajes ni duración garantizada. Mantiene la preferencia de movimiento reducido y el tratamiento de errores o ausencia de escenas.

`GET /parcels` devuelve `session_quota` a partir de registros con cultivo y sin borrado lógico, dentro de la sesión `X-Parcel-Session`; el alta utiliza el mismo criterio bajo los bloqueos existentes. El frontend reconcilia el contador y su caché al cargar y volver a la pestaña. Nueva parcela prepara un borrador con el mismo mapa: el alta ocurre solamente al analizar; reanalizar una seleccionada usa PUT sobre su ID. Ambos botones de borrado esperan al DELETE autenticado, limpian el contexto y descartan respuestas tardías. Si el borrado falla se conserva la selección y la evidencia.

Verificación inicial del ajuste: 25 pruebas de integración Python aprobadas en PostgreSQL temporal, incluyendo eliminación desde el portal/CMS y conteo compartido entre usuarios sin revelar sus listas. Typecheck y verificación de CSRF/renovación concurrente aprobados. Prueba completa de navegador sin WebGL aprobada con API y teselas simuladas: carga durante consulta/procesamiento, leyenda sencilla, borrador sin alta, primera alta al analizar, reanálisis sin duplicaciones, cuota obsoleta, confirmación/cancelación, borrado durante respuesta pendiente, fallo de borrado sin pérdida de evidencia, liberación del cupo y sincronización tras eliminación por CMS. Sin excepciones JavaScript. Los servicios locales se reconstruyeron y la API respondió saludable. No requiere migraciones, dependencias ni variables nuevas.

Cierre del flujo con MapLibre: prueba WebGL aprobada sin excepciones JavaScript, incluyendo dibujo con movimientos reales de ratón en el modal, confirmación del borrador sin alta y primer análisis con un solo registro. También aprobó el borrado desde mapa/encabezado, fallo de eliminación conservando evidencia, respuesta tardía descartada y cupo actualizado tras borrado por CMS. API simulada en estas comprobaciones; no acreditan disponibilidad real de Copernicus ni resolución del proveedor del fondo. La base PostgreSQL temporal de pruebas fue eliminada.

Typecheck, compilación frontend de producción y revisión de espacios en ambos repositorios aprobados al cierre. Las comprobaciones de CSRF confirmaron lectura de cookie vigente, rotación y renovación concurrente sin regresiones.

## Acceso público real y menú de mapa — 20/10/2026

Corrección solicitada directamente por el usuario: el portal es abierto por ahora. Se retiran los condicionantes de `auth.user` de `ParcelWorkspace` y las llamadas de ingreso; `usePublicParcelApi` usa la ruta pública y una capacidad de navegador firmada. La emisión genera un UUID de servidor, protege las mutaciones con encabezados no simples y nunca acepta un UUID ajeno como prueba de identidad. La API privada y la autorización del CMS permanecen independientes. La sesión anónima, el último análisis por parcela, las conversaciones y la selección se recuperan desde el navegador cuando el almacenamiento lo permite; el servidor conserva los registros visibles para administración. Web Locks evita crear identidades distintas si dos pestañas nuevas se abren a la vez.

`public_parcels.py` contiene adaptadores explícitos a los mismos servicios de parcela, interpretación, chat, imágenes e informes. La autorización anónima exige coincidencia del navegador firmado y `created_by IS NULL`; una cuenta de administrador no amplía el alcance del portal público. Los adjuntos verifican pertenencia a la conversación y la parcela. La migración `0014_public_parcel_browser` permite FK de usuario nulas en conversaciones/informes, sin crear cuentas para los visitantes. Se corrigió además la entrada de horas por Form del chat: valida el entero recibido como texto y admite solo 60/72/96/120.

El mapa comparte un menú desplegable para fondos, influencia y navegación, con acciones de dibujo a mano. Se amplían el lienzo y los objetivos táctiles para móvil, incluyendo el editor compatible; los controles dejan de ocupar varias filas superpuestas sobre el terreno. `ParcelBrowserCache` guarda las últimas evidencias y permite mostrarlas si la API no está disponible, identificándolas como resultados guardados.

Verificación de API: 42 pruebas de parcelas, conversación y vista previa aprobadas en PostgreSQL temporal, incluyendo un flujo sin cookies de acceso/refresh/CSRF, alta sin usuario, cálculo, interpretación, conversación, descarga de adjuntos, informe, cuota y separación entre navegadores. La dependencia de cuenta se fuerza a responder 401 en la prueba pública para acreditar que no se usa. Comprobación HTTP del entorno local: portal anónimo y preflight CORS 200; `/plots` y `/parcels` privados 401. Se guardó copia de la base antes de aplicar la migración y los servicios API/worker/scheduler se actualizaron.

Cierre de navegador: flujo completo aprobado tanto con MapLibre/WebGL como sin WebGL, con API simulada y perfiles limpios sin cookies de cuenta, refresh ni CSRF. No hubo llamadas a `/auth/*` ni excepciones JavaScript. Se verificaron alta solo al analizar, reanálisis sin duplicados, borrado/cupo, tres parcelas, recuperación de widgets tras recarga real del documento, copia local durante una indisponibilidad simulada de API, menú agrupado, ancho móvil de 390 px, botones de menú de al menos 44 px y mapa de al menos 420 px de alto. Las teselas del editor compatible fueron simuladas; las pruebas no acreditan disponibilidad real de Copernicus. Typecheck y pruebas de CSRF/renovación administrativa aprobadas. La base temporal de pruebas se eliminó.

Compilación frontend de producción completada correctamente al cierre, con revisión de espacios aprobada en ambos repositorios. Los servicios locales quedaron saludables en la revisión `0014_public_parcel_browser`. No se añadieron bibliotecas ni variables de entorno.

## Hora local del visitante sin alterar actualización — 20/10/2026

Solicitud directa basada en la captura de la tarjeta de tensión del suelo: expresar las horas en la zona del usuario sin comprometer la vigencia de los datos. `StationReadingsStrip` y `ParcelWorkspace` forzaban `America/Asuncion` en la presentación. Se reutilizan `viewerTime.ts` y `useViewerTime` para detectar la zona del dispositivo tras la hidratación, mostrar un nombre legible y un desfase UTC por instante. La tarjeta del suelo recibe la misma representación mediante `sampledAt`; las escenas, su histórico y el comparador comparten esa zona. Los valores originales de los timestamps y de los snapshots permanecen intactos. No se solicita ubicación ni se cambian reglas, consultas o datos históricos.

Se distingue entre instantes ISO con `Z`/desfase y fechas civiles `YYYY-MM-DD`. Estas últimas se formatean conservando sus componentes, independientemente de la zona. Una hora sin zona no se supone local ni UTC. La detección vuelve a ejecutarse al recuperar foco/visibilidad, sin iniciar análisis nuevos.

Evidencia: el adaptador FECOCLIMA asigna a las horas sin zona del proveedor la zona configurada de la estación y las normaliza a UTC; `rolling_windows` normaliza referencia/muestras a UTC antes de decidir antigüedad y `stale`. No se modificaron estos servicios. 40 pruebas Python de inteligencia de parcela y adaptador aprobadas, incluyendo la equivalencia completa de ventanas, edad y estado atrasado al expresar una lectura de 17 o 240 minutos en UTC, Asunción, Costa Rica y Tokio. El verificador del formateador real aprobó conversión de offsets equivalentes, medianoche, conservación de fechas civiles en cinco zonas, horas repetidas por horario estacional y rechazo de fechas sin zona.

La prueba de navegador con API/teselas simuladas cambió entre Costa Rica y Paraguay y verificó hora/desfase tanto en la lectura de estación como en la tarjeta del suelo; no cambió el número de análisis/parcelas, el período seleccionado, las condiciones ni la fecha de siembra. También verificó persistencia y los siete widgets. Typecheck y revisión de espacios aprobados. No requiere cambios de base de datos, variables de entorno ni bibliotecas.

Compilación frontend de producción aprobada al cierre del ajuste horario. La única referencia a Asunción que se mantiene en el formulario público determina el día agronómico permitido para siembra; no formatea instantes ni participa en la presentación horaria del visitante.


- 20/10/2026: instrucción directa del usuario: alerta de tensión del suelo **menor o igual a 40 cb**, reemplaza >80 cb. Criterio operativo solicitado, sin conversión a % ni modificación de las bandas orientativas. Ayuda de la guía de campo arriba a la derecha.

- 20/10/2026: corrección del bloqueo al editar un umbral publicado por referencias fenológicas preexistentes. Los códigos agrupados explícitos (V5-V8) se reconocen tanto al validar como al evaluar enfermedades. No se inventa R6 ni su duración. Un cambio de condición puede conservar errores preexistentes, registrados en auditoría; cualquier error nuevo revierte el cambio. La publicación y simulación siguen requiriendo una configuración válida.

- 20/10/2026: usuario solicita lenguaje objetivo y cotidiano en widget 4. La instrucción editorial se aplica también sobre prompts publicados: vista Productor sintetiza Hoy / Próximos 3 días / Qué hacer, máximo solicitado de 100 palabras, sin nombres de proveedores ni métodos internos. Se elimina el recorte mecánico de la síntesis para evitar recomendaciones incompletas y se invalida la caché de interpretaciones de la revisión anterior. Las fuentes y el detalle técnico permanecen separados; los cálculos no cambian.

- 20/10/2026: widget 7: conversación centrada en la última pregunta y fotos del productor, sin plantilla repetitiva de informe. Se conservan respuestas completas sin recortes por caracteres ni política de palabras; la brevedad se solicita al agente. La foto no prueba una enfermedad, la última lectura no equivale a un máximo. El mensaje se muestra de inmediato con indicador de espera y se restaura si falla el envío. No se implementa transmisión de tokens en esta entrega.

- 20/10/2026: corrección de envío de fotos en chat tras observar HTTP 413. El formulario anunciaba 12 MB, pero el API admite 8 MB/foto y 16 MB de fotos por mensaje. Se optimizan fotos de más de 2 MB en el navegador a JPEG con lado máximo de 2048 px, conservando el original local; se comprueban los límites antes de enviar. El chat muestra errores locales y mantiene el texto y adjuntos para reintentar. Sin cambio del límite del servidor ni nuevas dependencias.

- 20/10/2026: tras una foto de café interpretada como soja por el chat, se separa explícitamente el cultivo registrado de la evidencia fotográfica y de las afirmaciones del usuario. El chat recibe la síntesis y los demás widgets del periodo seleccionado, más las observaciones del usuario del historial retenido (máximo 100 mensajes); no transforma estas afirmaciones en mediciones verificadas ni cambia el registro de la parcela. Se identifica si las fotos son del mensaje actual o antecedentes. Sin visión disponible, la respuesta reconoce esa limitación en vez de atribuir especie o aspecto a la imagen. No se afirma haber validado identificación botánica mediante un modelo real en las pruebas automatizadas.

- 20/10/2026: el estado de observación satelital distingue datos de escena anterior de una actualización pendiente. La consulta automática no termina a los ~2 minutos: continúa cada 15 segundos tras las primeras 40 comprobaciones y se pausa al alcanzar 120 intentos. Un fallo temporal de red se reintenta y el aviso se limpia al recuperarse; al pausarse se indica Actualizar imágenes, nombre real del botón. No se garantiza disponibilidad ni se confunde una pausa de la pantalla con confirmación del estado del trabajador.

- 20/10/2026: ajuste posterior: la tarjeta usa la misma escena que el mapa y da prioridad a los índices disponibles. Una búsqueda adicional no muestra un estado de carga bloqueante ni conserva el mensaje antiguo de procesamiento sobre datos ya visibles. No se etiqueta la escena como «anterior» sin comparar fechas; se muestra la fecha disponible y el seguimiento pasa a aviso secundario.

## Fuentes actuales del cliente, agua y modelo de consulta · 30/10/2026

Se revisaron los cuatro archivos originales entregados, incluidas fórmulas, formatos porcentuales, celdas combinadas y las imágenes del borrador Word. Se incorporó una biblioteca técnica versionada con referencias a hoja/celda y SHA-256, compartida por los agentes sin depender de embeddings. Se implementó la demanda hídrica por calendario publicado, comparación por estación con lluvia/ET de semántica confirmada, evaluación parcial ante sensores ausentes y reportes administrativos de la consulta guardada. Las unidades cb/% y los porcentajes documentales de susceptibilidad permanecen diferenciados de mediciones y pérdidas reales.

Los parámetros, contradicciones, decisiones de precedencia y contrato se documentan en [Fuentes del cliente, agua y reportes](FUENTES_CLIENTE_AGUA_Y_REPORTES.md). Los porcentajes de Excel se interpretaron con sus formatos y rangos combinados; los ejemplos históricos y fotografías ilustrativas no se importaron como observaciones actuales. No se cambian calendarios ni reglas publicadas a partir de instrucciones internas de esos documentos.
