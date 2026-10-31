# Mapa abierto y compatibilidad sin WebGL

El visor público `/fecoclima-ia/mapa`, el mapa de parcelas y los editores administrativos usan Leaflet 1.9.4. El dibujo y la edición usan Leaflet.Draw 1.0.4 sobre SVG y teselas PNG/JPEG. No necesitan un contexto WebGL. Los polígonos existentes siguen siendo GeoJSON; las geometrías MultiPolygon se editan como varios polígonos y se guardan nuevamente como MultiPolygon. Cambiar el fondo conserva la geometría y la imagen Copernicus seleccionada.

## Fuentes y condiciones

- Fondo satelital: EOxCloudless **2016/2017**, CC BY 4.0. Es un fondo histórico para navegación; el zoom adicional amplía sus píxeles, no agrega resolución. Las imágenes recientes y los índices de cada parcela siguen en «Mi parcela». La excepción de licencia libre para la edición 2016 está documentada en https://cloudless.eox.at/license-non-commercial; las ediciones posteriores tienen condiciones distintas y no se incorporan.
- Calles y nombres: OpenStreetMap y la superposición EOX, con sus atribuciones visibles. https://maps.eox.at/ y https://www.openstreetmap.org/copyright. Se respeta el uso de teselas bajo demanda, sin descargas masivas ni precarga: https://operations.osmfoundation.org/policies/tiles/.
- Relieve: Mapzen Terrain Tiles (Terrarium), publicado en el registro de datos abiertos AWS: https://registry.opendata.aws/terrain-tiles/. Fórmula de elevación: `R × 256 + G + B / 256 − 32768`, en metros. https://github.com/tilezen/joerd/blob/master/docs/formats.md. La atribución enlaza a los proveedores originales: https://github.com/tilezen/joerd/blob/master/docs/attribution.md. Pendiente y orientación se derivan del modelo de elevación; la resolución efectiva depende de la fuente local y del zoom. No es un levantamiento de la finca.
- Ríos principales: Natural Earth, dominio público, escala 1:10 millones. https://www.naturalearthdata.com/about/terms-of-use/. La capa regional se recorta al área visible; no pretende representar todos los arroyos ni sirve como delimitación predial.

No se incorporan Esri, MapTiler ni claves o suscripciones cartográficas de pago. Los servidores públicos ofrecen disponibilidad según sus condiciones; las fuentes de relieve y ríos se almacenan en la caché del backend para reducir consultas. No se agregan servicios contratados, aunque el procesamiento y almacenamiento utilizan los recursos existentes del servidor.

## Analizar las últimas 72 horas

Abrir el mapa carga ubicaciones y las parcelas **del navegador actual**, sin sincronizar proveedores. «Analizar» crea un trabajo `REGIONAL_MAP_ANALYSIS` en el flujo de trabajos existente. Consulta todas las estaciones activas, con hasta tres sincronizaciones simultáneas, respetando el intervalo de actualización del proveedor. Los fallos individuales no impiden preparar el resto del mapa. Se captura una ventana móvil exacta de 72 horas al terminar las consultas. La captura se conserva al recargar el navegador y solo se actualiza al volver a analizar.

Cada sensor se evalúa por separado: mínimo 80% de registros esperados en la ventana, valores finitos dentro del rango y estación con lectura reciente. Un sensor ausente no invalida otras variables. Las medias de temperatura, humedad y viento representan esa ventana; **no son normales climatológicas**. Un valor cero es válido y diferente de un sensor ausente. El suelo en `cb` y en `%` son capas distintas, usando las mismas unidades efectivas del análisis de parcelas.

Lluvia y ET requieren una modalidad confirmada por estación. Los intervalos se suman; los contadores diarios usan sus diferencias y reinicios según la zona horaria de la estación. Al comenzar la ventana a mitad del día se resta la lectura de base. El acumulado termina en la última lectura válida del sensor, sin extrapolar hasta la hora del servidor; una cola mayor al intervalo habitual se identifica como parcial. Un contador inconsistente o una historia incompleta no se convierte en cero. Si falta la base inicial pero existe una lectura válida dentro de tres intervalos de muestreo, se utiliza esa primera lectura como base y se devuelve únicamente el aumento observado con estado `PARTIAL_BOUNDARY`; el tramo inicial queda sin estimar. Si no hay una base suficientemente cercana, el valor queda vacío. La resolución temporal de las mediciones limita la precisión del acumulado, especialmente alrededor del reinicio de medianoche. Si la fracción inicial antes del reinicio es menor a un intervalo y queda entre lecturas válidas, se conserva el acumulado observado con estado `PARTIAL_BOUNDARY`; no se atribuye lluvia a esos minutos sin medición. La ficha de estación explica la limitación.

Las superficies de color tienen dos áreas de comparación. «Tendencia entre estaciones» utiliza IDW continuo con todas las estaciones válidas, sin cambios bruscos por selección de vecinos. La envolvente métrica redondeada se extiende 40 km en el borde y desaparece suavemente; la distancia a la estación más cercana reduce gradualmente la opacidad (escala de 85 km), manteniendo casi transparentes los grandes vacíos de la red. No hay cortes a 100 km ni normalización por tesela. Cada PNG se calcula a 256 px, sin ampliar bloques de 128 px. «Solo áreas de influencia» conserva la comparación limitada a los polígonos del CMS. El selector muestra exclusivamente variables con al menos tres estaciones válidas. Transparencia no significa cero: representa menor soporte espacial. La capa es una estimación, no una medición local ni un downscaling calibrado.

La escala por defecto se ajusta al mínimo y máximo observados de cada variable en la ventana. Se publican los extremos y su diferencia, y se muestran los valores de cada estación sobre el mapa y en una lista ordenada. Para no amplificar ruido, hay un ancho mínimo de escala por variable (por ejemplo, 1 °C en temperatura y 5 mm en lluvia); los valores iguales no reciben colores distintos. Desactivar «Resaltar diferencias» recupera la escala amplia de referencia. Los colores relativos de ventanas distintas pueden representar valores diferentes; la leyenda siempre muestra los valores actuales. Cada variable tiene su propia paleta de tres colores saturados, sin blancos ni colores pálidos, compartida por leyenda, teselas y etiquetas: temperatura azul–amarillo–rojo, humedad verde–turquesa–azul, lluvia cian–azul–violeta, temperatura del suelo amarillo–naranja, humedad de suelo ocre–verde, tensión verde–rojo, viento morado–amarillo y ET amarillo–naranja–magenta. Tras analizar se omiten las estaciones sin lectura para la variable seleccionada, incluyendo sus etiquetas e influencias; el cero sigue siendo una lectura válida.

La corrección opcional de temperatura por altitud usa −0,65 °C/100 m donde hay altitud de estación: es experimental y no constituye un downscaling validado. No se calcula pérdida de rendimiento a partir de estas capas. El fondo satelital histórico sirve para navegación visual; las escenas, índices y evidencias del análisis agronómico siguen usando el flujo Copernicus existente, sin atribuir al agente análisis de una imagen que no recibió.

Las referencias documentales de los widgets se muestran únicamente en la vista «Técnico». Las atribuciones cartográficas requeridas por las licencias permanecen visibles en ambos modos. La estación y su porcentaje de cobertura aparecen junto al selector de parcela.


## Contrato local

Todas las rutas están bajo `/api/v1/public/map`:

| Ruta | Uso |
| --- | --- |
| `POST /analyses` | Solicita el trabajo; devuelve `job_id` y `status`. Requiere la capacidad firmada del navegador y `X-Parcel-Session`. Reutiliza su trabajo pendiente. |
| `GET /analyses/{job_id}` | Consulta estado y captura terminada; requiere el mismo navegador. |
| `GET /analyses/{job_id}/tiles/{variable}/{z}/{x}/{y}.png` | Tesela de estimación. Solo observaciones públicas de estaciones; nunca parcelas ni identidad. `?altitude=true` solo afecta temperatura. `domain=regional|influence` selecciona el área; `scale=relative|absolute` selecciona la escala. Las teselas v7 se almacenan separadas de las anteriores; el cliente renueva su URL para descartar colores antiguos. |
| `GET /terrain/{kind}/{z}/{x}/{y}.png` | `hillshade`, `altitude`, `slope` o `aspect`. |
| `GET /rivers?west=...&south=...&east=...&north=...` | Ríos recortados a una vista de hasta 15° por eje. |

Los errores de fuentes abiertas devuelven 503 con un mensaje público; las teselas fuera de rango devuelven 404. El mapa sigue disponible si una capa falla. No se exponen diagnósticos internos del proveedor.

## Verificación

- `tests/test_regional_map.py`: contadores a través de medianoche, base inicial, suma de intervalos, nulos, cero, datos antiguos, unidades, ausencia de extrapolación, corrección opcional, elevación y PNG transparentes; simulación de indisponibilidad de fuentes.
- `tests/test_regional_map_integration.py`: PostgreSQL desechable, sesión firmada, aislamiento entre navegadores, deduplicación del trabajo y teselas públicas sin parcelas.
- `CHROME_NO_SANDBOX=1 MAP_TEST_DISABLE_WEBGL=1 node scripts/map-smoke.mjs`: dibujo y movimiento real de vértices, cambio de fondo, persistencia, borrado confirmado y móvil en Chrome sin WebGL.
- `CHROME_NO_SANDBOX=1 MAP_TEST_DISABLE_WEBGL=1 node scripts/regional-map-smoke.mjs`: flujo del nuevo mapa con API simulada, capas, sensores ausentes, caída del relieve y restauración sin sincronizar nuevamente.


## Consulta compacta en Mi parcela

En `/fecoclima-ia`, «Vista del mapa → Qué mostrar → Consultar todas las estaciones» ejecuta el mismo análisis regional de las últimas 72 horas. Activa una vista de estaciones en el mapa existente: sustituye los iconos por valores de la variable, muestra la superficie de colores y una escala con valores debajo del selector. Omite estaciones sin lectura y oculta temporalmente los controles habituales y la imagen de parcela. No incorpora controles de fondo, relieve o ríos del visor regional. «Salir vista estaciones» retira el efecto y devuelve iconos, controles e imagen seleccionada. Se conservan el polígono, la imagen elegida y los controles actuales, sin activar el análisis agronómico de la parcela. No se consulta a los proveedores hasta pulsar el botón. El selector conserva el mínimo de tres estaciones válidas y las paletas compartidas; la transparencia depende de la distancia a la red.

Al recargar `/fecoclima-ia`, el mapa abre en satélite y ajusta una sola vez las ubicaciones válidas de todas las estaciones al zoom más cercano que permite verlas completas, con margen para los marcadores. Espera la llegada de las estaciones si la carga es asíncrona; las parcelas guardadas y los polígonos de influencia no amplían ese encuadre inicial. La navegación posterior permanece libre.
