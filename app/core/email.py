import asyncio
import logging
import smtplib
import socket
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


def _send_smtp_blocking(recipient_email: str, msg_string: str) -> None:
    """
    Synchronous SMTP send — runs in a thread pool via run_in_executor.
    Gmail App Passwords are stored with spaces for readability but must
    be used without spaces when authenticating.
    """
    smtp_password = settings.SMTP_PASSWORD.replace(" ", "")

    logger.info("[EMAIL] SMTP config: host=%s, port=%s, username=%s",
                settings.SMTP_HOST, settings.SMTP_PORT, settings.SMTP_USERNAME)

    # Step 1: DNS resolution check
    logger.info("[EMAIL] Step 1: Resolving DNS for %s ...", settings.SMTP_HOST)
    try:
        addr_info = socket.getaddrinfo(settings.SMTP_HOST, settings.SMTP_PORT)
        logger.info("[EMAIL] DNS resolved: %s", addr_info[0][4])
    except socket.gaierror as e:
        logger.error("[EMAIL] DNS resolution FAILED: %s", e)
        raise

    # Step 2: Raw socket connectivity check
    logger.info("[EMAIL] Step 2: Testing raw socket to %s:%s ...", settings.SMTP_HOST, settings.SMTP_PORT)
    try:
        test_sock = socket.create_connection((settings.SMTP_HOST, int(settings.SMTP_PORT)), timeout=10)
        test_sock.close()
        logger.info("[EMAIL] Raw socket connection OK")
    except OSError as e:
        logger.error("[EMAIL] Raw socket connection FAILED: %s", e)
        raise

    # Step 3: SMTP_SSL connection
    logger.info("[EMAIL] Step 3: Opening SMTP_SSL connection ...")
    with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
        logger.info("[EMAIL] SMTP_SSL connected successfully")

        # Step 4: Login
        logger.info("[EMAIL] Step 4: Logging in as %s ...", settings.SMTP_USERNAME)
        server.login(settings.SMTP_USERNAME, smtp_password)
        logger.info("[EMAIL] Login successful")

        # Step 5: Send
        logger.info("[EMAIL] Step 5: Sending email to %s ...", recipient_email)
        server.sendmail(settings.EMAILS_FROM_EMAIL, recipient_email, msg_string)
        logger.info("[EMAIL] Email sent successfully to %s", recipient_email)


async def send_otp_email(recipient_email: str, full_name: str, otp: str) -> None:
    """
    Send an OTP verification email.
    Falls back to logging the OTP when SMTP is not configured or sending fails.
    """
    if not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
        logger.warning("[EMAIL] SMTP not configured — OTP for %s is: %s", recipient_email, otp)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your SecureRAG++ verification code"
    msg["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
    msg["To"] = recipient_email

    plain_body = (
        f"Hi {full_name},\n\n"
        f"Your SecureRAG++ verification code is: {otp}\n\n"
        f"It expires in {settings.OTP_EXPIRE_MINUTES} minutes.\n\n"
        f"If you did not request this, ignore this email."
    )
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(_build_otp_html(full_name, otp), "html"))

    msg_string = msg.as_string()

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_smtp_blocking, recipient_email, msg_string)
        logger.info("[EMAIL] OTP email sent to %s", recipient_email)
    except Exception as e:
        logger.error("[EMAIL] FAILED to send OTP email to %s: %s", recipient_email, e)
        logger.warning("[EMAIL] Falling back — OTP for %s is: %s", recipient_email, otp)
