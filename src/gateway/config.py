from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    environment: str = "development"
    log_level: str = "info"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50

    # Auth / JWT
    jwks_url: str = "http://localhost:8080/.well-known/jwks.json"
    jwks_cache_ttl_seconds: int = 300
    jwt_algorithms: list[str] = ["RS256"]
    jwt_audience: str | None = None
    jwt_issuer: str | None = None

    # Portal integration — opaque token validation
    # NOTE: http://localhost is for local development only. When REQUIRE_HTTPS
    # is True (the default outside development), the gateway refuses to start
    # unless PORTAL_URL uses https://, preventing cleartext transmission of
    # HMAC'd token-validation requests in production.
    portal_url: str = "http://localhost:8001"
    portal_shared_secret: str = "dev-shared-secret-change-me-in-production"
    portal_token_cache_ttl: int = 60
    require_https: bool = True

    # Rate limiting defaults (requests per minute)
    rate_limit_user_rpm: int = 600
    rate_limit_vendor_rpm: int = 1000

    @model_validator(mode="after")
    def _enforce_https_in_production(self) -> "Settings":
        if self.require_https and not self.portal_url.startswith("https://"):
            raise ValueError(
                "REQUIRE_HTTPS is enabled but PORTAL_URL is not HTTPS. "
                "Set REQUIRE_HTTPS=false only for local development on loopback."
            )
        if not self.require_https and self.environment != "development":
            raise ValueError(
                "REQUIRE_HTTPS=false is only permitted when ENVIRONMENT=development. "
                "All non-development environments must use HTTPS."
            )
        return self

    # Observability
    otel_endpoint: str | None = None
    otel_service_name: str = "api-gateway"
    metrics_enabled: bool = True


settings = Settings()
