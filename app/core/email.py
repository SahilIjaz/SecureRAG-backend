import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

logger = logging.getLogger(__name__)


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


async def send_otp_email(recipient_email: str, full_name: str, otp: str) -> None:
    """
    Send an OTP verification email via SMTP (TLS on port 587).
    Raises RuntimeError if SMTP credentials are not configured.
    """
    if not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
        # In development without SMTP configured, just log the OTP
        logger.warning(
            "SMTP not configured — OTP for %s is: %s", recipient_email, otp
        )
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your SecureRAG++ verification code"
    msg["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
    msg["To"] = recipient_email

    html_body = _build_otp_html(full_name, otp)
    plain_body = (
        f"Hi {full_name},\n\n"
        f"Your SecureRAG++ verification code is: {otp}\n\n"
        f"It expires in {settings.OTP_EXPIRE_MINUTES} minutes.\n\n"
        f"If you did not request this, ignore this email."
    )

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAILS_FROM_EMAIL, recipient_email, msg.as_string())
        logger.info("OTP email sent to %s", recipient_email)
    except Exception as exc:
        logger.error("Failed to send OTP email to %s: %s", recipient_email, exc)
        raise RuntimeError(f"Email delivery failed: {exc}") from exc
