"""Private, deterministic fingerprint of CMS configuration; never prints secrets.

Run with --output before a deployment and --verify after restoring its database.
The database dump and persistent files remain the authoritative backup.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import MetaData, Table, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

TABLES = (
    "stations", "station_integrations", "crops", "phenology_stages", "crop_rule_sets",
    "crop_rule_versions", "base_conditions", "disease_rules", "ai_agents", "ai_agent_versions",
    "knowledge_documents", "agent_knowledge_documents", "integration_configs",
)
# Operational timestamps change while workers run; they are not CMS configuration.
VOLATILE = {"created_at", "updated_at", "last_synced_at", "last_error", "last_sync_at", "last_sync_status", "last_sync_error",
            "last_run_at", "last_result", "index_status", "indexed_at", "index_error"}
DEPLOYMENT_SETTINGS = {"environment", "database_url", "cookie_secure", "cookie_domain", "cors_origins",
                       "trusted_hosts", "frontend_base_url", "log_level", "storage_path"}


def serialize(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"Unsupported configuration value: {type(value).__name__}")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=serialize,
                                     separators=(",", ":")).encode()).hexdigest()


async def capture():
    engine = create_async_engine(settings.database_url, isolation_level="REPEATABLE READ")
    rows, fingerprints = {}, {}
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                for name in TABLES:
                    table = await connection.run_sync(
                        lambda conn, name=name: Table(name, MetaData(), autoload_with=conn))
                    columns = [c for c in table.columns if c.name not in VOLATILE]
                    records = [dict(r) for r in (await connection.execute(select(*columns))).mappings()]
                    records.sort(key=lambda row: str(row.get("id", digest(row))))
                    rows[name] = records
                    fingerprints[name] = {"count": len(records), "sha256": digest(records)}
        behavior = {key: value for key, value in settings.model_dump(mode="json").items()
                    if key not in DEPLOYMENT_SETTINGS
                    and not any(word in key for word in ("password", "secret", "key", "token"))}
        fingerprints["behavior_settings"] = {"sha256": digest(behavior)}
        return {"format": 1, "captured_at": datetime.now(UTC).isoformat(), "fingerprints": fingerprints,
                "behavior_settings": behavior, "configuration": rows}
    finally:
        await engine.dispose()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--verify", type=Path)
    args = parser.parse_args()
    state = await capture()
    if args.verify:
        expected = json.loads(args.verify.read_text())
        differences = [name for name in set(expected["fingerprints"]) | set(state["fingerprints"])
                       if expected["fingerprints"].get(name) != state["fingerprints"].get(name)]
        if differences:
            raise SystemExit("Configuration differs: " + ", ".join(sorted(differences)))
        print("Configuration verified: CMS prompts, rules, documents, stations and behavior settings match.")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output, ensure_ascii=False, indent=2, default=serialize)
            output.write("\n")
        print("Private configuration snapshot created.")


if __name__ == "__main__":
    asyncio.run(main())
