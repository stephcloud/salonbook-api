import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import auth, bookings, payments, salons, services, stylists
from app.core.config import settings
from app.db.session import engine
from app.jobs import expire_pending, retry_refunds


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # Housekeeping loops. POST /bookings stays correct without the first; a refund is
    # still attempted right after the webhook or cancel without the second, which only
    # retries what that missed.
    tasks = [
        asyncio.create_task(expire_pending.run_forever()),
        asyncio.create_task(retry_refunds.run_forever()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()


app = FastAPI(title="SalonBook API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(bookings.router, prefix="/api/v1")
app.include_router(payments.router, prefix="/api/v1")
app.include_router(salons.router, prefix="/api/v1")
app.include_router(services.router, prefix="/api/v1")
app.include_router(stylists.router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
