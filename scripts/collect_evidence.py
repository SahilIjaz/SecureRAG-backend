"""Executes the API-testable manual test cases (TC_AUTH / TC_WS / TC_DOC / TC_RAG /
TC_BILL) against a RUNNING backend and records every request and response as
evidence for Chapter 6.

Safe by construction: point it at a server started on a throw-away database
(nexus_test), with email providers blanked so no real e-mail is sent:

    DATABASE_URL=<.../nexus_test> BREVO_API_KEY= SMTP_USERNAME= SMTP_PASSWORD= \
        python -m uvicorn app.main:app --port 8001
    python scripts/collect_evidence.py --base http://localhost:8001 --db <.../nexus_test>

OTP codes are bcrypt-hashed in the database, so the harness overwrites the
stored hash with the hash of a known code (same function the app uses) to drive
the verify / reset flows — the e-mail leg itself is not exercised here and is
evidenced separately (screenshot).

Output: test_results/manual_evidence_<date>.json and .md (one row per case).
"""
import argparse
import asyncio
import io
import json
import sys
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "x" * 40)
from app.core.security import hash_otp  # noqa: E402

KNOWN_OTP = "123456"
RESULTS: list[dict] = []


def clip(x, n=300):
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= n else s[:n] + "…"


def record(tc, scenario, expected, resp, ok, note="", req=""):
    body = None
    if resp is not None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
    RESULTS.append({
        "id": tc, "scenario": scenario, "request": req, "expected": expected,
        "http_status": None if resp is None else resp.status_code,
        "response": clip(body) if body is not None else "", "note": note,
        "status": "PASS" if ok else "FAIL",
    })
    print(f"{'PASS' if ok else 'FAIL'}  {tc}  {scenario}  [{None if resp is None else resp.status_code}] {note}")


class Ctx:
    def __init__(self, base, db):
        self.c = httpx.Client(base_url=base, timeout=90)
        self.db = db

    async def pg(self):
        return await asyncpg.connect(self.db)

    async def set_otp(self, email, purpose, expired=False):
        con = await self.pg()
        uid = await con.fetchval("select id from users where email=$1", email)
        await con.execute("delete from email_verifications where user_id=$1 and purpose=$2::otppurpose", uid, purpose)
        exp = datetime.now(timezone.utc) + (timedelta(minutes=-5) if expired else timedelta(minutes=10))
        await con.execute(
            "insert into email_verifications (id, user_id, otp_code, purpose, expires_at, is_used, attempts) "
            "values ($1,$2,$3,$4::otppurpose,$5,false,0)",
            uuid.uuid4(), uid, hash_otp(KNOWN_OTP), purpose, exp)
        await con.close()


async def seed_trial(x, email):
    """Give the new workspace an active 3-day trial row. In production this row
    is created at the Stripe checkout step of onboarding, which needs a card and
    is therefore evidenced manually (screenshot), not by this script."""
    con = await x.pg()
    tid = await con.fetchval("select tenant_id from users where email=$1", email)
    await con.execute("delete from subscriptions where tenant_id=$1", tid)
    await con.execute(
        "insert into subscriptions (id, tenant_id, plan_name, billing_cycle, status, started_at, expires_at, created_at) "
        "values ($1,$2,'pro'::planname,'monthly'::billingcycle,'trial'::subscriptionstatus, now(), now() + interval '3 days', now())",
        uuid.uuid4(), tid)
    await con.close()


async def count_users(x, email):
    con = await x.pg()
    n = await con.fetchval("select count(*) from users where email=$1", email)
    await con.close()
    return n


def minimal_docx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Refunds are available within 14 days of purchase for the Starter plan.</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def minimal_pdf():
    objs = ["<</Type/Catalog/Pages 2 0 R>>", "<</Type/Pages/Kids[3 0 R]/Count 1>>",
            "<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            None, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    stream = "BT /F1 12 Tf 72 720 Td (Support hours are 9am to 5pm Monday to Friday.) Tj ET"
    objs[3] = f"<</Length {len(stream)}>>\nstream\n{stream}\nendstream"
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer<</Size {len(objs)+1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


async def run(base, db):
    x = Ctx(base, db)
    c = x.c
    tag = uuid.uuid4().hex[:8]
    email, pw = f"qa-evidence-{tag}@example.com", "Str0ng!Passw0rd#1"
    hdr = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731

    # ── Authentication ─────────────────────────────────────────────
    r = c.post("/api/auth/signup", json={"email": email, "password": pw, "companyName": f"QA Evidence {tag}"})
    record("TC_AUTH_001", "Valid user registration", "201, OTP initiated", r, r.status_code == 201, req="POST /api/auth/signup")
    time.sleep(21)
    r = c.post("/api/auth/signup", json={"email": email, "password": pw, "companyName": "Dup"})
    n = await count_users(x, email)
    record("TC_AUTH_002", "Existing email registration", "No second account created (response is enumeration-safe)", r, n == 1,
           note=f"users with this email in DB: {n}", req="POST /api/auth/signup (same email)")
    time.sleep(21)
    r = c.post("/api/auth/signup", json={"email": "not-an-email", "password": pw, "companyName": "X"})
    record("TC_AUTH_003", "Invalid email format", "422", r, r.status_code == 422, req="POST /api/auth/signup")
    r = c.post("/api/auth/signup", json={"email": f"weak-{tag}@example.com", "password": "abc", "companyName": "X"})
    record("TC_AUTH_004", "Invalid password", "422", r, r.status_code == 422, req="POST /api/auth/signup")

    await x.set_otp(email, "email_verification", expired=True)
    r = c.post("/api/auth/verify-otp", json={"email": email, "otp": KNOWN_OTP})
    record("TC_AUTH_007", "Expired OTP verification", "400 rejected", r, r.status_code == 400, req="POST /api/auth/verify-otp")
    await x.set_otp(email, "email_verification")
    r = c.post("/api/auth/verify-otp", json={"email": email, "otp": "000000"})
    record("TC_AUTH_006", "Invalid OTP verification", "400 rejected", r, r.status_code == 400, req="POST /api/auth/verify-otp")
    await x.set_otp(email, "email_verification")
    r = c.post("/api/auth/verify-otp", json={"email": email, "otp": KNOWN_OTP})
    ok = r.status_code == 200 and r.json().get("success") is True
    record("TC_AUTH_005", "Valid OTP verification", "200, email verified", r, ok, req="POST /api/auth/verify-otp")
    token = (r.json() or {}).get("token") if r.status_code == 200 else None

    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    ok = r.status_code == 200 and bool(r.json().get("token"))
    record("TC_AUTH_008", "Valid login", "200 + token", r, ok, req="POST /api/auth/login")
    token = r.json()["token"] if ok else token
    r2 = c.post("/api/auth/login", json={"email": email, "password": "Wrong!Pass123"})
    record("TC_AUTH_009", "Incorrect password", "401", r2, r2.status_code in (400, 401), req="POST /api/auth/login")
    r2 = c.post("/api/auth/login", json={"email": f"nobody-{tag}@example.com", "password": pw})
    record("TC_AUTH_010", "Unregistered email", "401", r2, r2.status_code in (400, 401), req="POST /api/auth/login")

    r = c.post("/api/auth/forgot-password", json={"email": email})
    record("TC_AUTH_012", "Password reset request", "200", r, r.status_code == 200, req="POST /api/auth/forgot-password")
    await x.set_otp(email, "password_reset")
    newpw = "N3w!Passw0rd#Z"
    r = c.post("/api/auth/reset-password", json={"email": email, "otp": KNOWN_OTP, "newPassword": newpw})
    record("TC_AUTH_013a", "Password reset completion", "200", r, r.status_code == 200, req="POST /api/auth/reset-password")
    r2 = c.post("/api/auth/login", json={"email": email, "password": newpw})
    ok = r.status_code == 200 and r2.status_code == 200
    record("TC_AUTH_013", "Password reset then login with new password", "login 200", r2, ok, req="POST /api/auth/login")
    token = r2.json()["token"] if r2.status_code == 200 else token

    r = c.get("/api/user/me")
    record("TC_AUTH_014", "Protected API without JWT", "401/403", r, r.status_code in (401, 403), req="GET /api/user/me")
    r = c.get("/api/user/me", headers=hdr("abc.def.ghi"))
    record("TC_AUTH_015", "Invalid JWT", "401", r, r.status_code in (401, 403), req="GET /api/user/me")
    r = c.get("/api/user/me", headers=hdr(token))
    record("TC_AUTH_015a", "Valid JWT accepted (control)", "200", r, r.status_code == 200, req="GET /api/user/me")
    r = c.post("/api/auth/logout", headers=hdr(token))
    r2 = c.get("/api/user/me", headers=hdr(token))
    record("TC_AUTH_016", "Logout invalidates the token", "logout 200 then /me 401", r2, r.status_code == 200 and r2.status_code in (401, 403), req="POST /api/auth/logout, GET /api/user/me")
    r = c.post("/api/auth/login", json={"email": email, "password": newpw})
    token = r.json()["token"]
    time.sleep(21)
    r = c.post("/api/auth/signup", json={"email": email, "password": pw, "companyName": "Dup2"})
    n = await count_users(x, email)
    record("TC_AUTH_002b", "Registration with an already VERIFIED email", "No second account (4xx or enumeration-safe 2xx)", r, n == 1,
           note=f"users with this email in DB: {n}", req="POST /api/auth/signup (verified email)")

    await seed_trial(x, email)

    # ── Workspace ──────────────────────────────────────────────────
    r = c.get("/api/settings/members", headers=hdr(token))
    record("TC_WS_006", "Owner views/manages members", "200 roster", r, r.status_code == 200, req="GET /api/settings/members")
    r = c.post("/api/settings/members/invite", headers=hdr(token), json={"email": f"invitee-{tag}@example.com", "role": "agent"})
    record("TC_WS_003", "Team invitation", "201 invite created", r, r.status_code == 201, req="POST /api/settings/members/invite")
    r = c.post("/api/settings/members/invite", headers=hdr(token), json={"email": f"x-{tag}@example.com", "role": "owner"})
    record("TC_WS_007a", "Owner role cannot be granted by invite", "422", r, r.status_code == 422, req="POST /api/settings/members/invite")
    r = c.post("/api/auth/accept-invite", json={"token": "bogus-token", "name": "X", "password": pw})
    record("TC_WS_005", "Invalid/expired invitation", "4xx rejected", r, r.status_code in (400, 404, 410, 422), req="POST /api/auth/accept-invite")
    r = c.post("/api/onboarding/step/9", headers=hdr(token), json={})
    record("TC_WS_002", "Invalid workspace details (bad onboarding step)", "400", r, r.status_code == 400, req="POST /api/onboarding/step/9")

    # ── Documents ──────────────────────────────────────────────────
    up = lambda name, data, mime: c.post("/api/knowledge/documents", headers=hdr(token), files={"files": (name, data, mime)})  # noqa: E731
    r = up("data.csv", b"a,b\n1,2\n", "text/csv")
    record("TC_DOC_005", "Unsupported CSV upload", "4xx rejected", r, r.status_code in (400, 413, 415, 422), req="POST /api/knowledge/documents")
    r = up("broken.pdf", b"%PDF-1.4\n\x00\x01garbage-not-a-real-pdf", "application/pdf")
    broken_r = r
    broken_id = r.json()[0]["id"] if r.status_code == 201 else None
    rejected_at_upload = r.status_code in (400, 413, 415, 422)
    r = up("fake.pdf", b"this is plain text pretending to be a pdf", "application/pdf")
    record("TC_DOC_007", "Fake PDF (text with .pdf extension)", "4xx rejected", r, r.status_code in (400, 413, 415, 422), req="POST /api/knowledge/documents")
    r = up("empty.txt", b"", "text/plain")
    record("TC_DOC_008", "Empty TXT file", "4xx rejected", r, r.status_code in (400, 413, 415, 422), req="POST /api/knowledge/documents")
    txt = f"Nexus QA handbook {tag}. The refund window is 14 days. Support hours are 9am to 5pm. The codeword is PELICAN-{tag}."
    r = up(f"handbook-{tag}.txt", txt.encode(), "text/plain")
    ok = r.status_code == 201
    record("TC_DOC_003", "Valid TXT upload", "201 accepted", r, ok, req="POST /api/knowledge/documents")
    doc_ids = [d["id"] for d in r.json()] if ok else []
    r = up(f"notes-{tag}.md", f"# Notes\nThe shipping time is 3 business days. Tag {tag}.".encode(), "text/markdown")
    record("TC_DOC_004", "Valid Markdown upload", "201 accepted", r, r.status_code == 201, req="POST /api/knowledge/documents")
    doc_ids += [d["id"] for d in r.json()] if r.status_code == 201 else []
    r = up(f"policy-{tag}.docx", minimal_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    record("TC_DOC_002", "Valid DOCX upload", "201 accepted", r, r.status_code == 201, req="POST /api/knowledge/documents")
    doc_ids += [d["id"] for d in r.json()] if r.status_code == 201 else []
    r = up(f"hours-{tag}.pdf", minimal_pdf(), "application/pdf")
    record("TC_DOC_001", "Valid PDF upload", "201 accepted", r, r.status_code == 201, req="POST /api/knowledge/documents")
    doc_ids += [d["id"] for d in r.json()] if r.status_code == 201 else []

    r = c.post("/api/knowledge/urls", headers=hdr(token), json={"url": "http://localhost:8000/health"})
    record("TC_DOC_013", "Restricted local URL", "4xx rejected", r, r.status_code in (400, 413, 415, 422), req="POST /api/knowledge/urls")
    for scheme, tcid in (("ftp://example.com/x", "TC_DOC_014"), ("javascript:alert(1)", "TC_DOC_014b")):
        r = c.post("/api/knowledge/urls", headers=hdr(token), json={"url": scheme})
        record(tcid, f"Unsupported URL scheme ({scheme.split(':')[0]})", "4xx rejected", r, r.status_code in (400, 413, 415, 422), req="POST /api/knowledge/urls")

    # TC_DOC_006: the corrupted PDF passes the upload check but must fail processing
    broken_status = None
    for _ in range(40):
        lst = c.get("/api/knowledge/documents", headers=hdr(token)).json()
        broken_status = next((d.get("status") for d in lst if d["id"] == broken_id), None)
        if broken_status not in ("Processing", None):
            break
        time.sleep(3)
    record("TC_DOC_006", "Malformed PDF", "Rejected at upload or ends in a failed state (never Indexed)", broken_r,
           rejected_at_upload or (broken_status is not None and broken_status != "Indexed"),
           note=f"final status: {broken_status}", req="POST /api/knowledge/documents, GET /api/knowledge/documents (poll)")

    # wait for indexing
    ready = False
    for _ in range(40):
        r = c.get("/api/knowledge/documents", headers=hdr(token))
        docs = r.json() if r.status_code == 200 else []
        mine = [d for d in docs if d["id"] in doc_ids]
        if mine and all(str(d.get("status", "")).lower() in ("ready", "indexed", "processed") for d in mine):
            ready = True
            break
        time.sleep(3)
    r = c.get("/api/knowledge/documents", headers=hdr(token))
    record("TC_DOC_016", "View documents", "200 list contains uploads", r, r.status_code == 200 and len(r.json()) >= 1, req="GET /api/knowledge/documents")
    statuses = {d["name"] if "name" in d else d.get("filename"): d.get("status") for d in (r.json() if r.status_code == 200 else [])}
    record("TC_DOC_011", "Chunking and indexing", "uploads reach ready status", r, ready, note=clip(statuses, 200), req="GET /api/knowledge/documents (poll)")
    record("TC_DOC_010", "Text extraction", "documents processed (ready)", r, ready, req="GET /api/knowledge/documents")

    # ── RAG ────────────────────────────────────────────────────────
    r = c.post("/api/chatbot/test-chat", headers=hdr(token), json={"message": ""})
    record("TC_RAG_009", "Empty chatbot message", "422", r, r.status_code == 422, req="POST /api/chatbot/test-chat")
    ans = None
    for attempt in range(3):
        r = c.post("/api/chatbot/test-chat", headers=hdr(token), json={"message": f"What is the codeword in the Nexus QA handbook {tag}?"})
        if r.status_code == 200 and f"PELICAN-{tag}".lower() in json.dumps(r.json()).lower():
            break
        time.sleep(5)
    record("TC_RAG_001", "Document-based question answering", "answer contains the codeword", r, r.status_code == 200 and f"pelican-{tag}".lower() in json.dumps(r.json()).lower(), req="POST /api/chatbot/test-chat")
    r = c.post("/api/chatbot/test-chat", headers=hdr(token), json={"message": "Who won the 1998 football world cup final and what was the score?"})
    body = json.dumps(r.json()).lower() if r.status_code == 200 else ""
    record("TC_RAG_002", "Question unrelated to uploaded knowledge", "fallback / no fabricated answer", r, r.status_code == 200 and "france" not in body and "brazil" not in body, req="POST /api/chatbot/test-chat")
    r = c.post("/api/chatbot/test-chat", headers=hdr(token), json={"message": "What is the refund window?"})
    r2 = c.post("/api/chatbot/test-chat", headers=hdr(token), json={"message": "And what are the support hours?"})
    record("TC_RAG_010", "Consecutive chatbot messages", "both 200", r2, r.status_code == 200 and r2.status_code == 200, req="POST /api/chatbot/test-chat x2")
    r = c.get("/api/chatbot/config", headers=hdr(token))
    record("TC_RAG_011a", "Read chatbot configuration", "200", r, r.status_code == 200, req="GET /api/chatbot/config")

    # ── Billing (read-only) ────────────────────────────────────────
    r = c.get("/api/settings/billing", headers=hdr(token))
    record("TC_BILL_002", "View subscription details", "200 with plan info", r, r.status_code == 200, req="GET /api/settings/billing")
    record("TC_BILL_007", "View workspace credit balance", "wallet balance present in billing info", r, r.status_code == 200 and "wallet" in json.dumps(r.json()).lower(), req="GET /api/settings/billing")
    r = c.get("/api/settings/billing/wallet/transactions", headers=hdr(token))
    record("TC_BILL_011", "View billing / usage history", "200 list", r, r.status_code == 200, req="GET /api/settings/billing/wallet/transactions")
    r = c.get("/api/settings/subscription-status", headers=hdr(token))
    record("TC_BILL_001", "Trial / subscription status for a new workspace", "200, usable", r, r.status_code == 200 and r.json().get("usable") is True, req="GET /api/settings/subscription-status")

    # ── Cleanup: delete uploaded docs (also evidences TC_DOC_015) ──
    first = True
    for d in doc_ids:
        r = c.delete(f"/api/knowledge/documents/{d}", headers=hdr(token))
        if first:
            record("TC_DOC_015", "Delete document", "200 removed", r, r.status_code == 200, req="DELETE /api/knowledge/documents/{id}")
            first = False

    out = Path(__file__).resolve().parents[1] / "test_results"
    out.mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    (out / f"manual_evidence_{stamp}.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    lines = ["| Test ID | Scenario | Request | HTTP | Expected | Result |", "|---|---|---|---|---|---|"]
    for e in sorted(RESULTS, key=lambda e: e["id"]):
        lines.append(f"| {e['id']} | {e['scenario']} | {e['request']} | {e['http_status']} | {e['expected']} | {e['status']} |")
    (out / f"manual_evidence_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    p = sum(e["status"] == "PASS" for e in RESULTS)
    print(f"\n{p}/{len(RESULTS)} passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--db", required=True, help="asyncpg DSN of the throw-away test database")
    a = ap.parse_args()
    asyncio.run(run(a.base, a.db))
