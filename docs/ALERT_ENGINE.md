# Motor determinístico de alertas

La IA no calcula el color ni modifica el resultado.

Flujo:

1. Lecturas normalizadas y deduplicadas.
2. Validación de rango y unidad.
3. Agregación diaria configurable.
4. Evaluación de seis condiciones base.
5. Cálculo de completitud y confianza.
6. Nivel: `NINGUNA`, `MODERADA`, `GRAVE` o `DATOS_INSUFICIENTES`.
7. Cruce con etapa fenológica y reglas de enfermedad publicadas.
8. Persistencia del detalle por condición y versión de regla.
9. Entrega del JSON estructurado al agente explicativo.

Por defecto, la falta de una variable obligatoria produce `DATOS_INSUFICIENTES`. El sensor de hoja requiere una configuración explícita por estación para convertir la señal en horas de mojado foliar.

Los umbrales ambiguos encontrados en las fuentes, por ejemplo “humedad elevada” o “lluvias persistentes”, se cargan como pendientes y no se publican automáticamente.
