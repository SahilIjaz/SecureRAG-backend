import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def _build_otp_html(full_name: str, otp: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8"/>
      <style>
        body {{ font-family: Arial, sans-serif; background:#f4f4f4; margin:0; padding:0; }}
        .container {{ max-width:500px; margin:40px auto; background:#ffffff;
                      border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.1); }}
        .header {{ background:#0f2027; padding:24px 32px; }}
        .header h1 {{ color:#ffffff; margin:0; font-size:22px; }}
        .body {{ padding:32px; }}
        .otp-box {{ background:#f0f7ff; border:1px solid #c3dffe; border-radius:8px;
                    text-align:center; padding:24px; margin:24px 0; }}
        .otp-code {{ font-size:42px; font-weight:bold; letter-spacing:12px;
                     color:#0f2027; font-family:monospace; }}
        .note {{ color:#888; font-size:13px; margin-top:8px; }}
        .footer {{ background:#f4f4f4; padding:16px 32px; text-align:center;
                   color:#aaa; font-size:12px; }}
      </style>
    </head>
    <body>
      <div class="container">
        <div class="header"><h1>SecureRAG++</h1></div>
        <div class="body">
          <p>Hi <strong>{full_name}</strong>,</p>
          <p>Use the verification code below to confirm your email address.
             This code expires in <strong>{settings.OTP_EXPIRE_MINUTES} minutes</strong>.</p>
          <div class="otp-box">
            <div class="otp-code">{otp}</div>
            <div class="note">Do not share this code with anyone.</div>
          </div>
          <p>If you did not create a SecureRAG++ account, you can safely ignore this email.</p>
        </div>
        <div class="footer">&copy; 2026 SecureRAG++. All rights reserved.</div>
      </div>
    </body>
    </html>
    """


def _build_plain_body(full_name: str, otp: str) -> str:
    return (
        f"Hi {full_name},\n\n"
        f"Your SecureRAG++ verification code is: {otp}\n\n"
        f"It expires in {settings.OTP_EXPIRE_MINUTES} minutes.\n\n"
        f"If you did not request this, ignore this email."
    )


def _send_via_brevo(recipient_email: str, full_name: str, otp: str) -> None:
    """Send OTP email using Brevo HTTP API (works on Render)."""
    payload = {
        "sender": {
            "name": settings.EMAILS_FROM_NAME,
            "email": settings.EMAILS_FROM_EMAIL,
        },
        "to": [{"email": recipient_email}],
        "subject": "Your SecureRAG++ verification code",
        "htmlContent": _build_otp_html(full_name, otp),
        "textContent": _build_plain_body(full_name, otp),
    }

    headers = {
        "api-key": settings.BREVO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    logger.info("[EMAIL] Sending via Brevo API to %s ...", recipient_email)
    response = httpx.post(BREVO_API_URL, json=payload, headers=headers, timeout=15)

    if response.status_code == 201:
        logger.info("[EMAIL] Brevo success: %s", response.json())
    else:
        logger.error("[EMAIL] Brevo error %s: %s", response.status_code, response.text)
        response.raise_for_status()


def _send_via_smtp(recipient_email: str, full_name: str, otp: str) -> None:
    """Fallback: send OTP email via SMTP (for local development)."""
    smtp_password = settings.SMTP_PASSWORD.replace(" ", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your SecureRAG++ verification code"
    msg["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
    msg["To"] = recipient_email
    msg.attach(MIMEText(_build_plain_body(full_name, otp), "plain"))
    msg.attach(MIMEText(_build_otp_html(full_name, otp), "html"))

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(settings.SMTP_USERNAME, smtp_password)
        server.sendmail(settings.EMAILS_FROM_EMAIL, recipient_email, msg.as_string())


async def send_otp_email(recipient_email: str, full_name: str, otp: str) -> None:
    """
    Send an OTP verification email.
    Uses Brevo HTTP API if configured, otherwise falls back to SMTP, then to logging.
    """
    loop = asyncio.get_event_loop()

    # Priority 1: Brevo HTTP API (works on Render)
    if settings.BREVO_API_KEY:
        try:
            await loop.run_in_executor(None, _send_via_brevo, recipient_email, full_name, otp)
            return
        except Exception as e:
            logger.error("[EMAIL] Brevo failed: %s", e)

    # Priority 2: SMTP (works locally)
    if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
        try:
            await loop.run_in_executor(None, _send_via_smtp, recipient_email, full_name, otp)
            logger.info("[EMAIL] OTP sent via SMTP to %s", recipient_email)
            return
        except Exception as e:
            logger.error("[EMAIL] SMTP failed: %s", e)

    # Priority 3: Log OTP (dev fallback)
    logger.warning("[EMAIL] No email provider available — OTP for %s is: %s", recipient_email, otp)
