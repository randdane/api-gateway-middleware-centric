"""Seed script — inserts example vendors directly into the database.

Usage:
    DATABASE_URL=postgresql+asyncpg://gateway:gateway@localhost:5432/gateway \
        python scripts/seed_vendors.py
"""

import asyncio
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Ensure src/ is on the path when run from the project root.
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.db.models import Vendor  # noqa: E402

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway",
)

VENDORS = [
    {
        "name": "Open-Meteo",
        "slug": "open-meteo",
        "base_url": "https://api.open-meteo.com",
        "auth_type": "none",
        "auth_config": {},
        "cache_ttl_seconds": 600,   # weather data is stable for 10 minutes
        "rate_limit_rpm": 600,
        "is_active": True,
    },
]


async def seed() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        for data in VENDORS:
            existing = await db.execute(
                select(Vendor).where(Vendor.slug == data["slug"])
            )
            if existing.scalar_one_or_none() is not None:
                print(f"  skip  {data['slug']} (already exists)")
                continue

            vendor = Vendor(**data)
            db.add(vendor)
            await db.commit()
            await db.refresh(vendor)
            print(f"  added {data['slug']} (id={vendor.id})")

    await engine.dispose()


if __name__ == "__main__":
    print("Seeding vendors...")
    asyncio.run(seed())
    print("Done.")
