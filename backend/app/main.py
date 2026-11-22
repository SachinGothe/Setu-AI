from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from urllib.parse import urlsplit, urlunsplit
from app.api.v1.router import router
from app.core.config import get_settings
from app.core.database import initialize_database


settings = get_settings()
allowed_origins = [settings.frontend_origin]
if settings.app_env.lower() != "production":
    configured = urlsplit(settings.frontend_origin)
    loopback_aliases = {"localhost": "127.0.0.1", "127.0.0.1": "localhost"}
    alias = loopback_aliases.get(configured.hostname or "")
    if alias:
        netloc = f"{alias}:{configured.port}" if configured.port else alias
        allowed_origins.append(urlunsplit((configured.scheme, netloc, configured.path, "", "")))

app = FastAPI(
    title="CivicPilot AI API",
    version="0.1.0",
    description="Independent public-service information and application-preparation assistant.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

@app.on_event("startup")
def startup() -> None:
    initialize_database()
