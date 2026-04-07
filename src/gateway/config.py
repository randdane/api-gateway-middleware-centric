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

    # Rate limiting defaults (requests per minute)
    rate_limit_user_rpm: int = 600
    rate_limit_vendor_rpm: int = 1000

    # Observability
    otel_endpoint: str | None = None
    otel_service_name: str = "api-gateway"
    metrics_enabled: bool = True


settings = Settings()
