"""
Email Monitor - EWS mailbox request/response mode.

Use case:
- Colleagues forward CAIS/EOP procurement emails to paraflow.ai@paraflow.bg.
- The worker reads unread emails from that mailbox.
- It extracts EOP URLs from the forwarded email body.
- It replies to the actual sender of the forwarded email.

This module intentionally does NOT filter only noreply@eop.bg. The requester is
usually an internal colleague, not the original CAIS sender.
"""

import logging
import re
from typing import Dict, List, Optional
from html import unescape

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from exchangelib import (
    Account,
    Configuration,
    Credentials,
    DELEGATE,
    HTMLBody,
    Mailbox,
)
from exchangelib.protocol import BaseProtocol
from exchangelib.items import Message

logger = logging.getLogger("EmailMonitor")


try:
    import requests.adapters

    class _NoVerifyHTTPAdapter(requests.adapters.HTTPAdapter):
        def send(self, *args, **kwargs):
            kwargs["verify"] = False
            return super().send(*args, **kwargs)

except ImportError:
    class _NoVerifyHTTPAdapter:  # type: ignore
        pass


class EmailMonitor:
    def __init__(
        self,
        email_address: str,
        password: str,
        imap_server: str = "",          # reused as EWS host
        imap_port: int = 443,
        use_ssl: bool = True,
        sender_filter: str = "",        # kept for backward compatibility, not used by default
        imap_user: str = "",            # domain\\username
        processed_category: str = "ZOP_AI_ANALYZED",
        failed_category: str = "ZOP_AI_FAILED",
    ):
        self.email_address = email_address
        self.password = password
        self.ews_host = imap_server
        self.sender_filter = (sender_filter or "").lower()
        self.username = imap_user if imap_user else email_address
        self.processed_category = processed_category
        self.failed_category = failed_category
        self._account: Optional[Account] = None

    # ── Connect ───────────────────────────────────────────────────────────────
    def _get_account(self) -> Account:
        if self._account is not None:
            return self._account

        ews_url = f"https://{self.ews_host}/EWS/Exchange.asmx"
        creds = Credentials(username=self.username, password=self.password)
        config = Configuration(
            service_endpoint=ews_url,
            credentials=creds,
            auth_type="NTLM",
            retry_policy=None,
        )

        # Exchange with internal/self-signed certificates.
        BaseProtocol.HTTP_ADAPTER_CLS = _NoVerifyHTTPAdapter

        self._account = Account(
            primary_smtp_address=self.email_address,
            config=config,
            autodiscover=False,
            access_type=DELEGATE,
        )
        logger.info("EWS connection OK → %s (user: %s)", ews_url, self.username)
        return self._account

    # ── URL helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _clean_url(url: str) -> str:
        url = unescape(url or "").strip()
        url = url.rstrip(".,;)>]\"'")
        return url

    @staticmethod
    def _extract_urls(text: str) -> List[str]:
        urls = set()
        for u in re.findall(r"https?://[^\s<>\"']+", text or ""):
            urls.add(EmailMonitor._clean_url(u))
        return sorted(urls)

    @staticmethod
    def _extract_all_urls(msg: Message) -> List[str]:
        urls = set()

        try:
            if msg.text_body:
                for u in re.findall(r"https?://[^\s<>\"']+", str(msg.text_body)):
                    urls.add(EmailMonitor._clean_url(u))
        except Exception:
            pass

        try:
            html = str(msg.body) if msg.body else ""
            if html:
                for u in re.findall(r'href=["\']?(https?://[^"\'>\s]+)', html, flags=re.I):
                    urls.add(EmailMonitor._clean_url(u))
                for u in re.findall(r"https?://[^\s<>\"']+", html):
                    urls.add(EmailMonitor._clean_url(u))
        except Exception:
            pass

        return sorted(u for u in urls if u)

    @staticmethod
    def _is_procurement_url(url: str) -> bool:
        if not url:
            return False
        u = url.lower()
        if "eop.bg" not in u:
            return False
        if "/registration/" in u or "/unsubscribe" in u:
            return False
        return bool(re.search(r"/today/\d+|/procurements/\d+|/tender/\d+|/tenders/\d+|/publicbuyer/", u))

    @staticmethod
    def _dedupe_keep_order(items: List[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @staticmethod
    def _get_body_text(msg: Message) -> str:
        try:
            if msg.text_body:
                return str(msg.text_body)
            if msg.body:
                return re.sub(r"<[^>]+>", " ", str(msg.body))
        except Exception:
            pass
        return ""

    @staticmethod
    def _get_sender_email(msg: Message) -> str:
        sender = getattr(msg, "sender", None)
        if sender is not None:
            email = getattr(sender, "email_address", None)
            if email:
                return str(email)
            raw = str(sender)
            match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", raw)
            if match:
                return match.group(0)
        return ""

    # ── Public: read requests ─────────────────────────────────────────────────
    def get_new_procurement_emails(self, limit: int = 100) -> List[Dict]:
        """
        Backward-compatible name.

        Returns unread mailbox messages that look like analysis requests.
        Important: this method does NOT mark emails as read. The worker marks the
        message as processed only after the reply is sent.
        """
        import datetime as _dt
        account = self._get_account()
        # exchangelib does not load `categories` by default — fetch only unread,
        # then Python-side exclude those already tagged ZOP_AI_ANALYZED.
        from exchangelib import fields as _ef
        all_messages = account.inbox.filter(
            is_read=False,
            datetime_received__gte=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
        ).only(
            "id", "changekey", "subject", "datetime_received",
            "sender", "is_read", "categories", "body"
        ).order_by("-datetime_received")[:limit]
        messages = [
            m for m in all_messages
            if self.processed_category not in (m.categories or [])
        ]

        results: List[Dict] = []
        own = (self.email_address or "").lower()

        for msg in messages:
            subject = msg.subject or ""
            sender_email = self._get_sender_email(msg)
            sender_lower = sender_email.lower()

            if sender_lower == own:
                logger.info("Пропускам собствен имейл: %s", subject[:80])
                continue

            if subject.lower().startswith(("zop analysis", "[zop analysis]", "re: [zop analysis]")):
                logger.info("Пропускам вероятен системен reply: %s", subject[:80])
                continue

            body = self._get_body_text(msg)
            all_urls = self._extract_all_urls(msg)
            procurement_urls = self._dedupe_keep_order([u for u in all_urls if self._is_procurement_url(u)])

            logger.info("Имейл: %s", subject[:100])
            logger.info("  Sender: %s", sender_email)
            logger.info("  URLs: %s | procurement URLs: %s", len(all_urls), len(procurement_urls))

            results.append({
                "message": msg,
                "subject": subject,
                "sender": sender_email,
                "sender_email": sender_email,
                "received_time": msg.datetime_received,
                "body": body,
                "procurement_urls": procurement_urls,
                "all_urls": all_urls,
                "message_id": msg.id,
            })

        logger.info("Общо заявки за обработка: %s", len(results))
        return results

    # ── Public: reply / mark ──────────────────────────────────────────────────
    def send_html_reply(self, to_email: str, subject: str, html_body: str) -> None:
        account = self._get_account()
        if not to_email:
            raise ValueError("Missing recipient email")

        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

        msg = Message(
            account=account,
            folder=account.sent,
            subject=reply_subject,
            body=HTMLBody(html_body),
            to_recipients=[Mailbox(email_address=to_email)],
        )
        msg.send_and_save()
        logger.info("Изпратен анализ до %s | subject=%s", to_email, reply_subject[:100])

    def mark_processed(self, msg: Message, category: Optional[str] = None) -> None:
        cat = category or self.processed_category
        try:
            categories = list(msg.categories or [])
            if cat not in categories:
                categories.append(cat)
            msg.categories = categories
            msg.is_read = True
            msg.save(update_fields=["categories", "is_read"])
        except Exception as exc:
            logger.warning("Не може да маркира имейла като processed: %s", exc)

    def mark_failed(self, msg: Message, category: Optional[str] = None) -> None:
        cat = category or self.failed_category
        try:
            categories = list(msg.categories or [])
            if cat not in categories:
                categories.append(cat)
            # Keep it read to avoid infinite retry loops. The failure category keeps audit trail.
            msg.categories = categories
            msg.is_read = True
            msg.save(update_fields=["categories", "is_read"])
        except Exception as exc:
            logger.warning("Не може да маркира имейла като failed: %s", exc)
