| Test ID | Scenario | Request | HTTP | Expected | Result |
|---|---|---|---|---|---|
| TC_AUTH_001 | Valid user registration | POST /api/auth/signup | 201 | 201, OTP initiated | PASS |
| TC_AUTH_002 | Existing email registration | POST /api/auth/signup (same email) | 201 | No second account created (response is enumeration-safe) | PASS |
| TC_AUTH_002b | Registration with an already VERIFIED email | POST /api/auth/signup (verified email) | 409 | No second account (4xx or enumeration-safe 2xx) | PASS |
| TC_AUTH_003 | Invalid email format | POST /api/auth/signup | 422 | 422 | PASS |
| TC_AUTH_004 | Invalid password | POST /api/auth/signup | 422 | 422 | PASS |
| TC_AUTH_005 | Valid OTP verification | POST /api/auth/verify-otp | 200 | 200, email verified | PASS |
| TC_AUTH_006 | Invalid OTP verification | POST /api/auth/verify-otp | 400 | 400 rejected | PASS |
| TC_AUTH_007 | Expired OTP verification | POST /api/auth/verify-otp | 400 | 400 rejected | PASS |
| TC_AUTH_008 | Valid login | POST /api/auth/login | 200 | 200 + token | PASS |
| TC_AUTH_009 | Incorrect password | POST /api/auth/login | 401 | 401 | PASS |
| TC_AUTH_010 | Unregistered email | POST /api/auth/login | 401 | 401 | PASS |
| TC_AUTH_012 | Password reset request | POST /api/auth/forgot-password | 200 | 200 | PASS |
| TC_AUTH_013 | Password reset then login with new password | POST /api/auth/login | 200 | login 200 | PASS |
| TC_AUTH_013a | Password reset completion | POST /api/auth/reset-password | 200 | 200 | PASS |
| TC_AUTH_014 | Protected API without JWT | GET /api/user/me | 401 | 401/403 | PASS |
| TC_AUTH_015 | Invalid JWT | GET /api/user/me | 401 | 401 | PASS |
| TC_AUTH_015a | Valid JWT accepted (control) | GET /api/user/me | 200 | 200 | PASS |
| TC_AUTH_016 | Logout invalidates the token | POST /api/auth/logout, GET /api/user/me | 401 | logout 200 then /me 401 | PASS |
| TC_BILL_001 | Trial / subscription status for a new workspace | GET /api/settings/subscription-status | 200 | 200, usable | PASS |
| TC_BILL_002 | View subscription details | GET /api/settings/billing | 200 | 200 with plan info | PASS |
| TC_BILL_007 | View workspace credit balance | GET /api/settings/billing | 200 | wallet balance present in billing info | PASS |
| TC_BILL_011 | View billing / usage history | GET /api/settings/billing/wallet/transactions | 200 | 200 list | PASS |
| TC_DOC_001 | Valid PDF upload | POST /api/knowledge/documents | 201 | 201 accepted | PASS |
| TC_DOC_002 | Valid DOCX upload | POST /api/knowledge/documents | 201 | 201 accepted | PASS |
| TC_DOC_003 | Valid TXT upload | POST /api/knowledge/documents | 201 | 201 accepted | PASS |
| TC_DOC_004 | Valid Markdown upload | POST /api/knowledge/documents | 201 | 201 accepted | PASS |
| TC_DOC_005 | Unsupported CSV upload | POST /api/knowledge/documents | 415 | 4xx rejected | PASS |
| TC_DOC_006 | Malformed PDF | POST /api/knowledge/documents, GET /api/knowledge/documents (poll) | 422 | Rejected at upload or ends in a failed state (never Indexed) | PASS |
| TC_DOC_007 | Fake PDF (text with .pdf extension) | POST /api/knowledge/documents | 415 | 4xx rejected | PASS |
| TC_DOC_008 | Empty TXT file | POST /api/knowledge/documents | 422 | 4xx rejected | PASS |
| TC_DOC_010 | Text extraction | GET /api/knowledge/documents | 200 | documents processed (ready) | PASS |
| TC_DOC_011 | Chunking and indexing | GET /api/knowledge/documents (poll) | 200 | uploads reach ready status | PASS |
| TC_DOC_013 | Restricted local URL | POST /api/knowledge/urls | 422 | 4xx rejected | PASS |
| TC_DOC_014 | Unsupported URL scheme (ftp) | POST /api/knowledge/urls | 422 | 4xx rejected | PASS |
| TC_DOC_014b | Unsupported URL scheme (javascript) | POST /api/knowledge/urls | 422 | 4xx rejected | PASS |
| TC_DOC_015 | Delete document | DELETE /api/knowledge/documents/{id} | 200 | 200 removed | PASS |
| TC_DOC_016 | View documents | GET /api/knowledge/documents | 200 | 200 list contains uploads | PASS |
| TC_RAG_001 | Document-based question answering | POST /api/chatbot/test-chat | 200 | answer contains the codeword | PASS |
| TC_RAG_002 | Question unrelated to uploaded knowledge | POST /api/chatbot/test-chat | 200 | fallback / no fabricated answer | PASS |
| TC_RAG_009 | Empty chatbot message | POST /api/chatbot/test-chat | 422 | 422 | PASS |
| TC_RAG_010 | Consecutive chatbot messages | POST /api/chatbot/test-chat x2 | 200 | both 200 | PASS |
| TC_RAG_011a | Read chatbot configuration | GET /api/chatbot/config | 200 | 200 | PASS |
| TC_WS_002 | Invalid workspace details (bad onboarding step) | POST /api/onboarding/step/9 | 400 | 400 | PASS |
| TC_WS_003 | Team invitation | POST /api/settings/members/invite | 201 | 201 invite created | PASS |
| TC_WS_005 | Invalid/expired invitation | POST /api/auth/accept-invite | 400 | 4xx rejected | PASS |
| TC_WS_006 | Owner views/manages members | GET /api/settings/members | 200 | 200 roster | PASS |
| TC_WS_007a | Owner role cannot be granted by invite | POST /api/settings/members/invite | 422 | 422 | PASS |