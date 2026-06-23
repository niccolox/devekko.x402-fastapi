import sentry_sdk
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi_x402 import init_x402
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.core.config import settings


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
)

# Set all CORS enabled origins
if settings.all_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.all_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Let browser clients read the x402 settlement receipt.
        expose_headers=["X-PAYMENT-RESPONSE"],
    )

# Enable x402 payments. The @pay decorator on routes only gates requests once
# init_x402 has installed the middleware, so leaving this off makes paid routes
# behave as ordinary routes.
if settings.X402_ENABLED:
    init_x402(
        app,
        pay_to=settings.X402_PAY_TO,
        network=settings.X402_NETWORK,
        facilitator_url=settings.X402_FACILITATOR_URL,
    )

app.include_router(api_router, prefix=settings.API_V1_STR)
