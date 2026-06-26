from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    APP_NAME: str = "SecureRAG++"
    DEBUG: bool = False

    # Email / SMTP (legacy, kept for local dev)
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    EMAILS_FROM_EMAIL: str = ""
    EMAILS_FROM_NAME: str = "SecureRAG++"

    # Brevo (used in production — HTTP API, works on Render)
    BREVO_API_KEY: str = ""

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""

    # OTP
    OTP_EXPIRE_MINUTES: int = 10

    # Frontend URL (for CORS)
    FRONTEND_URL: str = "http://localhost:5173"

    # File storage (local fallback)
    UPLOAD_DIR: str = "storage/uploads"
    MAX_UPLOAD_SIZE_MB: int = 50
    ALLOWED_MIME_TYPES: str = "application/pdf"

    # Web scraping (Crawl4AI)
    CRAWL4AI_ENABLED: bool = True
    CRAWL4AI_TIMEOUT: int = 30
    CRAWL4AI_MAX_CONTENT_SIZE_MB: int = 50

    # Cloudinary
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # File Encryption (at rest)
    FILE_ENCRYPTION_KEY: str = ""

    # Vector Database (Pinecone) for RAG
    PINECONE_API_KEY: str = ""
    PINECONE_INDEX_NAME: str = "securerag-documents"

    # Anthropic API for embeddings
    ANTHROPIC_API_KEY: str = ""

    # RAG Chunking settings
    RAG_CHUNK_SIZE: int = 500  # tokens
    RAG_CHUNK_OVERLAP: int = 50  # tokens
    RAG_SEARCH_TOP_K: int = 5  # top results for Q&A

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )


settings = Settings()
