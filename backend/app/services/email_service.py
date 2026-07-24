import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import settings

logger = logging.getLogger(__name__)


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
    ) -> bool:
        """
        Send email.

        Returns True if sent. Returns False (WITHOUT raising) only when SMTP is not
        configured -- so an unconfigured dev/test environment logs a skip instead of
        spamming ARQ retries. On a real transport failure the exception PROPAGATES so the
        enqueuing job (``send_email_job``) can retry and record the terminal outcome
        (fixes the swallow-all defect §9.2).

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

        # Send email -- a transport failure raises out of this call so the job retries.
        async with aiosmtplib.SMTP(hostname=settings.SMTP_HOST, port=settings.SMTP_PORT) as smtp:
            await smtp.starttls()
            await smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            await smtp.send_message(msg)

        logger.info(f"Email sent to {recipients}: {subject}")
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
