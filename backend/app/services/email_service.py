import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
from typing import Dict, Optional, List
import os
import logging

logger = logging.getLogger(__name__)

# Email configuration from environment
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@werco.com")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Werco ERP System")


class EmailService:
    """Email sending service with template support"""

    def __init__(self):
        # Setup Jinja2 template environment
        template_dir = Path(__file__).parent.parent / "templates" / "email"
        template_dir.mkdir(parents=True, exist_ok=True)

        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(['html', 'xml'])
        )

    async def send_email(
        self,
        to: str | List[str],
        subject: str,
        body: str = None,
        template: str = None,
        context: Dict = None,
        html: bool = True
    ) -> bool:
        """
        Send email

        Args:
            to: Recipient email(s)
            subject: Email subject
            body: Plain text body (if not using template)
            template: Template name (without .html)
            context: Template context variables
            html: Send as HTML

        Returns:
            True if sent successfully
        """
        try:
            # Validate configuration
            if not SMTP_USER or not SMTP_PASSWORD:
                logger.warning("SMTP credentials not configured, skipping email send")
                return False

            # Prepare recipients
            recipients = [to] if isinstance(to, str) else to

            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
            msg["To"] = ", ".join(recipients)

            # Render body
            if template:
                html_body = self._render_template(template, context or {})
                plain_body = self._html_to_plain(html_body)
            else:
                html_body = body
                plain_body = body

            # Attach parts
            msg.attach(MIMEText(plain_body, "plain"))
            if html and html_body:
                msg.attach(MIMEText(html_body, "html"))

            # Send email
            async with aiosmtplib.SMTP(hostname=SMTP_HOST, port=SMTP_PORT) as smtp:
                await smtp.starttls()
                await smtp.login(SMTP_USER, SMTP_PASSWORD)
                await smtp.send_message(msg)

            logger.info(f"Email sent to {recipients}: {subject}")
            return True

        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

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

    async def send_batch(
        self,
        emails: List[Dict],
        max_batch_size: int = 50
    ) -> Dict[str, int]:
        """
        Send batch of emails

        Args:
            emails: List of email dicts with 'to', 'subject', 'body/template', 'context'
            max_batch_size: Max emails per batch

        Returns:
            Dict with success/failure counts
        """
        success = 0
        failed = 0

        for i in range(0, len(emails), max_batch_size):
            batch = emails[i:i + max_batch_size]

            for email_data in batch:
                sent = await self.send_email(**email_data)
                if sent:
                    success += 1
                else:
                    failed += 1

        return {"success": success, "failed": failed}


# Singleton instance
email_service = EmailService()
