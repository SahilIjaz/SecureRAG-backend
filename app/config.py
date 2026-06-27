from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    APP_NAME: str = "SecureRAG++"
    DEBUG: bool = False

    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    EMAILS_FROM_EMAIL: str = ""
    EMAILS_FROM_NAME: str = "SecureRAG++"

    BREVO_API_KEY: str = ""

    GOOGLE_CLIENT_ID: str = ""

    OTP_EXPIRE_MINUTES: int = 10

    FRONTEND_URL: str = "http://localhost:5173"

    UPLOAD_DIR: str = "storage/uploads"
    MAX_UPLOAD_SIZE_MB: int = 50
    ALLOWED_MIME_TYPES: str = "application/pdf"

    CRAWL4AI_ENABLED: bool = True
    CRAWL4AI_TIMEOUT: int = 30
    CRAWL4AI_MAX_CONTENT_SIZE_MB: int = 50

    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    FILE_ENCRYPTION_KEY: str = ""

    PINECONE_API_KEY: str = ""
    PINECONE_INDEX_NAME: str = "securerag-documents"

    ANTHROPIC_API_KEY: str = ""

    RAG_CHUNK_SIZE: int = 500      RAG_CHUNK_OVERLAP: int = 50      RAG_SEARCH_TOP_K: int = 5
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

settings = Settings()
