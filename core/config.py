from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Pydantic Settings config to read from .env file
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Google Cloud / Gemini
    GOOGLE_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    # Default so a missing env var can never crash startup (the Cloud Run
    # build trigger does not reliably inject this). Override via env when needed.
    GOOGLE_CLOUD_PROJECT: str = "eagle-eye-496806"
    GOOGLE_CLOUD_LOCATION: str = "us-central1"
    GEMINI_MODEL: str = "gemini-3.5-flash"

    # MongoDB
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "eagleeyes"

    # Arize Phoenix
    PHOENIX_API_KEY: Optional[str] = None
    PHOENIX_COLLECTOR_ENDPOINT: str = "https://app.phoenix.arize.com"
    PHOENIX_PROJECT_NAME: str = "eagleeyes-aml"
    PHOENIX_LOCAL_PORT: int = 6090

    # App Settings
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    SELF_IMPROVE_AFTER_N_TRANSACTIONS: int = 500


# Export singleton instance
settings = Settings()

# Propagate keys to environment variables for underlying SDKs (like google-genai / ADK)
import os
api_key = settings.GEMINI_API_KEY or settings.GOOGLE_API_KEY
if api_key:
    os.environ["GEMINI_API_KEY"] = api_key
    # Remove GOOGLE_API_KEY to avoid dual-key SDK warnings
    os.environ.pop("GOOGLE_API_KEY", None)
    # Force Developer API (AI Studio) mode. Without this, the stray
    # GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION values can push google-genai
    # into Vertex mode, where (with no project/location in the process env) it
    # builds a malformed host like "-aiplatform.googleapis.com" -> getaddrinfo
    # failed. Pinning this to false keeps it on generativelanguage.googleapis.com.
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"