"""
Refresh-token issuance, rotation, and reuse detection
(app/core/security.py create_refresh_token, app/services/auth_service.py
issue_refresh_token/refresh_access_token/revoke_all_refresh_tokens).

DB-backed: creates a throwaway tenant + user on the configured database and
hard-deletes them at the end (cascade removes their refresh_tokens rows too).
Skips itself when the database is unreachable.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_token_refresh.py -v
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import select, text

from app.core.security import create_refresh_token, decode_token
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User
from app.services import auth_service


async def _fresh_engine():
    """Each pytest-asyncio test function gets its own event loop; dispose the
    engine first so its connection pool doesn't hand back a connection bound
    to a previous (now-closed) loop — see tests/test_audit.py's same helper."""
    from app.database import async_engine
    await async_engine.dispose()


async def _make_tenant_and_user():
    from app.database import AsyncSessionLocal

    await _fresh_engine()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    try:
        async with AsyncSessionLocal() as db:
            db.add(Tenant(id=tenant_id, workspace_name=f"qa-refresh-{tenant_id.hex[:6]}", slug=f"qa-refresh-{tenant_id.hex[:10]}"))
            db.add(User(
                id=user_id, tenant_id=tenant_id, full_name="QA Refresh",
                email=f"qa-refresh-{user_id.hex[:10]}@example.com",
                password_hash=None, is_email_verified=True, is_active=True,
            ))
            await db.commit()
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {e}")
    return tenant_id, user_id


async def _drop_tenant(tenant_id: uuid.UUID) -> None:
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from tenants where id = :id"), {"id": tenant_id})
        await db.commit()


def test_create_refresh_token_embeds_jti_and_type():
    token, jti, expire = create_refresh_token({"sub": "u1"})
    payload = jwt.get_unverified_claims(token)
    assert payload["jti"] == jti
    assert payload["type"] == "refresh"
    assert payload["sub"] == "u1"
    assert decode_token(token)["exp"] == int(expire.timestamp())


@pytest.mark.asyncio
async def test_issue_and_rotate_refresh_token():
    from app.database import AsyncSessionLocal

    tenant_id, user_id = await _make_tenant_and_user()
    try:
        async with AsyncSessionLocal() as db:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            raw_token, _ = await auth_service.issue_refresh_token(user, db)
            await db.commit()

        async with AsyncSessionLocal() as db:
            access_token, new_raw, new_expires = await auth_service.refresh_access_token(raw_token, db)
            await db.commit()
        assert access_token and new_raw and new_raw != raw_token

        old_jti = decode_token(raw_token)["jti"]
        new_jti = decode_token(new_raw)["jti"]
        async with AsyncSessionLocal() as db:
            old_row = (await db.execute(select(RefreshToken).where(RefreshToken.jti == old_jti))).scalar_one()
            new_row = (await db.execute(select(RefreshToken).where(RefreshToken.jti == new_jti))).scalar_one()
            assert old_row.revoked_at is not None
            assert new_row.revoked_at is None
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_reusing_a_rotated_refresh_token_is_rejected_and_revokes_all():
    from app.database import AsyncSessionLocal

    tenant_id, user_id = await _make_tenant_and_user()
    try:
        async with AsyncSessionLocal() as db:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            raw_token, _ = await auth_service.issue_refresh_token(user, db)
            await db.commit()

        # First use rotates it — this succeeds and produces a second, still-valid token.
        async with AsyncSessionLocal() as db:
            _, second_raw, _ = await auth_service.refresh_access_token(raw_token, db)
            await db.commit()

        # Replaying the now-revoked original must fail...
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc:
                await auth_service.refresh_access_token(raw_token, db)
            await db.commit()
        assert exc.value.status_code == 401

        # ...and must have revoked the second (legitimately rotated) token too,
        # as a precaution against a stolen token being replayed.
        second_jti = decode_token(second_raw)["jti"]
        async with AsyncSessionLocal() as db:
            second_row = (await db.execute(select(RefreshToken).where(RefreshToken.jti == second_jti))).scalar_one()
            assert second_row.revoked_at is not None
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_revoke_all_refresh_tokens_marks_every_live_token_revoked():
    from app.database import AsyncSessionLocal

    tenant_id, user_id = await _make_tenant_and_user()
    try:
        async with AsyncSessionLocal() as db:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            await auth_service.issue_refresh_token(user, db)
            await auth_service.issue_refresh_token(user, db)
            await db.commit()

        async with AsyncSessionLocal() as db:
            await auth_service.revoke_all_refresh_tokens(user_id, db)
            await db.commit()

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))).scalars().all()
            assert len(rows) == 2
            assert all(r.revoked_at is not None for r in rows)
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_access_token_cannot_be_used_as_a_refresh_token():
    """Rejected by the `type` claim check alone, before any DB access —
    no database needed for this one."""
    from app.core.security import create_access_token

    access_token = create_access_token({"sub": str(uuid.uuid4())})
    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token(access_token, db=None)
    assert exc.value.status_code == 401
