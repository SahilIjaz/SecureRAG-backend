"""
Cloudinary storage client with file encryption.

Files are uploaded as raw resources under the folder:
    securerag/uploads/{tenant_id}/

All files are encrypted at rest before uploading to Cloudinary.

Cloudinary returns a secure_url which is stored as file_path in the DB.
The public_id is also returned so files can be deleted later.
"""

import asyncio
import logging
import os
import uuid
from typing import Tuple

import cloudinary
import cloudinary.uploader
from cryptography.fernet import Fernet

from app.config import settings

logger = logging.getLogger(__name__)

_cipher = None

def _get_cipher() -> Fernet:
    """Get or initialize the Fernet cipher for file encryption."""
    global _cipher
    if _cipher is None:
        if not settings.FILE_ENCRYPTION_KEY:
            raise ValueError("FILE_ENCRYPTION_KEY not set in environment")
        _cipher = Fernet(settings.FILE_ENCRYPTION_KEY.encode())
    return _cipher

def _configure_cloudinary() -> None:
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )

def _upload_blocking(
    file_content: bytes,
    public_id: str,
    content_type: str,
) -> dict:
    """Upload file to Cloudinary (expects encrypted content)."""
    _configure_cloudinary()
    result = cloudinary.uploader.upload(
        file_content,
        public_id=public_id,
        resource_type="raw", overwrite=False,
        use_filename=False,
    )
    return result

def _delete_blocking(public_id: str) -> None:
    _configure_cloudinary()
    cloudinary.uploader.destroy(public_id, resource_type="raw")
    logger.info("Deleted from Cloudinary: %s", public_id)

async def upload_file_to_cloudinary(
    file_content: bytes,
    tenant_id: uuid.UUID,
    original_filename: str,
    content_type: str,
) -> Tuple[str, str]:
    """
    Uploads a file to Cloudinary with encryption at rest.
    Returns (public_id, secure_url).

    public_id → stored as file_path in DB (used for deletion)
    secure_url → the HTTPS URL to access the file

    File is encrypted before upload for security.
    """
    cipher = _get_cipher()
    encrypted_content = cipher.encrypt(file_content)

    ext = os.path.splitext(original_filename)[1].lower()
    unique_name = f"{uuid.uuid4()}{ext}"
    public_id = f"securerag/uploads/{tenant_id}/{unique_name}"

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        _upload_blocking,
        encrypted_content, public_id,
        content_type,
    )

    secure_url = result["secure_url"]
    returned_public_id = result["public_id"]
    logger.info("Uploaded encrypted file to Cloudinary: %s", secure_url)
    return returned_public_id, secure_url

async def decrypt_file(encrypted_content: bytes) -> bytes:
    """
    Decrypt file content retrieved from Cloudinary.

    Args:
        encrypted_content: Encrypted file bytes from Cloudinary

    Returns:
        Decrypted file bytes
    """
    cipher = _get_cipher()
    return cipher.decrypt(encrypted_content)

async def delete_file_from_cloudinary(public_id: str) -> None:
    """Deletes a file from Cloudinary by its public_id."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _delete_blocking, public_id)
