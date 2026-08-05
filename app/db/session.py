from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped session that cleans up its own authorization cache.

    The `finally` is the safety net described in `app/core/authz.py`: whatever
    this request wrote to `users` or `helpers`, the matching cached `Principal`
    is dropped on the way out. Without it, forgetting one `invalidate_principal`
    call in a new endpoint leaves a suspended account working for five minutes.
    """
    # Imported here rather than at module scope: authz imports the models, which
    # import db.base, and keeping this local sidesteps any future import cycle.
    from app.core.authz import flush_principal_invalidations

    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await flush_principal_invalidations(session)
