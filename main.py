#!/usr/bin/env python3
"""
Procurement Agent - IMAP Version
Автоматизиран мониторинг на обществени поръчки с vendor-lock анализ
Paraflow AI - paraflow.ai@paraflow.bg
"""

import os
import time
import logging
import schedule
from datetime import datetime
from dotenv import load_dotenv

from modules.email_monitor import EmailMonitor
from modules.document_processor import DocumentProcessor
from modules.vendor_analyzer import VendorAnalyzer
from modules.report_generator import ReportGenerator
from modules.file_writer import LocalFileWriter

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/app/logs/procurement_agent.log"),
    ],
)
logger = logging.getLogger("ProcurementAgent")


# ── Main workflow ─────────────────────────────────────────────────────────────
def run_agent():
    logger.info("=" * 60)
    logger.info("Procurement Agent - стартиране на цикъл")
    logger.info(f"Час: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    try:
        # 1. Проверка на имейл
        monitor = EmailMonitor(
            email_address=os.getenv("EMAIL_ADDRESS"),
            password=os.getenv("EMAIL_PASSWORD"),
            imap_server=os.getenv("IMAP_SERVER", "mail.paraflow.bg"),
            imap_port=int(os.getenv("IMAP_PORT", "993")),
            use_ssl=os.getenv("IMAP_SSL", "true").lower() == "true",
            sender_filter=os.getenv("SENDER_FILTER", "noreply@eop.bg"),
            imap_user=os.getenv("IMAP_USER", ""),   # напр. paraflow\\paraflow.ai
        )

        emails = monitor.get_new_procurement_emails()
        if not emails:
            logger.info("Няма нови поръчки.")
            return

        logger.info(f"Намерени {len(emails)} нови имейл(а).")

        processor = DocumentProcessor(download_dir="/app/output/downloads")
        analyzer = VendorAnalyzer(
            llm_endpoint=os.getenv("LOCAL_LLM_ENDPOINT", "http://ollama:11434/api/generate"),
            llm_model=os.getenv("LOCAL_LLM_MODEL", "qwen2.5:14b-instruct"),
        )
        reporter = ReportGenerator()
        writer = LocalFileWriter(output_dir="/app/output/reports")

        for email_data in emails:
            logger.info(f"Обработка: {email_data['subject']}")

            # 2. Свали документи
            documents = processor.process_email(email_data)
            if not documents:
                logger.warning("Без документи – прескачам.")
                continue

            # 3. Vendor-lock анализ
            analysis_results = []
            for doc in documents:
                result = analyzer.analyze(doc)
                analysis_results.append(result)

            # 4. Генерирай репорт
            report_path = reporter.generate(email_data, analysis_results)

            # 5. Запиши
            final_path = writer.save(report_path, email_data)
            logger.info(f"Репортът е записан: {final_path}")

            # 6. Изпрати репорта по имейл като reply
            try:
                sender = os.getenv("REPORT_RECIPIENT", os.getenv("EMAIL_ADDRESS", ""))
                subject = email_data.get("subject", "ZOP Analysis")
                # Генерирай кратко HTML summary
                risks = [r.get("risk", "UNKNOWN") for r in analysis_results if isinstance(r, dict)]
                tenders = [r.get("tender_id", "") for r in analysis_results if isinstance(r, dict)]
                html_body = f"""<html><body>
                <h2>ZOP Analysis Report</h2>
                <p>Заявка от: {sender}</p>
                <p>Тема: {subject}</p>
                <p>Анализирани поръчки: {len(analysis_results)}</p>
                <ul>{"".join(f"<li>Поръчка {t}: риск {r}</li>" for t, r in zip(tenders, risks))}</ul>
                <p>Пълният репорт е наличен локално: {final_path}</p>
                <p><i>Анализът е автоматичен. За финално решение е нужна експертна проверка.</i></p>
                </body></html>"""
                monitor.send_html_reply(
                    to_email=sender,
                    subject=subject,
                    html_body=html_body,
                )
            except Exception as reply_exc:
                logger.error(f"Грешка при reply: {reply_exc}", exc_info=True)

            # 7. Маркирай като обработен
            try:
                monitor.mark_processed(email_data["message"])
            except Exception as mark_exc:
                logger.error(f"Грешка при mark_processed: {mark_exc}", exc_info=True)

    except Exception as exc:
        logger.error(f"Грешка в цикъла: {exc}", exc_info=True)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_mode = os.getenv("RUN_MODE", "scheduled")
    interval = int(os.getenv("CHECK_INTERVAL_HOURS", "4"))

    if run_mode == "once":
        logger.info("Режим: ONCE")
        run_agent()
    else:
        logger.info(f"Режим: SCHEDULED (на всеки {interval} часа)")
        run_agent()  # веднъж веднага
        schedule.every(interval).hours.do(run_agent)
        while True:
            schedule.run_pending()
            time.sleep(60)
