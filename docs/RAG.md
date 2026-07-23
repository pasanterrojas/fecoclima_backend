# Base de conocimiento RAG

- Admite PDF, DOCX y TXT.
- Conserva documento, página/sección, cultivo, institución, año y versión.
- Divide el texto en fragmentos con solapamiento.
- Puede generar embeddings mediante un proveedor compatible configurado.
- Guarda vectores en pgvector.
- Si no hay proveedor de embeddings, utiliza recuperación léxica de respaldo para no bloquear el sistema.
- Solo recupera documentos activos del cultivo seleccionado.
- El agente debe devolver las fuentes utilizadas en su esquema JSON.
