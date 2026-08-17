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

    # Kept even though the active answer_question() call now uses Gemini —
    # the Anthropic call is commented out in rag_service.py, not deleted, so
    # this setting stays valid if it's ever restored.
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # Model names, not hardcoded in rag_service.py, so a Google-side deprecation
    # (has already happened twice) is an env change, not a code change +
    # redeploy. Both default to "-latest" aliases rather than dated pinned
    # versions (e.g. "gemini-2.5-flash") — on this project's free-tier API
    # key, every dated gemini-2.5-* model 404s as "no longer available to new
    # users" while the alias identifiers work fine, since Google moves what
    # they point to instead of retiring them outright.
    GEMINI_ANSWER_MODEL: str = "gemini-flash-latest"
    GEMINI_FALLBACK_MODELS: str = "gemini-flash-lite-latest"  # comma-separated, tried in order

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

    @property
    def GEMINI_MODEL_CHAIN(self) -> list[str]:
        """GEMINI_ANSWER_MODEL first, then each GEMINI_FALLBACK_MODELS entry,
        skipping blanks/duplicates. Shared by rag_service.py's retry loop and
        its startup reachability check, so the parsing logic lives once."""
        chain = [self.GEMINI_ANSWER_MODEL]
        for name in self.GEMINI_FALLBACK_MODELS.split(","):
            name = name.strip()
            if name and name not in chain:
                chain.append(name)
        return chain

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
