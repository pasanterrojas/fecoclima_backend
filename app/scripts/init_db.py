from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import AsyncSessionLocal
from app.services.rag import index_document
from app.services.seed import seed_all
from app.db.models import KnowledgeDocument
from sqlalchemy import select


async def main() -> None:
    settings.validate_for_production()
    async with AsyncSessionLocal() as db:
        await seed_all(db)
        pending = (await db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.index_status == "PENDING"))).all()
        for document in pending:
            try:
                await index_document(db, document.id)
            except Exception as exc:
                document.index_status = "FAILED"
                document.index_error = str(exc)[:2000]
                await db.commit()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
