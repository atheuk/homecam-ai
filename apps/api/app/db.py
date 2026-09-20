from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from .config import settings
from .models.db import Base
engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
async def init_db():
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
async def get_db():
    async with SessionLocal() as session: yield session
