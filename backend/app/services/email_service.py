import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailSubmissionUnknown(Exception):
    """Submission may have reached SMTP; automatic resend could duplicate mail."""


class EmailBeforeSubmissionFailure(Exception):
    """SMTP connection/authentication failed before message submission."""


class EmailRejected(Exception):
    """SMTP explicitly refused the sender, recipient or message."""


class EmailService:
    """Email sending service with template support.

    SMTP configuration is read from ``Settings`` (``settings.SMTP_*``) rather than
    module-level ``os.getenv`` so a single validated config source drives the app, the
    worker, and Alembic.
    """

    def __init__(self):
        # Setup Jinja2 template environment
        template_dir = Path(__file__).parent.parent / "templates" / "email"
        template_dir.mkdir(parents=True, exist_ok=True)

        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)), autoescape=select_autoescape(['html', 'xml'])
        )

    async def send_email(
        self,
        to: str | List[str],
        subject: str,
        body: str = None,
        template: str = None,
        context: Dict = None,
        html: bool = True,
        message_id: str = None,
    ) -> bool:
        """
        Send email.

        Returns True when SMTP accepted the message, not proof of inbox delivery.
        Returns False when SMTP is unconfigured. Typed transport failures distinguish
        safe pre-submission retry from explicit rejection and uncertain submission;
        callers must not automatically retry the latter.

        Args:
            to: Recipient email(s)
            subject: Email subject
            body: Plain text body (if not using template)
            template: Template name (without .html)
            context: Template context variables
            html: Send as HTML
        """
        # Validate configuration -- soft skip when unconfigured (no raise).
        if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
            logger.warning("SMTP credentials not configured, skipping email send")
            return False

        # Prepare recipients
        recipients = [to] if isinstance(to, str) else to

        # Create message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM}>"
        msg["To"] = ", ".join(recipients)
        if message_id:
            msg["Message-ID"] = f"<{message_id}@werco-background>"

        # Render body
        if template:
            html_body = self._render_template(template, context or {})
            plain_body = self._html_to_plain(html_body)
        else:
            html_body = body
            plain_body = body

        # Attach parts
        msg.attach(MIMEText(plain_body or "", "plain"))
        if html and html_body:
            msg.attach(MIMEText(html_body, "html"))

        # Disable aiosmtplib's implicit opportunistic STARTTLS. Negotiate TLS
        # exactly once: implicit TLS on 465, explicit STARTTLS otherwise.
        submitted = False
        try:
            implicit_tls = settings.SMTP_PORT == 465
            async with aiosmtplib.SMTP(
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                timeout=30,
                start_tls=False,
                use_tls=implicit_tls,
            ) as smtp:
                if not implicit_tls:
                    await smtp.starttls()
                await smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                submitted = True
                errors, _ = await smtp.send_message(msg)
                if errors:
                    # A list send can accept some recipients and refuse others.
                    # Never retry the accepted recipients as a whole batch.
                    raise EmailSubmissionUnknown(
                        'The mail server accepted only some recipients. Check its logs before resending.'
                    )
        except (aiosmtplib.SMTPRecipientsRefused, aiosmtplib.SMTPSenderRefused, aiosmtplib.SMTPDataError) as exc:
            raise EmailRejected('The mail server rejected the recipient, sender or message.') from exc
        except Exception as exc:
            if not submitted:
                raise EmailBeforeSubmissionFailure(
                    'Email connection or authentication failed before submission.'
                ) from exc
            if isinstance(exc, EmailSubmissionUnknown):
                raise
            raise EmailSubmissionUnknown(
                'The mail server outcome is unknown. Check its logs before resending.'
            ) from exc
        logger.info('Mail server accepted background email')
        return True

    def _render_template(self, template_name: str, context: Dict) -> str:
        """Render email template"""
        template = self.env.get_template(f"{template_name}.html")
        return template.render(**context)

    def _html_to_plain(self, html: str) -> str:
        """Convert HTML to plain text (basic)"""
        # Simple HTML to text conversion
        import re

        text = re.sub('<[^<]+?>', '', html)
        text = re.sub(r'\n\s*\n', '\n\n', text)
        return text.strip()


# Singleton instance
email_service = EmailService()
