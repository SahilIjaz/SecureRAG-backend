"""
Frontend-compat notifications endpoints (/api/notifications/...) — the
dashboard bell's backend.

  - GET    /api/notifications          -> {notifications, unreadCount}
  - POST   /api/notifications/{id}/read -> {success}
  - POST   /api/notifications/read-all  -> {success}
  - DELETE /api/notifications/{id}      -> {success}
  - DELETE /api/notifications           -> {success}  (clear all)
"""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.database import get_db
from app.models.notification import Notification
from app.models.user import User
from app.schemas.frontend import FENotification, FENotificationListResponse, FESuccessResponse
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/notifications", tags=["Frontend — Notifications"])

_LIST_LIMIT = 50

def _fe_notification(n: Notification) -> FENotification:
    return FENotification(
        id=str(n.id),
        type=n.type,
        title=n.title,
        body=n.body,
        link=n.link,
        isRead=n.is_read,
        createdAt=helpers._as_aware(n.created_at).isoformat(),
        timeAgo=helpers.time_ago(n.created_at),
    )

@router.get("", response_model=FENotificationListResponse)
async def list_notifications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FENotificationListResponse:
    # A member sees whole-tenant notices (user_id NULL) plus ones addressed to
    # them — never notices meant for another agent.
    visible = or_(Notification.user_id.is_(None), Notification.user_id == current_user.id)
    result = await db.execute(
        select(Notification)
        .where(Notification.tenant_id == current_user.tenant_id, visible)
        .order_by(Notification.created_at.desc())
        .limit(_LIST_LIMIT)
    )
    rows = result.scalars().all()

    unread_result = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.tenant_id == current_user.tenant_id,
            visible,
            Notification.is_read == False,
        )
    )
    unread_count = unread_result.scalar_one()

    return FENotificationListResponse(
        notifications=[_fe_notification(n) for n in rows],
        unreadCount=unread_count,
    )

async def _get_notification_or_404(
    notification_id: str, user: User, db: AsyncSession
) -> Notification:
    try:
        nid = uuid.UUID(notification_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")

    result = await db.execute(
        select(Notification).where(Notification.id == nid, Notification.tenant_id == user.tenant_id)
    )
    notif = result.scalar_one_or_none()
    if notif is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    return notif

@router.post("/read-all", response_model=FESuccessResponse)
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    await db.execute(
        update(Notification)
        .where(
            Notification.tenant_id == current_user.tenant_id,
            or_(Notification.user_id.is_(None), Notification.user_id == current_user.id),
            Notification.is_read == False,
        )
        .values(is_read=True)
    )
    return FESuccessResponse()

@router.post("/{notification_id}/read", response_model=FESuccessResponse)
async def mark_read(
    notification_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    notif = await _get_notification_or_404(notification_id, current_user, db)
    notif.is_read = True
    await db.flush()
    return FESuccessResponse()

@router.delete("", response_model=FESuccessResponse)
async def clear_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    await db.execute(
        delete(Notification).where(
            Notification.tenant_id == current_user.tenant_id,
            or_(Notification.user_id.is_(None), Notification.user_id == current_user.id),
        )
    )
    return FESuccessResponse()

@router.delete("/{notification_id}", response_model=FESuccessResponse)
async def delete_notification(
    notification_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    notif = await _get_notification_or_404(notification_id, current_user, db)
    await db.delete(notif)
    return FESuccessResponse()
