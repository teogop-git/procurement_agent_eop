"""
Local File Writer
Записва готовите репорти в локалната файлова система
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict

logger = logging.getLogger("LocalFileWriter")


class LocalFileWriter:
    def __init__(self, output_dir: str = "/app/output/reports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save(self, report_path: str, email_data: Dict) -> str:
        src = Path(report_path)
        if not src.exists():
            logger.error(f"Файлът не съществува: {src}")
            return report_path

        # Организирай по дата
        date_dir = self.output_dir / datetime.now().strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)

        dest = date_dir / src.name
        shutil.copy2(src, dest)
        logger.info(f"Копиран: {src.name} → {dest}")
        return str(dest)
