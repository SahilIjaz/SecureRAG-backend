from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, ValidationInfo, field_validator

from app.models.subscription import BillingCycle, PlanName

class SignupStep1Request(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=255, examples=["Jane Doe"])
    email: EmailStr = Field(..., examples=["jane@example.com"])
    password: str = Field(..., min_length=8, max_length=128, examples=["S3cur3P@ss!"])

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("full_name must not be blank")
        return v.strip()

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        errors = []
        if len(v) < 8:
            errors.append("at least 8 characters")
        if not any(c.isupper() for c in v):
            errors.append("at least one uppercase letter")
        if not any(c.islower() for c in v):
            errors.append("at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            errors.append("at least one digit")
        if errors:
            raise ValueError("Password must contain " + ", ".join(errors))
        return v

class OTPVerifyRequest(BaseModel):
    email: EmailStr = Field(..., examples=["jane@example.com"])
    otp_code: str = Field(..., min_length=4, max_length=4, examples=["3821"])

    @field_validator("otp_code")
    @classmethod
    def otp_must_be_digits(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("OTP must contain only digits")
        return v

class ResendOTPRequest(BaseModel):
    email: EmailStr = Field(..., examples=["jane@example.com"])

VALID_EMPLOYEE_RANGES = {"1-15", "16-49", "50-199", "200-1999", "2000-4999", "just-me"}
VALID_BUSINESS_CATEGORIES = {
    "Healthcare",
    "Finance & Banking",
    "Government & NGO",
    "Education",
    "Technology",
    "Retail",
    "Other",
}

class OrganizationInfoRequest(BaseModel):
    business_category: str = Field(..., examples=["Healthcare"])
    employee_count_range: str = Field(..., examples=["1-15"])

    @field_validator("business_category")
    @classmethod
    def valid_category(cls, v: str) -> str:
        if v not in VALID_BUSINESS_CATEGORIES:
            raise ValueError(f"business_category must be one of: {', '.join(sorted(VALID_BUSINESS_CATEGORIES))}")
        return v

    @field_validator("employee_count_range")
    @classmethod
    def valid_range(cls, v: str) -> str:
        if v not in VALID_EMPLOYEE_RANGES:
            raise ValueError(f"employee_count_range must be one of: {', '.join(sorted(VALID_EMPLOYEE_RANGES))}")
        return v

class WorkspaceSetupRequest(BaseModel):
    workspace_name: str = Field(..., min_length=2, max_length=100, examples=["Acme Corp"])

    @field_validator("workspace_name")
    @classmethod
    def workspace_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("workspace_name must not be blank")
        return v.strip()

class PlanSelectionRequest(BaseModel):
    plan_name: PlanName = Field(..., examples=["free"])
    billing_cycle: Optional[BillingCycle] = Field(None, examples=["monthly"])

class GoogleLoginRequest(BaseModel):
    id_token: str = Field(..., description="Google ID token from the frontend")

class SocialLoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    is_new_user: bool = False
    onboarding_token: Optional[str] = None

class SigninRequest(BaseModel):
    email: EmailStr = Field(..., examples=["jane@example.com"])
    password: str = Field(..., min_length=1, examples=["S3cur3P@ss!"])

class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., examples=["eyJhbGciOi..."])

class ForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(..., examples=["jane@example.com"])

class VerifyResetOTPRequest(BaseModel):
    email: EmailStr = Field(..., examples=["jane@example.com"])
    otp_code: str = Field(..., min_length=4, max_length=4, examples=["8472"])

    @field_validator("otp_code")
    @classmethod
    def otp_digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("OTP must contain only digits")
        return v

class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)
    confirm_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        errors = []
        if len(v) < 8:
            errors.append("at least 8 characters")
        if not any(c.isupper() for c in v):
            errors.append("at least one uppercase letter")
        if not any(c.islower() for c in v):
            errors.append("at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            errors.append("at least one digit")
        if errors:
            raise ValueError("Password must contain " + ", ".join(errors))
        return v

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v: str, info: ValidationInfo) -> str:
        if "new_password" in info.data and v != info.data["new_password"]:
            raise ValueError("Passwords do not match")
        return v

class MessageResponse(BaseModel):
    message: str
    email: Optional[str] = None

class OTPVerifyResponse(BaseModel):
    message: str
    email: str
    onboarding_token: str

class VerifyResetOTPResponse(BaseModel):
    message: str
    reset_token: str

class OrganizationInfoResponse(BaseModel):
    message: str
    email: str
    full_name: str
    business_category: str
    employee_count_range: str

class TokenResponse(BaseModel):
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Literal["bearer"] = "bearer"
    onboarding_token: Optional[str] = None
    needs_onboarding: Optional[bool] = None

class OnboardingCompleteResponse(TokenResponse):
    message: str
    workspace_name: Optional[str] = None
    slug: Optional[str] = None
