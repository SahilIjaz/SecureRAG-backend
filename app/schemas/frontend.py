"""
Wire-format schemas for the Nexus dashboard frontend.

These mirror Nexus-frontend/src/types/api.types.ts + entities.types.ts EXACTLY
(field names are camelCase, enum values match the display strings the UI
renders). Keep the two files in sync — the frontend is the source of truth.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import settings

FEPlanId = Literal["starter", "growth", "business"]

# ── Auth ──────────────────────────────────────────────────────────────────────

class FEUser(BaseModel):
    id: str
    email: EmailStr
    companyName: str
    onboardingCompleted: Optional[bool] = None

class FESignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    companyName: str = Field(..., min_length=1, max_length=255)

class FESignupResponse(BaseModel):
    success: bool
    message: str

class FELoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)

class FELoginResponse(BaseModel):
    success: bool
    token: str
    user: FEUser

class FEGoogleLoginRequest(BaseModel):
    idToken: str = Field(..., description="Google ID token (credential) from Google Identity Services")

class FEOtpVerifyRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)

class FEOtpVerifyResponse(BaseModel):
    success: bool
    token: Optional[str] = None
    user: Optional[FEUser] = None

class FEForgotPasswordRequest(BaseModel):
    email: EmailStr

class FEResendOtpRequest(BaseModel):
    email: EmailStr

class FEResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)
    newPassword: str = Field(..., min_length=8, max_length=128)

class FESuccessResponse(BaseModel):
    success: bool = True

class FEChangePasswordRequest(BaseModel):
    currentPassword: str
    newPassword: str = Field(..., min_length=8, max_length=128)

# ── Onboarding ────────────────────────────────────────────────────────────────

class FEOnboardingStep1(BaseModel):
    businessCategory: str
    teamSize: str

class FEOnboardingStep2(BaseModel):
    workspaceName: str = Field(..., min_length=2, max_length=100)

class FEOnboardingStep3(BaseModel):
    plan: FEPlanId

class FEOnboardingStep4(BaseModel):
    hasDocuments: bool

class FECompleteOnboardingRequest(BaseModel):
    businessCategory: str
    teamSize: str
    workspaceName: str = Field(..., min_length=2, max_length=100)
    plan: FEPlanId
    hasDocuments: bool
    uploadedFiles: Optional[int] = 0
    uploadedUrls: Optional[int] = 0

class FEOnboardingSummary(BaseModel):
    businessCategory: str
    teamSize: str
    workspaceName: str
    plan: FEPlanId
    hasDocuments: bool
    uploadedFiles: int
    uploadedUrls: int
    completedAt: str

class FECompleteOnboardingResponse(BaseModel):
    success: bool
    summary: FEOnboardingSummary

class FEUploadDocumentsResponse(BaseModel):
    success: bool
    uploadedFiles: int
    uploadedUrls: int

class FESaveOnboardingDetailsRequest(BaseModel):
    businessCategory: str
    teamSize: str

# ── Dashboard ─────────────────────────────────────────────────────────────────

class FEOverviewStats(BaseModel):
    totalConversations: int
    totalConversationsDelta: float
    resolutionRate: float
    resolutionRateDelta: float
    unresolved: int
    unresolvedDelta: float
    avgResponseSeconds: float
    avgResponseDelta: float

class FEVolumePoint(BaseModel):
    date: str
    count: int

class FESentimentData(BaseModel):
    positive: int
    neutral: int
    negative: int
    csatScore: float

class FEUnresolvedQuestion(BaseModel):
    id: str
    question: str
    askedCount: int
    reason: str
    lastAsked: str

class FEKnowledgeGap(BaseModel):
    id: str
    topic: str
    queryCount: int
    priority: Literal["High", "Medium", "Low"]

class FEConversationTopic(BaseModel):
    label: str
    percentage: int

class FERecentConversation(BaseModel):
    id: str
    initials: str
    name: str
    preview: str
    timeAgo: str
    status: Literal["Resolved", "Open", "Handed off"]
    sentiment: Literal["Positive", "Neutral", "Negative"]

class FEPlanUsage(BaseModel):
    plan: str
    messagesUsed: int
    messagesTotal: int
    docsUsed: int
    docsTotal: int
    urlsUsed: int
    urlsTotal: int
    resetsOn: str

# ── Chatbot config ────────────────────────────────────────────────────────────

class FEChatbotIdentity(BaseModel):
    name: str
    avatarUrl: Optional[str] = None
    welcomeMessage: str
    persona: str
    language: str
    fallbackMessage: str

class FEChatbotBehavior(BaseModel):
    handoffToHuman: bool
    confidenceThreshold: int = Field(..., ge=0, le=100)
    collectEmailBeforeChat: bool
    collectNameBeforeChat: bool = False
    collectPhoneBeforeChat: bool = False
    showSources: bool
    stayOnTopic: bool
    tone: str
    maxResponseLength: Literal["short", "medium", "long"]

class FEChatbotAppearance(BaseModel):
    accentColor: str
    bubblePosition: Literal["bottom-right", "bottom-left"]
    showPoweredBy: bool
    widgetTheme: Literal["light", "dark", "auto"]
    fontSize: Literal["small", "medium", "large"]

class FEChatbotDeploy(BaseModel):
    status: Literal["live", "draft"]
    deployedDomain: str
    allowedDomains: str
    botSlug: str
    apiKey: str

class FEChatbotConfig(BaseModel):
    identity: FEChatbotIdentity
    behavior: FEChatbotBehavior
    appearance: FEChatbotAppearance
    deploy: FEChatbotDeploy

class FESaveChatbotConfigResponse(BaseModel):
    success: bool
    config: FEChatbotConfig

class FERegenerateApiKeyResponse(BaseModel):
    apiKey: str

# ── Conversations ─────────────────────────────────────────────────────────────

class CitationSource(BaseModel):
    """One retrieved chunk a bot reply cites — used by both the Test Chatbot
    response and persisted conversation messages."""
    documentId: Optional[str] = None
    documentName: Optional[str] = None
    snippet: str
    page: Optional[int] = None
    score: float

class FEConversationListItem(BaseModel):
    id: str
    name: str
    initials: str
    preview: str
    timeAgo: str
    createdAt: str
    status: Literal["Open", "Handed off", "Resolved"]
    sentiment: Literal["Positive", "Neutral", "Negative"]
    channel: Literal["Widget", "API", "Internal Test"]
    isLive: bool = False
    visitorEmail: Optional[str] = None
    visitorPhone: Optional[str] = None
    # Whether the visitor's widget still appears to have this chat open
    # right now (see helpers.is_visitor_still_present) — separate from
    # isLive, which only reflects whether an owner formally joined.
    visitorPresent: bool = False

class FEConversationMessage(BaseModel):
    id: str
    role: Literal["user", "agent", "bot"]
    text: str
    # ISO-8601 with tz offset (UTC) — the frontend renders this in the
    # viewer's own local time zone rather than trusting a server-formatted
    # clock string, which is what caused dashboard message times to show the
    # server's UTC hour instead of the owner's local hour.
    createdAt: str
    sources: List[CitationSource] = []

class FEConversationDetail(FEConversationListItem):
    messages: List[FEConversationMessage]

class FESendReplyRequest(BaseModel):
    text: str = Field(..., min_length=1)

class FESendReplyResponse(BaseModel):
    message: FEConversationMessage

class FEPurgeMessagesResponse(BaseModel):
    purged: int

class FEConversationLiveSummary(BaseModel):
    """GET /api/conversations/live-summary — lets the dashboard show how many
    conversations are simultaneously live/waiting without opening each one
    (sidebar badge, Conversations list summary strip). See
    NexusContext/LIVE_AGENT_HANDOFF_PLAN.md §7."""
    liveCount: int
    waitingCount: int

# ── Knowledge base ────────────────────────────────────────────────────────────

class FEKnowledgeDocument(BaseModel):
    id: str
    name: str
    status: Literal["Indexed", "Processing", "Failed"]
    chunks: Optional[int] = None
    sizeLabel: str
    addedLabel: str

class FEKnowledgeUrl(BaseModel):
    id: str
    url: str
    status: Literal["Indexed", "Processing", "Failed"]
    chunks: Optional[int] = None
    addedLabel: str

class FEKnowledgeStats(BaseModel):
    documentsUsed: int
    documentsLimit: int
    urlsUsed: int
    urlsLimit: int
    totalChunks: int
    chunksLimit: int

class FEAddUrlRequest(BaseModel):
    url: str

class FEAddUrlResponse(BaseModel):
    url: FEKnowledgeUrl

class FEFaqEntry(BaseModel):
    id: str
    question: str
    answer: str
    status: Literal["Indexed", "Processing", "Failed"]
    chunks: Optional[int] = None
    addedLabel: str

def _non_blank(v: str, field_name: str) -> str:
    # Pydantic's plain min_length counts raw characters, so "   " passes it —
    # strip first, then check, so whitespace-only input is actually rejected.
    v = v.strip()
    if not v:
        raise ValueError(f"{field_name} cannot be empty.")
    return v

class FEAddFaqRequest(BaseModel):
    question: str = Field(..., max_length=settings.FAQ_QUESTION_MAX_LEN)
    answer: str = Field(..., max_length=settings.FAQ_ANSWER_MAX_LEN)

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, v: str) -> str:
        return _non_blank(v, "question")

    @field_validator("answer")
    @classmethod
    def _answer_not_blank(cls, v: str) -> str:
        return _non_blank(v, "answer")

class FEAddFaqResponse(BaseModel):
    faq: FEFaqEntry

class FEUpdateFaqRequest(BaseModel):
    question: str = Field(..., max_length=settings.FAQ_QUESTION_MAX_LEN)
    answer: str = Field(..., max_length=settings.FAQ_ANSWER_MAX_LEN)

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, v: str) -> str:
        return _non_blank(v, "question")

    @field_validator("answer")
    @classmethod
    def _answer_not_blank(cls, v: str) -> str:
        return _non_blank(v, "answer")

# ── Settings ──────────────────────────────────────────────────────────────────

class FEWorkspaceSettings(BaseModel):
    name: str
    urlSlug: str
    industry: str

class FESaveWorkspaceSettingsResponse(BaseModel):
    success: bool
    settings: FEWorkspaceSettings

class FENotificationSettings(BaseModel):
    unresolvedConversations: bool
    weeklySummary: bool
    knowledgeGapsDetected: bool
    planUsageWarnings: bool
    productUpdates: bool

class FENotificationSettingsPatch(BaseModel):
    unresolvedConversations: Optional[bool] = None
    weeklySummary: Optional[bool] = None
    knowledgeGapsDetected: Optional[bool] = None
    planUsageWarnings: Optional[bool] = None
    productUpdates: Optional[bool] = None

class FEPaymentMethod(BaseModel):
    brand: str
    last4: str
    expiry: str

class FEBillingInfo(BaseModel):
    planId: FEPlanId
    status: Literal["Active", "Trialing", "Past Due", "Canceled"]
    priceLabel: str
    renewsOn: str
    paymentMethod: FEPaymentMethod
    trialEndsOn: Optional[str] = None
    # Prepaid wallet — separate concept from the plan above (plan quota
    # covers document/storage limits; the wallet covers real LLM $ cost).
    # See NexusContext plan doc i-want-to-implement-floofy-hickey.md
    # section C. trialMessagesRemaining/trialDaysRemaining are None once
    # the trial has ended (see Tenant.trial_ended_at) — at that point every
    # provider, including the default, draws from walletBalanceUsd.
    walletBalanceUsd: float = 0.0
    trialMessagesRemaining: Optional[int] = None
    trialDaysRemaining: Optional[int] = None

class FEChangePlanRequest(BaseModel):
    planId: FEPlanId

class FECheckoutRequest(BaseModel):
    planId: FEPlanId

class FECheckoutResponse(BaseModel):
    clientSecret: str

class FESetupIntentResponse(BaseModel):
    clientSecret: str

class FEWalletTopupRequest(BaseModel):
    amountUsd: float = Field(..., ge=0.50, le=1000)

class FEWalletTopupResponse(BaseModel):
    checkoutUrl: str

class FEWalletTransaction(BaseModel):
    id: str
    type: Literal["topup", "deduction", "refund"]
    amountUsd: float
    balanceAfterUsd: Optional[float] = None
    createdAt: str

class FEWalletTransactionsResponse(BaseModel):
    transactions: List[FEWalletTransaction]
    total: int
    page: int
    pageSize: int

class FEApiKey(BaseModel):
    id: str
    label: str
    maskedKey: str
    createdLabel: str

class FECreateApiKeyRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)

class FECreateApiKeyResponse(BaseModel):
    key: FEApiKey
    rawKey: str

# ── Profile ───────────────────────────────────────────────────────────────────

class FEUserProfile(BaseModel):
    name: str
    email: EmailStr
    companyName: str
    avatarUrl: Optional[str] = None
    role: str

class FESaveProfileRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    companyName: str = Field(..., min_length=1, max_length=255)
    avatarUrl: Optional[str] = None

class FESaveProfileResponse(BaseModel):
    success: bool
    profile: FEUserProfile

# ── Notifications (in-app bell) ─────────────────────────────────────────────────

class FENotification(BaseModel):
    id: str
    type: str
    title: str
    body: Optional[str] = None
    link: Optional[str] = None
    isRead: bool
    createdAt: str
    timeAgo: str

class FENotificationListResponse(BaseModel):
    notifications: List[FENotification]
    unreadCount: int
