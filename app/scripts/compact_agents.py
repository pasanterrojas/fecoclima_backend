"""Archive redundant generated agents, preserving runs, versions and custom agents."""
import argparse
import asyncio
import json
from app.db.session import AsyncSessionLocal
from app.services.agent_constellation import compact_generated_agents
from app.services.audit import write_audit


async def run(apply: bool):
    async with AsyncSessionLocal() as db:
        result = await compact_generated_agents(db)
        if apply:
            await write_audit(db, action="AGENTS_CONSOLIDATE", entity_type="ai_agent", after=result)
            await db.commit()
        else:
            await db.rollback()
        print(json.dumps({"applied":apply,**result},ensure_ascii=False))


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    asyncio.run(run(parser.parse_args().apply))
