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
    # Workspace role — the frontend uses this to route agents to the inbox and
    # hide owner-only navigation. Defaults to owner for the single-user case.
    role: Literal["owner", "admin", "agent"] = "owner"

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
    idToken: str = Field(..., max_length=8192, description="Google ID token (credential) from Google Identity Services")

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
    currentPassword: str = Field(..., max_length=128)
    newPassword: str = Field(..., min_length=8, max_length=128)

# ── Onboarding ────────────────────────────────────────────────────────────────

class FEOnboardingStep1(BaseModel):
    businessCategory: str = Field(..., max_length=255)
    teamSize: str = Field(..., max_length=50)

class FEOnboardingStep2(BaseModel):
    workspaceName: str = Field(..., min_length=2, max_length=100)

class FEOnboardingStep3(BaseModel):
    plan: FEPlanId

class FEOnboardingStep4(BaseModel):
    hasDocuments: bool

class FECompleteOnboardingRequest(BaseModel):
    businessCategory: str = Field(..., max_length=255)
    teamSize: str = Field(..., max_length=50)
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
    businessCategory: str = Field(..., max_length=255)
    teamSize: str = Field(..., max_length=50)

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
    # Aggregate storage (MB). storageTotalMb is -1 when the plan is unlimited.
    storageUsedMb: float = 0.0
    storageTotalMb: float = 0.0
    resetsOn: str

# ── Chatbot config ────────────────────────────────────────────────────────────

class FEChatbotIdentity(BaseModel):
    # These strings are interpolated into the LLM prompt, so they are bounded
    # to keep the prompt from being blown out with attacker-controlled text.
    name: str = Field(..., max_length=100)
    avatarUrl: Optional[str] = Field(None, max_length=2048)
    welcomeMessage: str = Field(..., max_length=2000)
    persona: str = Field(..., max_length=50)
    language: str = Field(..., max_length=20)
    fallbackMessage: str = Field(..., max_length=2000)

class FEChatbotBehavior(BaseModel):
    handoffToHuman: bool
    confidenceThreshold: int = Field(..., ge=0, le=100)
    collectEmailBeforeChat: bool
    collectNameBeforeChat: bool = False
    collectPhoneBeforeChat: bool = False
    showSources: bool
    stayOnTopic: bool
    tone: str = Field(..., max_length=50)
    maxResponseLength: Literal["short", "medium", "long"]

class FEChatbotAppearance(BaseModel):
    accentColor: str = Field(..., max_length=32)
    bubblePosition: Literal["bottom-right", "bottom-left"]
    showPoweredBy: bool
    widgetTheme: Literal["light", "dark", "auto"]
    fontSize: Literal["small", "medium", "large"]

class FEChatbotDeploy(BaseModel):
    status: Literal["live", "draft"]
    deployedDomain: str = Field(..., max_length=255)
    allowedDomains: str = Field(..., max_length=4096)
    botSlug: str = Field(..., max_length=120)
    # apiKey is server-generated; a client-supplied value is ignored on save
    # (see chatbot.save_config). Bounded here only as defence in depth.
    apiKey: str = Field(..., max_length=128)

class FEMenuItem(BaseModel):
    name: str = Field(..., max_length=120)
    description: str = Field("", max_length=1000)
    price: str = Field("", max_length=40)

class FEMenuCategory(BaseModel):
    label: str = Field(..., max_length=120)
    items: List[FEMenuItem] = Field(default_factory=list, max_length=100)

class FEChatbotConfig(BaseModel):
    identity: FEChatbotIdentity
    behavior: FEChatbotBehavior
    appearance: FEChatbotAppearance
    deploy: FEChatbotDeploy
    # Optional quick-reply menu (e.g. a restaurant's categories → items). The
    # widget renders these as tappable chips; a tap sends the item as a normal
    # message. Empty/absent means no menu. Gated by the plan's `menu` feature.
    menu: List[FEMenuCategory] = Field(default_factory=list, max_length=50)

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
    # Which team member is handling this chat (multi-agent queue). null = in the
    # shared queue / unassigned.
    assignedUserId: Optional[str] = None
    assignedToName: Optional[str] = None

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
    text: str = Field(..., min_length=1, max_length=8000)

class FESendReplyResponse(BaseModel):
    message: FEConversationMessage

class FEPurgeMessagesResponse(BaseModel):
    purged: int

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
    # source_url column is String(2048); cap here so an over-long URL is a 422
    # rather than a DB DataError. Scheme/SSRF validation happens in the handler.
    url: str = Field(..., min_length=1, max_length=2048)

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
    name: str = Field(..., max_length=255)
    urlSlug: str = Field(..., max_length=255)
    industry: str = Field(..., max_length=255)

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

class FEChangePlanRequest(BaseModel):
    planId: FEPlanId

class FECheckoutRequest(BaseModel):
    planId: FEPlanId

class FECheckoutResponse(BaseModel):
    clientSecret: str

class FESetupIntentResponse(BaseModel):
    clientSecret: str

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

# ── Team / RBAC ───────────────────────────────────────────────────────────────

class FEMember(BaseModel):
    id: str
    name: str
    email: str
    role: Literal["owner", "admin", "agent"]
    online: bool = False

class FEPendingInvite(BaseModel):
    id: str
    email: str
    role: Literal["owner", "admin", "agent"]
    invitedLabel: str

class FETeamRoster(BaseModel):
    members: List[FEMember]
    pendingInvites: List[FEPendingInvite]
    agentsUsed: int
    agentsLimit: int

class FEInviteRequest(BaseModel):
    email: EmailStr

class FEAcceptInviteRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=256)
    name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)

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
