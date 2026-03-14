# Import all models here so that Alembic's autogenerate can detect them
# and SQLAlchemy's metadata is fully populated before migrations run.

from app.models.tenant import Tenant
from app.models.user import User
from app.models.email_verification import EmailVerification
from app.models.subscription import Subscription
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount

__all__ = [
    "Tenant",
    "User",
    "EmailVerification",
    "Subscription",
    "TenantQuota",
    "UsageCount",
]
