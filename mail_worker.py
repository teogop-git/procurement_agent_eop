"""
ZOP mailbox worker.

Polls paraflow.ai@paraflow.bg, analyzes every forwarded EOP/CAIS procurement
email, and replies to the colleague who sent the request.
"""

import html
import logging
import os
import re
import time
import traceback
from email.utils import parseaddr
from datetime import datetime
from typing import Any, Dict, List

from procurement_agent.modules.document_processor import DocumentProcessor
from procurement_agent.modules.email_monitor import EmailMonitor
from procurement_agent.modules.vendor_analyzer import VendorAnalyzer

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ZopMailWorker")


def esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else ""


def render_requirements(analysis: Dict[str, Any], max_rows: int = 80) -> str:
    rows = []
    for req in (analysis.get("requirements") or [])[:max_rows]:
        src = req.get("source") or {}
        rows.append(
            "<tr>"
            f"<td>{esc(req.get('id'))}</td>"
            f"<td>{esc(req.get('category'))}</td>"
            f"<td>{esc(req.get('normalized_requirement') or req.get('original_text'))}</td>"
            f"<td>{esc(req.get('threshold'))}</td>"
            f"<td>{esc(src.get('document_name'))}<br>стр. {esc(src.get('page'))}</td>"
            f"<td>{esc(src.get('quote') or req.get('original_text'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>Не са извлечени технически изисквания.</p>"
    return f"""
    <table>
      <thead><tr><th>ID</th><th>Категория</th><th>Изискване</th><th>Праг</th><th>Източник</th><th>Цитат</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_vendor_lock(analysis: Dict[str, Any]) -> str:
    rows = []
    for item in analysis.get("vendor_lock_indicators") or []:
        ev = item.get("evidence") or {}
        rows.append(
            "<tr>"
            f"<td>{esc(item.get('id'))}</td>"
            f"<td>{esc(item.get('requirement_id'))}</td>"
            f"<td>{esc(item.get('risk'))}</td>"
            f"<td>{esc(item.get('indicator_type'))}</td>"
            f"<td>{esc(item.get('reasoning'))}</td>"
            f"<td>{esc(ev.get('document_name'))}<br>стр. {esc(ev.get('page'))}<br>{esc(ev.get('quote'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>Не са открити доказани vendor-lock индикатори.</p>"
    return f"""
    <table>
      <thead><tr><th>ID</th><th>REQ</th><th>Риск</th><th>Индикатор</th><th>Reasoning</th><th>Evidence</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_product_matrix(analysis: Dict[str, Any]) -> str:
    products = analysis.get("candidate_products") or []
    requirements = analysis.get("requirements") or []

    if not products or not requirements:
        return "<p>Няма достатъчно данни за продуктова сравнителна таблица.</p>"

    header = "<tr><th>REQ</th><th>Изискване</th>"
    for p in products:
        header += f"<th>{esc(p.get('vendor'))} {esc(p.get('model'))}</th>"
    header += "</tr>"

    rows = []
    for req in requirements:
        req_id = req.get("id")
        row = f"<tr><td>{esc(req_id)}</td><td>{esc(req.get('normalized_requirement'))}</td>"
        for p in products:
            found = next((c for c in p.get("coverage", []) if c.get("requirement_id") == req_id), None)
            if found:
                row += f"<td><b>{esc(found.get('status'))}</b><br>{esc(found.get('explanation'))}</td>"
            else:
                row += "<td>unknown</td>"
        row += "</tr>"
        rows.append(row)

    return f"""
    <table>
      <thead>{header}</thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_documents(document: Dict[str, Any]) -> str:
    rows = []
    for d in document.get("raw_documents") or []:
        rows.append(
            "<tr>"
            f"<td>{esc(d.get('document_name'))}</td>"
            f"<td>{esc(d.get('document_type'))}</td>"
            f"<td>{esc(d.get('extraction_quality'))}</td>"
            f"<td>{esc(len(d.get('pages') or []))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>Няма успешно извлечени документи.</p>"
    return f"""
    <table>
      <thead><tr><th>Документ</th><th>Тип</th><th>Извличане</th><th>Страници</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_single_result(url: str, result: Dict[str, Any], document: Dict[str, Any]) -> str:
    analysis = result.get("analysis") or {}
    summary = analysis.get("summary") or {}
    confidence = analysis.get("confidence") or {}
    debug = analysis.get("debug") or {}

    recs = "".join(
        f"<li><b>{esc(r.get('type'))}</b>: {esc(r.get('recommendation'))}<br>{esc(r.get('justification'))}</li>"
        for r in analysis.get("recommendations") or []
    ) or "<li>Няма препоръки.</li>"

    limitations = "".join(
        f"<li>{esc(x)}</li>" for x in confidence.get("limitations") or []
    ) or "<li>Няма отчетени ограничения.</li>"

    return f"""
    <section>
      <h2>{esc(document.get('title') or url)}</h2>
      <p><b>URL:</b> {esc(url)}</p>
      <div class="summary">
        <p><b>Risk:</b> {esc(summary.get('overall_risk_level'))}</p>
        <p><b>Vendor-lock detected:</b> {esc(summary.get('vendor_lock_detected'))}</p>
        <p><b>Confidence:</b> {esc(confidence.get('score'))}</p>
        <p><b>Backend:</b> {esc(result.get('analyzed_by'))}</p>
        <p><b>Debug:</b> documents={esc(debug.get('documents_analyzed'))}, chunks={esc(debug.get('chunks_analyzed'))}, products={esc(debug.get('product_catalog_items'))}</p>
        <p>{esc(summary.get('brief_summary'))}</p>
      </div>

      <h3>1. Анализирани документи</h3>
      {render_documents(document)}

      <h3>2. Извлечени технически изисквания</h3>
      {render_requirements(analysis)}

      <h3>3. Vendor-lock индикатори</h3>
      {render_vendor_lock(analysis)}

      <h3>4. Продуктова сравнителна таблица</h3>
      {render_product_matrix(analysis)}

      <h3>5. Препоръки</h3>
      <ul>{recs}</ul>

      <h3>6. Ограничения</h3>
      <ul>{limitations}</ul>
    </section>
    """


def _normalize_email_address(value: str) -> str:
    if not value:
        return ""
    _, addr = parseaddr(str(value))
    return addr.strip().lower()


def _env_csv_set(name: str, default: str = "") -> set[str]:
    raw = os.getenv(name, default)
    return {
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    }


IGNORE_REPLY_SENDERS = _env_csv_set(
    "ZOP_IGNORE_REPLY_SENDERS",
    "noreply@eop.bg,no-reply@eop.bg"
)

FALLBACK_REPORT_RECIPIENT = os.getenv(
    "ZOP_FALLBACK_REPORT_RECIPIENT",
    "TIvanov@paraflow.bg"
).strip()


def _resolve_reply_recipient(sender: str) -> str:
    sender_email = _normalize_email_address(sender)

    if not sender_email:
        return FALLBACK_REPORT_RECIPIENT

    if sender_email in IGNORE_REPLY_SENDERS:
        if FALLBACK_REPORT_RECIPIENT:
            logger.info(
                "Sender %s is ignored/noreply. Sending analysis to fallback recipient %s.",
                sender_email,
                FALLBACK_REPORT_RECIPIENT,
            )
            return FALLBACK_REPORT_RECIPIENT

        logger.info("Sender %s is ignored/noreply. Skipping reply.", sender_email)
        return ""

    return sender_email


def render_email_response(request: Dict[str, Any], results: List[Dict[str, Any]], errors: List[str]) -> str:
    result_html = []
    for item in results:
        result_html.append(render_single_result(item["url"], item["result"], item["document"]))

    errors_html = "".join(f"<li>{esc(e)}</li>" for e in errors)
    if errors_html:
        errors_html = f"<h2>Грешки при обработка</h2><ul>{errors_html}</ul>"

    return f"""
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: Arial, sans-serif; color: #222; }}
        h1, h2, h3 {{ color: #17324d; }}
        table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px 0; }}
        th, td {{ border: 1px solid #d0d7de; padding: 7px; vertical-align: top; font-size: 12px; }}
        th {{ background: #f1f5f9; }}
        .summary {{ background: #f8fafc; border-left: 4px solid #17324d; padding: 12px; margin: 12px 0; }}
        section {{ border-top: 2px solid #e5e7eb; margin-top: 24px; padding-top: 16px; }}
      </style>
    </head>
    <body>
      <h1>ZOP Analysis Report</h1>
      <p><b>Заявка от:</b> {esc(request.get('sender_email') or request.get('sender'))}</p>
      <p><b>Тема:</b> {esc(request.get('subject'))}</p>
      <p><b>Дата на анализ:</b> {esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>
      <p>Анализът е автоматичен. За финално решение по участие/оспорване е нужна експертна проверка.</p>
      {errors_html}
      {''.join(result_html)}
    </body>
    </html>
    """


def render_no_url_response(request: Dict[str, Any]) -> str:
    return f"""
    <html><body style="font-family: Arial, sans-serif;">
      <h2>ZOP Analysis</h2>
      <p>Не открих валиден линк към обществена поръчка в полученото съобщение.</p>
      <p>Моля препратете имейл от ЦАИС/ЕОП, който съдържа линк от вида:</p>
      <pre>https://app.eop.bg/today/...</pre>
      <p>Тема на полученото съобщение: {esc(request.get('subject'))}</p>
    </body></html>
    """

def build_monitor() -> EmailMonitor:
    email_address = os.getenv("EMAIL_ADDRESS") or os.getenv("MAIL_ADDRESS") or "paraflow.ai@paraflow.bg"
    password = os.getenv("EMAIL_PASSWORD") or os.getenv("MAIL_PASSWORD") or ""
    ews_host = os.getenv("IMAP_SERVER") or os.getenv("EWS_HOST") or ""
    ews_user = os.getenv("IMAP_USER") or os.getenv("EWS_USER") or ""

    if not password:
        raise RuntimeError("Missing EMAIL_PASSWORD/MAIL_PASSWORD")
    if not ews_host:
        raise RuntimeError("Missing IMAP_SERVER/EWS_HOST")

    return EmailMonitor(
        email_address=email_address,
        password=password,
        imap_server=ews_host,
        imap_user=ews_user,
    )


def process_one_request(monitor: EmailMonitor, request: Dict[str, Any]) -> None:
    sender = request.get("sender_email") or request.get("sender")
    recipient = _resolve_reply_recipient(sender)

    if not recipient:
        logger.info("No valid recipient for ZOP analysis reply. sender=%s", sender)
        return

    subject = request.get("subject") or "ZOP Analysis Request"
    msg = request.get("message")
    urls = request.get("procurement_urls") or request.get("urls") or []

    if isinstance(urls, str):
        urls = [urls]

    if not urls:
        monitor.send_html_reply(recipient, f"[ZOP Analysis] Няма намерен линк - {subject}", render_no_url_response(request))
        if msg:
            monitor.mark_failed(msg)
        return

    processor = DocumentProcessor()
    analyzer = VendorAnalyzer()

    results: List[Dict[str, Any]] = []
    errors: List[str] = []

    for url in urls:
        try:
            logger.info("Processing URL: %s", url)
            document = processor.process_url(url)
            result = analyzer.analyze(document)
            results.append({"url": url, "document": document, "result": result})
        except Exception as exc:
            logger.exception("Failed URL: %s", url)
            errors.append(f"{url}: {exc}")

    html_body = render_email_response(request, results, errors)
    reply_subject = f"[ZOP Analysis] {subject}"
    monitor.send_html_reply(recipient, reply_subject, html_body)

    if msg:
        if results:
            monitor.mark_processed(msg)
        else:
            monitor.mark_failed(msg)


def run_once() -> None:
    monitor = build_monitor()
    requests = monitor.get_new_procurement_emails(limit=int(os.getenv("ZOP_MAIL_BATCH_LIMIT", "50")))
    for request in requests:
        try:
            process_one_request(monitor, request)
        except Exception:
            logger.error("Request failed:\n%s", traceback.format_exc())
            msg = request.get("message")
            if msg:
                monitor.mark_failed(msg)


def main() -> None:
    poll_seconds = int(os.getenv("ZOP_MAIL_POLL_SECONDS", "60"))
    run_once_only = os.getenv("RUN_ONCE", "0") == "1"

    while True:
        run_once()
        if run_once_only:
            break
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
