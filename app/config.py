from pydantic import model_validator
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
    ALLOWED_MIME_TYPES: str = (
        "application/pdf,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "text/plain,"
        "text/markdown"
    )

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

    RAG_CHUNK_SIZE: int = 500
    RAG_CHUNK_OVERLAP: int = 50
    RAG_SEARCH_TOP_K: int = 5

    # Background-job recovery: how long a document may sit in "processing"
    # before a startup sweep assumes the worker died and reschedules it.
    INDEXING_STALE_MINUTES: int = 15

    # FAQ entries (question + answer pairs added directly, no file involved).
    FAQ_QUESTION_MAX_LEN: int = 500
    FAQ_ANSWER_MAX_LEN: int = 5000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    @model_validator(mode="after")
    def _validate_chunk_overlap(self) -> "Settings":
        if self.RAG_CHUNK_OVERLAP >= self.RAG_CHUNK_SIZE:
            raise ValueError(
                f"RAG_CHUNK_OVERLAP ({self.RAG_CHUNK_OVERLAP}) must be smaller than "
                f"RAG_CHUNK_SIZE ({self.RAG_CHUNK_SIZE}); otherwise chunking degrades "
                "to near-1-token steps."
            )
        return self

settings = Settings()
