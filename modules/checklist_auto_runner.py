from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional


SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".txt", ".zip"
}


def extract_eop_id(eop_url: str = "", fallback: str = "") -> str:
    if eop_url:
        m = re.search(r"/today/(\d+)", eop_url)
        if m:
            return m.group(1)

        m = re.search(r"(\d{5,})", eop_url)
        if m:
            return m.group(1)

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", fallback or "unknown")
    return safe.strip("_") or "unknown"


def extract_eop_url_from_text(text: str) -> str:
    if not text:
        return ""

    m = re.search(r"https://app\.eop\.bg/[^\s<>\"]+", text)
    return m.group(0).strip() if m else ""


def copy_documents(source_dir: Path, target_dir: Path) -> List[str]:
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: List[str] = []

    for src in source_dir.rglob("*"):
        if not src.is_file():
            continue

        if src.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        dst = target_dir / src.name

        if dst.exists():
            stem = dst.stem
            suffix = dst.suffix
            i = 2
            while True:
                alt = target_dir / f"{stem}_{i}{suffix}"
                if not alt.exists():
                    dst = alt
                    break
                i += 1

        shutil.copy2(src, dst)
        copied.append(str(dst))

    return copied


def run_command(args: List[str]) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = "/app"

    subprocess.run(
        args,
        cwd="/app",
        env=env,
        check=True,
    )


def auto_generate_checklist(
    source_documents_dir: str,
    eop_url: str = "",
    analysis_id: str = "",
    template_path: str = "/app/procurement_agent/templates/Чек лист ОП+договор.docx",
    data_root: str = "/app/procurement_agent/data/eop",
    output_root: str = "/app/procurement_agent/out/eop",
) -> Dict[str, object]:
    source_dir = Path(source_documents_dir)

    if not source_dir.exists():
        raise FileNotFoundError(f"source_documents_dir does not exist: {source_dir}")

    op_id = extract_eop_id(eop_url=eop_url, fallback=analysis_id)

    target_documents_dir = Path(data_root) / op_id / "documents"
    op_output_dir = Path(output_root) / op_id

    target_documents_dir.mkdir(parents=True, exist_ok=True)
    op_output_dir.mkdir(parents=True, exist_ok=True)

    copied_files = copy_documents(source_dir, target_documents_dir)

    output_json = op_output_dir / f"checklist_data.{op_id}.json"
    debug_text = op_output_dir / f"checklist_debug_text.{op_id}.txt"
    output_docx = op_output_dir / f"Чек лист ОП {op_id}.попълнен.auto.docx"

    run_command([
        sys.executable,
        "-m",
        "procurement_agent.modules.checklist_extractor",
        "--documents-dir",
        str(target_documents_dir),
        "--eop-url",
        eop_url,
        "--output-json",
        str(output_json),
        "--debug-text",
        str(debug_text),
        "--preview",
    ])

    run_command([
        sys.executable,
        "-m",
        "procurement_agent.modules.checklist_generator",
        "--template",
        template_path,
        "--mapping",
        "/app/procurement_agent/mappings/checklist_mapping.bg.yml",
        "--data",
        str(output_json),
        "--output",
        str(output_docx),
    ])

    return {
        "enabled": True,
        "op_id": op_id,
        "eop_url": eop_url,
        "source_documents_dir": str(source_dir),
        "target_documents_dir": str(target_documents_dir),
        "copied_files": copied_files,
        "output_json": str(output_json),
        "debug_text": str(debug_text),
        "output_docx": str(output_docx),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatically generate OP checklist from mail analysis documents.")
    parser.add_argument("--source-documents-dir", required=True)
    parser.add_argument("--eop-url", default="")
    parser.add_argument("--analysis-id", default="")
    parser.add_argument("--template", default="/app/procurement_agent/templates/Чек лист ОП+договор.docx")
    parser.add_argument("--data-root", default="/app/procurement_agent/data/eop")
    parser.add_argument("--output-root", default="/app/procurement_agent/out/eop")

    args = parser.parse_args()

    result = auto_generate_checklist(
        source_documents_dir=args.source_documents_dir,
        eop_url=args.eop_url,
        analysis_id=args.analysis_id,
        template_path=args.template,
        data_root=args.data_root,
        output_root=args.output_root,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
