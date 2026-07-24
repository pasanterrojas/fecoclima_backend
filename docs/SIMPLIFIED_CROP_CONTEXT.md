# Contexto agronómico simplificado

## Regla operacional

El portal público no requiere un registro de ciclo productivo. Cada consulta queda definida por:

1. estación meteorológica;
2. cultivo;
3. fecha de siembra;
4. periodo visualizado.

La fecha de siembra es un parámetro de la consulta. No se configuran ventanas de siembra ni campañas permanentes.

## Flujo

```text
estación + cultivo + fecha de siembra
                |
                v
lecturas observadas -> pandas/numpy -> agregados diarios
                |
                v
fecha de análisis - fecha de siembra -> edad y etapa fenológica estimada
                |
                v
seis condiciones publicadas -> NINGUNA / MODERADA / GRAVE / DATOS_INSUFICIENTES
                |
                +--> enfermedades observadas según reglas medibles y etapa
                |
pronóstico Open-Meteo -> riesgo potencial diario + proyección no diagnóstica
                |
                v
agente IA único del cultivo -> explicación y recomendaciones
```

Los widgets, gráficos y colores se alimentan del procesamiento determinístico. El agente no decide el nivel de alerta: interpreta el resultado, la fenología y el pronóstico.

## Operación automática

El scheduler mantiene automáticamente:

- sincronización de estaciones;
- agregados meteorológicos;
- pronóstico por coordenadas;
- cola de reportes y tareas técnicas.

La evaluación específica de cultivo y fenología se calcula al consultar el portal o generar un reporte, porque la fecha de siembra se recibe directamente del usuario y no se mantiene como campaña administrativa.

## Agentes

Cada cultivo tiene un solo agente operativo. Al crear un cultivo desde el administrador se crea automáticamente un agente base con propósito `CROP_ANALYSIS`. El administrador puede editar su prompt, modelo, temperatura, esquema JSON y colección RAG.

## Fenología inicial

Los calendarios iniciales de soja y trigo provienen de las hojas de fenología suministradas. Para cultivos sin calendario publicado se muestra `Configuración pendiente`; no se inventan etapas ni umbrales.

## Predicción y enfermedades

La proyección usa las reglas publicadas derivadas de las hojas Excel. Una enfermedad solamente se muestra cuando todas las variables de su regla medible están disponibles y se cumplen. Las expresiones ambiguas y las variables que el proveedor de pronóstico no entrega se muestran como limitaciones.

El nivel pronosticado es potencial. La alerta oficial sigue exigiendo datos observados completos para las seis condiciones.

## Parcelas

Las parcelas no intervienen en la selección de cultivo ni en la fenología. Son una configuración geográfica opcional, asociada una sola vez a una estación, para información satelital. La misma geometría sirve para cualquier cultivo seleccionado.

## Compatibilidad

Las tablas históricas de ciclos se conservan para no destruir datos anteriores. Sus endpoints de mantenimiento quedan retirados (`410 Gone`) y no participan en el portal, los reportes ni las evaluaciones nuevas.
