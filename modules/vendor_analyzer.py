"""
Vendor-Lock Analyzer v2.

Backward compatible with the old VendorAnalyzer.analyze(document) entrypoint, but the
analysis schema is now evidence-driven and suitable for a ZOP agent report.

Expected document input from document_processor_corrected.py:
{
  "procurement_url": "...",
  "title": "...",
  "combined_text": "...",          # kept for old compatibility
  "raw_documents": [ ... ],        # preferred, with document/page metadata
  "sources_reviewed": [ ... ]
}

Optional product catalog:
Set PRODUCT_CATALOG_PATH=/app/config/product_catalog.json
Catalog format:
[
  {
    "vendor": "Dell",
    "model": "PowerEdge R760",
    "category": "Server",
    "product_source": "local_datasheet",
    "datasheet_url": "...",
    "specs_json": {"cpu": "...", "memory": "..."},
    "evidence": {"document_name": "...", "page": 1, "quote": "..."}
  }
]

If no product catalog is provided, the analyzer must not claim product compliance.
It must return unknown/no catalog limitations instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
try:
    from .technical_spec_extractor import TechnicalSpecExtractor
except ImportError:
    try:
        from procurement_agent.modules.technical_spec_extractor import TechnicalSpecExtractor
    except ImportError:
        from modules.technical_spec_extractor import TechnicalSpecExtractor

import requests

logger = logging.getLogger("VendorAnalyzer")

RiskLevel = str

SYSTEM_PROMPT = """Ти си експерт по обществени поръчки, vendor-lock анализатор и presales solution engineer в България.

КРИТИЧНО:
- Връщай единствено валиден JSON обект.
- Не използвай markdown.
- Не измисляй факти, продукти, страници, цитати или технически параметри.
- Всеки извод трябва да има evidence с document_name, page/chunk_id и quote.
- Ако няма evidence, маркирай като INSUFFICIENT_EVIDENCE или unknown.
- LOW риск се допуска само когато има анализирани документи, извлечени изисквания и доказателства защо няма заключване.
- Product compliance "meets" е допустим само ако има продуктова спецификация в предоставения product catalog.
"""

ANALYSIS_PROMPT = """Анализирай обществена поръчка за vendor-lock риск и техническо съответствие.

ПОРЪЧКА:
URL: {procurement_url}
Заглавие: {title}

АНАЛИЗИРАНИ ИЗТОЧНИЦИ:
{sources_json}

ДОКУМЕНТНИ CHUNKS С МЕТАДАННИ:
{chunks_json}

ПРОДУКТОВ КАТАЛОГ:
{product_catalog_json}

ЗАДАЧИ:
1. Извлечи всички технически изисквания като REQ записи.
2. За всяко изискване посочи оригинален цитат, документ, страница и chunk_id.
3. Анализирай vendor-lock индикатори:
   - конкретен производител
   - конкретен модел
   - part number
   - proprietary технология
   - необосновано тесен параметър
   - уникална комбинация от параметри
   - сертификат, който ограничава пазара
   - гаранционно/сервизно условие
   - липса на "или еквивалент"
   - съвместимост с конкретна среда без обосновка
4. Ако рискът е LOW, обясни детайлно защо НЕ е заключена, с evidence.
5. Посочи остатъчните рискове.
6. Ако има продуктов каталог, сравни продуктите срещу всяко REQ. Ако няма каталог, не измисляй продукти.
7. Дай препоръки: participate, ask_clarification, propose_equivalent, challenge_requirement, no_bid.
8. Посочи limitations и confidence.

Върни JSON точно в този формат:
{{
  "summary": {{
    "overall_risk_level": "LOW | MEDIUM | HIGH | INSUFFICIENT_EVIDENCE",
    "vendor_lock_detected": true,
    "brief_summary": "...",
    "confidence_score": 0.0
  }},
  "procurement": {{
    "url": "{procurement_url}",
    "title": "{title}"
  }},
  "sources_reviewed": [
    {{
      "document_name": "...",
      "document_type": "...",
      "pages_reviewed": [1, 2],
      "status": "reviewed | no_text | missing",
      "extraction_quality": "text | low_text | empty | unknown"
    }}
  ],
  "requirements": [
    {{
      "id": "REQ-001",
      "category": "CPU | RAM | Storage | Network | Warranty | Certification | Software | Service | Delivery | Other",
      "original_text": "точният текст от документа",
      "normalized_requirement": "нормализирано техническо изискване",
      "mandatory": true,
      "threshold": "конкретен праг/стойност или null",
      "source": {{
        "document_name": "...",
        "page": 1,
        "section": null,
        "chunk_id": "...",
        "quote": "..."
      }}
    }}
  ],
  "vendor_lock_indicators": [
    {{
      "id": "VL-001",
      "requirement_id": "REQ-001",
      "indicator_type": "brand_reference | model_reference | part_number | proprietary_technology | narrow_parameter | unique_combination | certification | warranty_constraint | service_constraint | delivery_constraint | unclear_equivalent | other",
      "risk": "LOW | MEDIUM | HIGH | INSUFFICIENT_EVIDENCE",
      "reasoning": "детайлно защо това е или не е риск",
      "evidence": {{
        "document_name": "...",
        "page": 1,
        "section": null,
        "chunk_id": "...",
        "quote": "..."
      }}
    }}
  ],
  "why_not_locked": [
    {{
      "claim": "конкретна причина защо поръчката не изглежда заключена",
      "supporting_requirement_ids": ["REQ-001"],
      "evidence": {{
        "document_name": "...",
        "page": 1,
        "section": null,
        "chunk_id": "...",
        "quote": "..."
      }}
    }}
  ],
  "residual_risks": [
    {{
      "risk": "...",
      "reason": "...",
      "recommended_action": "...",
      "evidence": {{
        "document_name": "...",
        "page": 1,
        "section": null,
        "chunk_id": "...",
        "quote": "..."
      }}
    }}
  ],
  "candidate_products": [
    {{
      "vendor": "...",
      "model": "...",
      "category": "...",
      "product_source": "...",
      "match_score": 0.0,
      "coverage": [
        {{
          "requirement_id": "REQ-001",
          "status": "meets | partially_meets | does_not_meet | unknown",
          "product_spec": "...",
          "explanation": "кое покрива и кое не",
          "evidence": {{
            "document_name": "...",
            "page": 1,
            "section": null,
            "chunk_id": "...",
            "quote": "..."
          }}
        }}
      ],
      "overall_comment": "..."
    }}
  ],
  "product_comparison_matrix": [
    {{
      "requirement_id": "REQ-001",
      "requirement": "...",
      "products": [
        {{"vendor": "...", "model": "...", "status": "meets | partially_meets | does_not_meet | unknown", "comment": "..."}}
      ]
    }}
  ],
  "recommendations": [
    {{
      "type": "participate | ask_clarification | propose_equivalent | challenge_requirement | no_bid",
      "recommendation": "...",
      "justification": "..."
    }}
  ],
  "confidence": {{
    "score": 0.0,
    "limitations": ["..."]
  }},
  "debug": {{
    "documents_analyzed": 0,
    "chunks_analyzed": 0,
    "product_catalog_items": 0
  }}
}}
"""


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    raw = (raw or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if match:
        raw = match.group(1).strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse error: %s | sample=%s", exc, raw[:500])
        return None


def _evidence_ok(obj: Dict[str, Any], key: str = "evidence") -> bool:
    ev = obj.get(key)
    if not isinstance(ev, dict):
        return False
    return bool(ev.get("document_name") and ev.get("quote"))


def _source_ok(req: Dict[str, Any]) -> bool:
    src = req.get("source")
    if not isinstance(src, dict):
        return False
    return bool(src.get("document_name") and src.get("quote"))


def _empty_analysis(reason: str, title: str = "", url: str = "") -> Dict[str, Any]:
    return {
        "summary": {
            "overall_risk_level": "INSUFFICIENT_EVIDENCE",
            "vendor_lock_detected": False,
            "brief_summary": f"Недостатъчни данни за надежден анализ: {reason}",
            "confidence_score": 0.0,
        },
        "procurement": {"url": url, "title": title},
        "sources_reviewed": [],
        "requirements": [],
        "vendor_lock_indicators": [],
        "why_not_locked": [],
        "residual_risks": [],
        "candidate_products": [],
        "product_comparison_matrix": [],
        "recommendations": [
            {
                "type": "ask_clarification",
                "recommendation": "Да не се взема решение преди повторно извличане на документацията.",
                "justification": reason,
            }
        ],
        "confidence": {"score": 0.0, "limitations": [reason]},
        "debug": {"documents_analyzed": 0, "chunks_analyzed": 0, "product_catalog_items": 0},
        # legacy keys for existing renderer compatibility
        "hardware_specifications": [],
        "software_requirements": [],
        "certifications_and_standards": [],
        "indirect_indicators": [],
        "compliant_reformulations": [],
    }


class _LocalOllamaBackend:
    name = "local"

    def __init__(self, endpoint: str, model: str, timeout: int = int(os.getenv("OLLAMA_ANALYSIS_TIMEOUT", "180"))):
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout

    def call(self, prompt: str) -> Tuple[bool, str]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": SYSTEM_PROMPT,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 4096},
        }
        try:
            resp = requests.post(self.endpoint, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return True, resp.json().get("response", "")
        except Exception as exc:
            return False, str(exc)


class _AnthropicBackend:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514", timeout: int = 180):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def call(self, prompt: str) -> Tuple[bool, str]:
        if not self.api_key:
            return False, "Няма ANTHROPIC_API_KEY"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 8192,
            "temperature": 0.0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True, resp.json()["content"][0]["text"]
        except Exception as exc:
            return False, str(exc)


class _OpenAIBackend:
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o", timeout: int = 180):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def call(self, prompt: str) -> Tuple[bool, str]:
        if not self.api_key:
            return False, "Няма OPENAI_API_KEY"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 8192,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True, resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            return False, str(exc)


class VendorAnalyzer:
    def __init__(
        self,
        llm_endpoint: str = "http://ollama:11434/api/generate",
        llm_model: str = "qwen2.5:14b-instruct",
        max_text_chars: int = 30_000,
        max_chunks: int = 30,
        max_chunk_chars: int = 2_000,
    ):
        self.max_text_chars = max_text_chars
        self.max_chunks = max_chunks
        self.max_chunk_chars = max_chunk_chars

        priority_str = os.getenv("LLM_PRIORITY", "local,anthropic,openai")
        priority: List[str] = [p.strip() for p in priority_str.split(",") if p.strip()]

        all_backends = {
            "local": _LocalOllamaBackend(endpoint=llm_endpoint, model=os.getenv("OLLAMA_ANALYSIS_MODEL", llm_model)),
            "anthropic": _AnthropicBackend(
                api_key=os.getenv("ANTHROPIC_API_KEY", ""),
                model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            ),
            "openai": _OpenAIBackend(
                api_key=os.getenv("OPENAI_API_KEY", ""),
                model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            ),
        }

        self.backends = [all_backends[n] for n in priority if n in all_backends]
        logger.info("LLM приоритет: %s", " → ".join(b.name for b in self.backends))

    def _run_with_fallback(self, prompt: str) -> Tuple[Dict[str, Any], str]:
        for backend in self.backends:
            logger.info("  Опит с [%s]...", backend.name)
            ok, raw = backend.call(prompt)
            if not ok:
                logger.warning("  [%s] неуспешен: %s", backend.name, raw)
                continue
            parsed = _parse_json(raw)
            if parsed is None:
                logger.warning("  [%s] невалиден JSON – следващ", backend.name)
                continue
            logger.info("  ✅ [%s] успешен", backend.name)
            return parsed, backend.name

        logger.error("  ❌ Всички backends неуспешни")
        return {}, "none"

    @staticmethod
    def _split_env_list(value: str) -> List[str]:
        return [x.strip().lower() for x in (value or "").split(",") if x.strip()]

    @staticmethod
    def _extract_domain(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        try:
            parsed = urlparse(value if "://" in value else f"https://{value}")
            host = (parsed.netloc or parsed.path.split("/", 1)[0]).lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    @classmethod
    def _domain_allowed(cls, value: str, allowed_domains: List[str]) -> bool:
        if not allowed_domains:
            return True
        domain = cls._extract_domain(value)
        if not domain:
            return False
        return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in allowed_domains)

    @staticmethod
    def _extract_hpe_sku(text: str) -> Optional[str]:
        text = str(text or "")
        matches = re.findall(r"[A-Z][A-Z0-9]{4,}(?:-[A-Z0-9]{2,})?", text)
        skip = {"HPE", "SKU", "OCA", "VMWARE", "COHESITY", "VEEAM", "SERVER", "SERVERS"}
        for m in matches:
            if m.upper() not in skip and any(ch.isdigit() for ch in m):
                return m.upper()
        return None

    @staticmethod
    def _normalize_discovery_model(manufacturer: str, product_name: str) -> str:
        manufacturer_l = str(manufacturer or "").lower()
        product_name_s = str(product_name or "").strip()
        product_name_l = product_name_s.lower()
        sku = VendorAnalyzer._extract_hpe_sku(product_name_s)

        if manufacturer_l in {"hpe", "hewlett packard enterprise"}:
            if "proliant dl380 gen11" in product_name_l:
                return f"ProLiant DL380 Gen11 ({sku})" if sku else "ProLiant DL380 Gen11"
            if "proliant dl380 gen10" in product_name_l:
                return f"ProLiant DL380 Gen10 ({sku})" if sku else "ProLiant DL380 Gen10"

        return product_name_s or "unknown"

    @staticmethod
    def _is_discovery_candidate_allowed(candidate: Dict[str, Any], filename: str) -> Tuple[bool, str]:
        source_type = str(candidate.get("source_type") or "").strip().lower()
        manufacturer = str(candidate.get("manufacturer") or candidate.get("vendor") or "").strip().lower()
        product_name = str(candidate.get("product_name") or candidate.get("model") or candidate.get("name") or "").strip()
        product_name_l = product_name.lower()
        filename_l = str(filename or "").lower()

        skip_test_files = os.getenv("PRODUCT_DISCOVERY_SKIP_TEST_FILES", "true").lower() in {"1", "true", "yes", "on"}
        if skip_test_files and filename_l.startswith("test-"):
            return False, "test discovery file"

        allowed_manufacturers = VendorAnalyzer._split_env_list(
            os.getenv(
                "PRODUCT_DISCOVERY_ALLOWED_MANUFACTURERS",
                "hpe,hewlett packard enterprise,dell,lenovo",
            )
        )
        if allowed_manufacturers and manufacturer not in allowed_manufacturers:
            return False, f"manufacturer not allowed: {manufacturer or 'unknown'}"

        allow_search = os.getenv("PRODUCT_DISCOVERY_ALLOW_SEARCH", "false").lower() in {"1", "true", "yes", "on"}
        allow_shopping = os.getenv("PRODUCT_DISCOVERY_ALLOW_SHOPPING", "false").lower() in {"1", "true", "yes", "on"}

        if source_type == "search" and not allow_search:
            return False, "search source disabled"
        if source_type == "shopping" and not allow_shopping:
            return False, "shopping source disabled"

        allowed_source_types = VendorAnalyzer._split_env_list(
            os.getenv(
                "PRODUCT_DISCOVERY_ALLOWED_SOURCE_TYPES",
                "datasheet,product_bulletin,hpe_product_bulletin,local_datasheet,local_catalog,quick_specs,quickspecs",
            )
        )
        if source_type and source_type not in allowed_source_types:
            return False, f"source type not allowed: {source_type}"

        allowed_domains = VendorAnalyzer._split_env_list(
            os.getenv(
                "PRODUCT_DISCOVERY_ALLOWED_DOMAINS",
                "hpe.com,dell.com,lenovo.com",
            )
        )

        product_url = candidate.get("product_url") or ""
        datasheet_url = candidate.get("datasheet_url") or ""
        raw = candidate.get("raw_search_result") or {}
        raw_source = raw.get("source") or ""
        raw_db_path = raw.get("db_path") or ""
        qs_file = raw.get("QSFileName") or raw.get("QSPrimaryFileName") or ""

        has_local_enterprise_evidence = bool(
            source_type in allowed_source_types
            and manufacturer in allowed_manufacturers
            and (raw_db_path or qs_file or candidate.get("evidence_quotes"))
        )

        external_refs = [x for x in [product_url, datasheet_url, raw_source] if x]
        if external_refs and allowed_domains:
            if not any(VendorAnalyzer._domain_allowed(ref, allowed_domains) for ref in external_refs):
                return False, "domain not allowed"
        elif allowed_domains and not has_local_enterprise_evidence:
            return False, "no allowed domain or local enterprise evidence"

        accessory_markers = [
            "drive cage kit",
            "midplane",
            "backplane",
            "cable kit",
            "riser kit",
            "fan kit",
            "power supply kit",
            "heatsink kit",
            "enablement kit",
            "ordering instructions",
            "tracking",
            "oca",
            " kit ",
            " cage ",
        ]
        if any(marker in f" {product_name_l} " for marker in accessory_markers):
            return False, "accessory/order helper record"

        blocked_domains = VendorAnalyzer._split_env_list(
            os.getenv(
                "PRODUCT_DISCOVERY_BLOCKED_DOMAINS",
                "ebay.com,amazon.com,aliexpress.com,temu.com,walmart.com,newegg.com,msy.com.au",
            )
        )
        if any(VendorAnalyzer._domain_allowed(ref, blocked_domains) for ref in external_refs):
            return False, "blocked public marketplace/domain"

        return True, "allowed"

    def _load_product_catalog(self) -> List[Dict[str, Any]]:
        catalog: List[Dict[str, Any]] = []

        path = os.getenv("PRODUCT_CATALOG_PATH", "/app/config/product_catalog.json").strip()

        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            item = dict(item)
                            item.setdefault("product_source", "local_catalog")
                            catalog.append(item)
                    logger.info("Продуктов каталог: %s продукта от %s", len(data), path)
                else:
                    logger.warning("PRODUCT_CATALOG_PATH=%s не съдържа JSON list", path)

            except Exception as exc:
                logger.warning("Не може да се зареди PRODUCT_CATALOG_PATH=%s: %s", path, exc)

        discovery_dir = os.getenv("PRODUCT_DISCOVERY_OUTPUT_DIR", "/app/product_discovery_output").strip()

        if os.path.isdir(discovery_dir):
            loaded_from_discovery = 0
            skipped_from_discovery = 0
            skip_reasons: Dict[str, int] = {}

            for filename in sorted(os.listdir(discovery_dir)):
                if not filename.endswith(".product_discovery.json"):
                    continue

                full_path = os.path.join(discovery_dir, filename)

                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        payload = json.load(f)

                    candidates = payload.get("candidates") or []

                    if not isinstance(candidates, list):
                        logger.warning("Product discovery файлът %s има невалидно candidates поле", full_path)
                        continue

                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            continue

                        allowed, reason = self._is_discovery_candidate_allowed(candidate, filename)
                        if not allowed:
                            skipped_from_discovery += 1
                            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                            continue

                        manufacturer = (
                            candidate.get("manufacturer")
                            or candidate.get("vendor")
                            or "unknown"
                        )

                        product_name = (
                            candidate.get("product_name")
                            or candidate.get("model")
                            or candidate.get("name")
                            or "unknown"
                        )
                        product_name = self._normalize_discovery_model(manufacturer, product_name)

                        category = candidate.get("category") or "unknown"
                        specs = candidate.get("extracted_specs") or {}

                        evidence_quotes = candidate.get("evidence_quotes") or []
                        if isinstance(evidence_quotes, list):
                            quote = "\\n".join(str(x) for x in evidence_quotes[:5] if x)
                        else:
                            quote = str(evidence_quotes)

                        raw_search_result = candidate.get("raw_search_result") or {}
                        sku = self._extract_hpe_sku(candidate.get("product_name") or product_name)

                        normalized = {
                            "vendor": str(manufacturer).strip(),
                            "model": str(product_name).strip(),
                            "category": str(category).strip(),
                            "sku": sku,
                            "product_source": (
                                candidate.get("source_type")
                                or raw_search_result.get("source")
                                or "product_discovery"
                            ),
                            "datasheet_url": candidate.get("datasheet_url") or candidate.get("product_url"),
                            "product_url": candidate.get("product_url"),
                            "specs_json": specs,
                            "evidence": {
                                "document_name": (
                                    raw_search_result.get("QSFileName")
                                    or raw_search_result.get("QSPrimaryFileName")
                                    or candidate.get("datasheet_url")
                                    or candidate.get("product_url")
                                    or filename
                                ),
                                "page": None,
                                "section": "product_discovery",
                                "chunk_id": candidate.get("req_id"),
                                "quote": quote[:1000],
                            },
                            "discovery": {
                                "analysis_id": payload.get("analysis_id"),
                                "req_id": candidate.get("req_id"),
                                "score": candidate.get("score"),
                                "confidence": candidate.get("confidence"),
                                "compliance": candidate.get("compliance"),
                                "gaps": candidate.get("gaps"),
                                "source_file": filename,
                            },
                        }

                        if normalized["vendor"].lower() == "unknown" and normalized["model"].lower() == "unknown":
                            continue

                        catalog.append(normalized)
                        loaded_from_discovery += 1

                except Exception as exc:
                    logger.warning("Не може да се зареди product discovery файл %s: %s", full_path, exc)

            if loaded_from_discovery or skipped_from_discovery:
                logger.info(
                    "Product discovery каталог: loaded=%s skipped=%s from %s reasons=%s",
                    loaded_from_discovery,
                    skipped_from_discovery,
                    discovery_dir,
                    skip_reasons,
                )
        else:
            logger.info("PRODUCT_DISCOVERY_OUTPUT_DIR не съществува или не е mount-нат: %s", discovery_dir)

        source_priority = {
            "datasheet": 100,
            "hpe_product_bulletin": 95,
            "product_bulletin": 95,
            "quick_specs": 90,
            "quickspecs": 90,
            "local_datasheet": 80,
            "local_catalog": 50,
            "local_demo_catalog": 40,
        }

        merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        for item in catalog:
            vendor = str(item.get("vendor", "")).strip()
            model = str(item.get("model", "")).strip()
            category = str(item.get("category", "")).strip()

            vendor_l = vendor.lower()
            model_l = model.lower()
            category_l = category.lower()

            if not vendor_l and not model_l:
                continue

            # Merge the same enterprise platform into one catalog row.
            # SKU/accessories should have been filtered before this point.
            key = (vendor_l, model_l, category_l)
            current = merged.get(key)

            if current is None:
                merged[key] = item
                continue

            current_source = str(current.get("product_source") or "").lower()
            item_source = str(item.get("product_source") or "").lower()

            current_priority = source_priority.get(current_source, 10)
            item_priority = source_priority.get(item_source, 10)

            if item_priority > current_priority:
                primary = item
                secondary = current
            else:
                primary = current
                secondary = item

            primary_specs = primary.get("specs_json") if isinstance(primary.get("specs_json"), dict) else {}
            secondary_specs = secondary.get("specs_json") if isinstance(secondary.get("specs_json"), dict) else {}

            # Secondary fills baseline fields; primary evidence/source wins.
            merged_specs = dict(secondary_specs)
            merged_specs.update(primary_specs)
            primary["specs_json"] = merged_specs

            if not primary.get("sku") and secondary.get("sku"):
                primary["sku"] = secondary.get("sku")

            evidences = []
            for source_item in (primary, secondary):
                ev = source_item.get("evidence")
                if isinstance(ev, dict) and ev.get("quote"):
                    evidences.append(ev)

            if evidences:
                primary["evidence"] = evidences[0]
                primary["additional_evidence"] = evidences[1:]

            if not primary.get("discovery") and secondary.get("discovery"):
                primary["discovery"] = secondary.get("discovery")

            primary["merged_sources"] = list(
                dict.fromkeys(
                    [
                        str(primary.get("product_source") or ""),
                        str(secondary.get("product_source") or ""),
                    ]
                )
            )

            merged[key] = primary

        deduped = list(merged.values())

        logger.info("Обединен продуктов каталог: %s продукта", len(deduped))

        max_products = int(os.getenv("MAX_PRODUCT_CATALOG_ITEMS", "100"))
        return deduped[:max_products]

    def _build_sources(self, document: Dict[str, Any]) -> List[Dict[str, Any]]:
        if document.get("sources_reviewed"):
            return document["sources_reviewed"]

        raw_documents = document.get("raw_documents") or []
        sources: List[Dict[str, Any]] = []
        for doc in raw_documents:
            pages = [p.get("page") for p in doc.get("pages", []) if p.get("page") is not None]
            sources.append(
                {
                    "document_name": doc.get("document_name", "unknown"),
                    "document_type": doc.get("document_type", "unknown"),
                    "pages_reviewed": pages,
                    "status": "reviewed" if pages else "no_text",
                    "extraction_quality": doc.get("extraction_quality", "unknown"),
                }
            )
        return sources

    def _split_text(self, text: str, max_chars: int) -> List[str]:
        text = (text or "").strip()
        if not text:
            return []
        paragraphs = [p.strip() for p in re.split(r"\n{2,}|\r\n", text) if p.strip()]
        chunks: List[str] = []
        cur = ""
        for p in paragraphs:
            if len(cur) + len(p) + 2 > max_chars:
                if cur:
                    chunks.append(cur)
                cur = p[:max_chars]
            else:
                cur = f"{cur}\n{p}".strip() if cur else p
        if cur:
            chunks.append(cur)
        return chunks

    def _build_chunks(self, document: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_documents = document.get("raw_documents") or []
        chunks: List[Dict[str, Any]] = []

        def doc_priority(doc: Dict[str, Any]) -> int:
            name = str(doc.get("document_name") or "").lower()
            doc_type = str(doc.get("document_type") or "").lower()

            if doc_type == "technical_specification":
                return 0
            if any(k in name for k in ["техническа спецификация", "технически изисквания", "техн. спецификация"]):
                return 0
            if doc_type in {"evaluation_methodology", "clarification"}:
                return 10
            if doc_type in {"invitation", "estimated_value_argumentation"}:
                return 30
            if doc_type in {"price_offer_template", "declaration_template", "contract_draft"}:
                return 90
            if any(k in name for k in ["ценово предложение", "ценова оферта", "образец цена"]):
                return 90
            return 40

        ordered_docs = sorted(raw_documents, key=doc_priority)
        has_technical_docs = any(doc_priority(doc) == 0 for doc in ordered_docs)

        # If a technical specification exists, do not spend LLM context on price templates.
        if has_technical_docs:
            ordered_docs = [doc for doc in ordered_docs if doc_priority(doc) < 90]

        for doc in ordered_docs:
            for page in doc.get("pages", []):
                page_text = page.get("text") or ""
                for idx, part in enumerate(self._split_text(page_text, self.max_chunk_chars), start=1):
                    chunks.append(
                        {
                            "document_name": doc.get("document_name", "unknown"),
                            "document_type": doc.get("document_type", "unknown"),
                            "page": page.get("page"),
                            "section": page.get("section"),
                            "chunk_id": f"{doc.get('document_name', 'unknown')}:p{page.get('page')}:c{idx}",
                            "text": part,
                        }
                    )
                    if len(chunks) >= self.max_chunks:
                        return chunks

        # Fallback for old DocumentProcessor output only with combined_text.
        if not chunks and document.get("combined_text"):
            combined = document.get("combined_text", "")[: self.max_text_chars]
            for idx, part in enumerate(self._split_text(combined, self.max_chunk_chars), start=1):
                chunks.append(
                    {
                        "document_name": "combined_text",
                        "document_type": "legacy_combined_text",
                        "page": None,
                        "section": None,
                        "chunk_id": f"combined_text:c{idx}",
                        "text": part,
                    }
                )
                if len(chunks) >= self.max_chunks:
                    break

        return chunks

    @staticmethod
    def _requirement_category(text: str) -> str:
        t = (text or "").lower()
        if any(k in t for k in ["процесор", "processor", "cpu", "ядра", "core", "xeon", "epyc"]):
            return "CPU"
        if any(k in t for k in ["памет", "ram", "ddr", "memory"]):
            return "RAM"
        if any(k in t for k in ["диск", "ssd", "hdd", "nvme", "storage", "raid"]):
            return "Storage"
        if any(k in t for k in ["мреж", "network", "gbe", "sfp", "rj45", "ethernet"]):
            return "Network"
        if any(k in t for k in ["гаранц", "warranty", "onsite", "поддръж"]):
            return "Warranty"
        if any(k in t for k in ["сертифик", "iso", "ce", "стандарт"]):
            return "Certification"
        if any(k in t for k in ["софтуер", "software", "лиценз"]):
            return "Software"
        if any(k in t for k in ["сервиз", "service", "поддръжка"]):
            return "Service"
        if any(k in t for k in ["доставка", "срок"]):
            return "Delivery"
        return "Other"

    @staticmethod
    def _extract_threshold(text: str) -> Optional[str]:
        patterns = [
            r"(?:минимум|най-малко|не по-малко от|до|максимум)\s*[^\n.;]{0,80}",
            r"\b\d+[\.,]?\d*\s*(?:gb|tb|mb|ghz|mhz|gbe|u|ядра|месеца|дни|бр\.?|%)\b",
            r"\b\d+\s*x\s*\d+[\.,]?\d*\s*(?:gb|tb|gbe)\b",
        ]
        hits: List[str] = []
        for pattern in patterns:
            hits.extend(re.findall(pattern, text or "", flags=re.IGNORECASE))
        return "; ".join(dict.fromkeys([h.strip() for h in hits if h.strip()]))[:250] or None

    @staticmethod
    def _technical_line_candidates(text: str) -> List[str]:
        candidates: List[str] = []
        raw_lines = re.split(r"\n+", text or "")
        buffer = ""

        for line in raw_lines:
            line = re.sub(r"\s+", " ", line).strip(" -–—\t")
            if not line:
                continue

            starts_new = bool(re.match(r"^(?:\d+(?:\.\d+)*[\.)]?|[a-zа-я]\)|[-•])\s+", line, flags=re.I))
            has_tech_kw = bool(re.search(
                r"процесор|processor|cpu|ядра|ram|памет|ddr|ssd|nvme|диск|storage|raid|мреж|network|gbe|sfp|rj45|гаранц|onsite|сертифик|софтуер|лиценз|доставка|срок|захран|power",
                line,
                flags=re.I,
            ))

            if starts_new or has_tech_kw:
                if buffer:
                    candidates.append(buffer.strip())
                buffer = line
            elif buffer and len(buffer) < 400:
                buffer += " " + line

        if buffer:
            candidates.append(buffer.strip())

        clean: List[str] = []
        seen = set()
        for c in candidates:
            c = re.sub(r"^(?:\d+(?:\.\d+)*[\.)]?|[a-zа-я]\)|[-•])\s+", "", c, flags=re.I).strip()
            if len(c) < 12 or len(c) > 700:
                continue
            if not re.search(r"процесор|processor|cpu|ядра|ram|памет|ddr|ssd|nvme|диск|storage|raid|мреж|network|gbe|sfp|rj45|гаранц|onsite|сертифик|софтуер|лиценз|доставка|срок|захран|power", c, flags=re.I):
                continue
            key = c.lower()
            if key in seen:
                continue
            seen.add(key)
            clean.append(c)
        return clean

    def _heuristic_extract_requirements(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Extract requirements deterministically.

        Priority order:
        1. Position-based technical specification extractor for Bulgarian ZOP specs
           anchored with "Поз. X.Y.". This prevents document headings, definitions,
           and broken PDF continuation fragments from becoming REQ-001/REQ-002.
        2. Legacy heuristic line extraction for documents that do not use "Поз.".
        """
        structured_requirements = self._extract_structured_technical_requirements(chunks)
        if structured_requirements:
            return structured_requirements

        technical_chunks = [
            c for c in chunks
            if c.get("document_type") == "technical_specification"
            or "техничес" in str(c.get("document_name", "")).lower()
        ]
        if not technical_chunks:
            technical_chunks = chunks

        requirements: List[Dict[str, Any]] = []
        for chunk in technical_chunks:
            for candidate in self._technical_line_candidates(chunk.get("text", "")):
                # Do not allow broken definition/procedure fragments to become requirements
                # in documents where the real technical requirements are position-anchored.
                if "Поз." in str(chunk.get("text", "")) and "Поз." not in candidate:
                    continue

                req_id = f"REQ-{len(requirements) + 1:03d}"
                requirements.append(
                    {
                        "id": req_id,
                        "category": self._requirement_category(candidate),
                        "original_text": candidate,
                        "normalized_requirement": candidate,
                        "mandatory": True,
                        "threshold": self._extract_threshold(candidate),
                        "source": {
                            "document_name": chunk.get("document_name"),
                            "page": chunk.get("page"),
                            "section": chunk.get("section"),
                            "chunk_id": chunk.get("chunk_id"),
                            "quote": candidate,
                        },
                    }
                )
                if len(requirements) >= 60:
                    return requirements
        return requirements

    @staticmethod
    def _vendor_lock_indicator_for_requirement(req: Dict[str, Any]) -> Dict[str, Any]:
        text = f"{req.get('original_text', '')} {req.get('normalized_requirement', '')}".lower()
        risk: RiskLevel = "LOW"
        indicator_type = "other"
        reasoning = "Изискването е извлечено като технически параметър; не е открит очевиден vendor-lock индикатор."

        brand_pattern = r"\b(dell|hpe|hewlett|packard|lenovo|cisco|fortinet|juniper|vmware|microsoft|oracle|ibm|netapp|pure storage|emc)\b"
        if re.search(brand_pattern, text, flags=re.I) and "еквивалент" not in text:
            risk = "HIGH"
            indicator_type = "brand_reference"
            reasoning = "Открита е референция към конкретен производител без ясно допускане на еквивалент."
        elif re.search(r"\b[A-Z]{2,}[A-Z0-9\-]{3,}\b", req.get("original_text", "")) and "еквивалент" not in text:
            risk = "MEDIUM"
            indicator_type = "model_reference"
            reasoning = "Има моделоподобна/part-number формулировка, която трябва да се провери за ограничителен ефект."
        elif "или еквивалент" not in text and any(k in text for k in ["intel xeon", "amd epyc", "vmware", "windows server"]):
            risk = "MEDIUM"
            indicator_type = "unclear_equivalent"
            reasoning = "Посочена е конкретна технология/платформа и не е ясно дали се допуска еквивалент."
        elif "или еквивалент" in text or "еквивалент" in text:
            risk = "LOW"
            indicator_type = "equivalent_allowed"
            reasoning = "Изискването съдържа допускане на еквивалент, което намалява риска от заключване."

        return {
            "id": "",
            "requirement_id": req.get("id"),
            "indicator_type": indicator_type,
            "risk": risk,
            "reasoning": reasoning,
            "evidence": req.get("source", {}),
        }

    @staticmethod
    def _product_text(product: Dict[str, Any]) -> str:
        specs = product.get("specs_json") or {}
        if isinstance(specs, dict):
            specs_text = " ".join(f"{k}: {v}" for k, v in specs.items())
        else:
            specs_text = str(specs)

        evidence = product.get("evidence") or {}
        evidence_quote = ""
        if isinstance(evidence, dict):
            evidence_quote = str(evidence.get("quote") or "")

        discovery = product.get("discovery") or {}
        discovery_text = ""
        if isinstance(discovery, dict):
            discovery_text = " ".join(
                str(discovery.get(k) or "")
                for k in ("req_id", "score", "confidence", "compliance", "gaps", "source_file")
            )

        return " ".join(
            str(x)
            for x in [
                product.get("vendor", ""),
                product.get("model", ""),
                product.get("sku", ""),
                product.get("category", ""),
                product.get("product_source", ""),
                product.get("datasheet_url", ""),
                specs_text,
                evidence_quote,
                discovery_text,
            ]
            if x
        ).lower()

    @staticmethod
    def _product_spec_value_for_requirement(req: Dict[str, Any], product: Dict[str, Any]) -> str:
        """
        Return the most relevant product catalog value for a requirement.
        This is intentionally deterministic: no compliance claim is made here;
        it only selects catalog evidence suitable for a comparative table cell.
        """
        specs = product.get("specs_json") or {}
        category = str(req.get("category") or req.get("component") or "").lower()
        parameter = str(req.get("parameter") or "").lower()
        req_text = " ".join(
            str(x)
            for x in [
                req.get("normalized_requirement"),
                req.get("requirement_text"),
                req.get("text"),
                req.get("original_text"),
            ]
            if x
        ).lower()

        terms_by_category = {
            "cpu": ["cpu", "processor", "процесор", "core", "ядр", "ghz", "cache"],
            "processor": ["cpu", "processor", "процесор", "core", "ядр", "ghz", "cache"],
            "ram": ["ram", "memory", "памет", "ddr", "gb/s", "bandwidth"],
            "memory": ["ram", "memory", "памет", "ddr", "gb/s", "bandwidth"],
            "storage": ["ssd", "nvme", "disk", "drive", "storage", "диск", "tb", "dwpd", "read", "write"],
            "gpu": ["gpu", "accelerator", "nvidia", "графич", "ускорител", "cuda"],
            "network": ["network", "ethernet", "nic", "qsfp", "sfp", "gbe", "порт", "controller"],
            "ethernet": ["network", "ethernet", "nic", "qsfp", "sfp", "gbe", "порт", "controller"],
            "infiniband": ["infiniband", "ib", "xdr", "osfp", "nvidia"],
            "power": ["power", "psu", "supply", "захран", "watt", "redundant", "220", "240"],
            "cooling": ["cool", "cooling", "liquid", "dlc", "d2c", "cold", "охлаж"],
            "management": ["management", "ipmi", "kvm", "ilo", "idrac", "xclarity", "remote"],
            "warranty": ["warranty", "support", "service", "гаранц", "поддръж"],
            "service": ["warranty", "support", "service", "гаранц", "поддръж"],
            "delivery": ["delivery", "доставка", "срок"],
        }

        terms = set(terms_by_category.get(category, []))
        for key in terms_by_category:
            if key in parameter or key in req_text:
                terms.update(terms_by_category[key])

        if isinstance(specs, dict):
            matched = []
            for key, value in specs.items():
                key_l = str(key).lower()
                value_l = str(value).lower()
                if terms and any(t in key_l or t in value_l for t in terms):
                    matched.append(f"{key}: {value}")

            if matched:
                return "; ".join(matched)[:800]

            # Fallback: compact full catalog entry when no exact field can be selected.
            return "; ".join(f"{k}: {v}" for k, v in specs.items())[:800]

        return str(specs)[:800]

    @staticmethod
    def _coverage_for_requirement(req: Dict[str, Any], product: Dict[str, Any]) -> Dict[str, Any]:
        req_text = " ".join(
            str(x)
            for x in [
                req.get("category"),
                req.get("component"),
                req.get("parameter"),
                req.get("normalized_requirement"),
                req.get("requirement_text"),
                req.get("text"),
            ]
            if x
        ).lower()
        prod_text = VendorAnalyzer._product_text(product)
        product_spec_value = VendorAnalyzer._product_spec_value_for_requirement(req, product)

        category_terms = {
            "cpu": ["cpu", "processor", "xeon", "epyc", "процесор", "core", "ядр"],
            "processor": ["cpu", "processor", "xeon", "epyc", "процесор", "core", "ядр"],
            "ram": ["ram", "memory", "ddr", "памет"],
            "memory": ["ram", "memory", "ddr", "памет"],
            "storage": ["ssd", "nvme", "storage", "raid", "disk", "drive", "диск"],
            "gpu": ["gpu", "nvidia", "accelerator", "графич", "ускорител"],
            "network": ["network", "gbe", "sfp", "qsfp", "rj45", "ethernet", "мреж", "nic"],
            "ethernet": ["network", "gbe", "sfp", "qsfp", "rj45", "ethernet", "мреж", "nic"],
            "infiniband": ["infiniband", "xdr", "osfp", "nvidia"],
            "power": ["power", "psu", "supply", "захран", "watt", "redundant", "220", "240"],
            "cooling": ["cooling", "liquid", "dlc", "d2c", "cold", "охлаж"],
            "management": ["management", "ipmi", "kvm", "ilo", "idrac", "xclarity", "remote"],
            "warranty": ["warranty", "onsite", "support", "гаранц"],
            "service": ["service", "support", "сервиз", "поддръж"],
            "delivery": ["delivery", "доставка", "срок"],
            "software": ["software", "license", "лиценз", "софтуер"],
        }

        category = str(req.get("category") or req.get("component") or "").lower()
        terms = set(category_terms.get(category, []))
        for key, values in category_terms.items():
            if key in req_text:
                terms.update(values)

        hits = sum(1 for t in terms if t in prod_text)

        if category == "delivery":
            status = "unknown"
            explanation = "Срокът за доставка не може да се докаже от продуктов datasheet; изисква се търговска проверка."
        elif hits >= 2:
            status = "partially_meets"
            explanation = f"Каталогова стойност за проверка: {product_spec_value}" if product_spec_value else "Нужна е конфигурационна проверка по конкретен SKU/BOM."
        elif hits == 1:
            status = "unknown"
            explanation = f"Възможна каталогова следа: {product_spec_value}" if product_spec_value else "Има частично съвпадение по категория, но няма достатъчно evidence за съответствие."
        else:
            status = "unknown"
            explanation = "Няма достатъчно данни в продуктовия каталог за това изискване."

        evidence = product.get("evidence") or {
            "document_name": product.get("product_source") or "product_catalog",
            "page": None,
            "section": None,
            "chunk_id": None,
            "quote": product_spec_value or str(product.get("specs_json", ""))[:500],
        }
        evidence.setdefault("section", None)
        evidence.setdefault("chunk_id", None)
        evidence.setdefault("quote", product_spec_value or str(product.get("specs_json", ""))[:500])

        return {
            "requirement_id": req.get("id"),
            "status": status,
            "product_spec": product_spec_value or str(product.get("specs_json", ""))[:500],
            "explanation": explanation,
            "evidence": evidence,
        }


    def _is_real_technical_requirement(self, req: Dict[str, Any]) -> bool:
        """
        Returns True only for requirements that contain actual technical/product parameters.
        This prevents product matching against EOP metadata, delivery schedules, procedure text,
        invitation headings, and other non-technical content.
        """
        category = str(req.get("category", "")).strip().lower()

        text_parts = [
            req.get("normalized_requirement"),
            req.get("requirement"),
            req.get("text"),
            req.get("description"),
            req.get("quote"),
        ]

        source = req.get("source") or {}
        if isinstance(source, dict):
            text_parts.extend([
                source.get("quote"),
                source.get("section"),
                source.get("document_name"),
            ])

        text = " ".join(str(x) for x in text_parts if x).strip().lower()

        non_technical_categories = {
            "delivery",
            "procedure",
            "metadata",
            "administrative",
            "schedule",
            "contract",
            "price",
            "invitation",
            "procurement",
        }

        if category in non_technical_categories:
            return False

        procedural_keywords = [
            "пазарни консултации",
            "вид на процедурата",
            "процедура само за публикуване",
            "свързани процедури",
            "стратегическа поръчка",
            "описание на вида стратегическа поръчка",
            "експортиран на",
            "график",
            "основни параметри",
            "директивата за чистите превозни средства",
            "наличие на критерии",
            "намаление на екологичното въздействие",
            "попълва се",
        ]

        if any(k in text for k in procedural_keywords):
            return False

        technical_categories = {
            "cpu",
            "processor",
            "ram",
            "memory",
            "storage",
            "disk",
            "gpu",
            "network",
            "nic",
            "psu",
            "power",
            "cooling",
            "server",
            "workstation",
            "computer",
            "hardware",
            "software",
            "license",
            "backup",
            "tape",
            "tape_library",
        }

        technical_keywords = [
            "cpu", "processor", "процесор", "ядра", "ядро", "cores", "core", "ghz", "mhz",
            "ram", "memory", "памет", "ddr4", "ddr5", "ecc",
            "ssd", "nvme", "hdd", "storage", "диск", "дисков", "капацитет", "tb", "gb",
            "gpu", "video", "видеокарта", "графична", "nvidia", "rtx", "cuda",
            "psu", "power supply", "захранване", "watt", "ват", "w", "redundant",
            "raid", "controller", "контролер",
            "nic", "ethernet", "gbe", "10gbe", "25gbe", "100gbe", "200g", "400g", "800g", "sfp", "sfp+", "qsfp", "qsfp56", "qsfp112", "osfp", "infiniband", "xdr",
            "ilo", "idrac", "xclarity", "ipmi", "kvm",
            "pcie", "dwpd", "gb/s", "direct-to-chip", "d2c", "cold-plate", "dlc", "liquid", "течно охлаждане",
            "lto", "tape", "лента", "ленти", "drive", "slot", "слот",
            "rack", "tower", "form factor", "u rack",
            "операционна система", "os", "license", "лиценз",
        ]

        has_technical_keyword = any(k in text for k in technical_keywords)

        if category in technical_categories and has_technical_keyword:
            return True

        return has_technical_keyword

    def _filter_real_technical_requirements(self, requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            req for req in requirements
            if self._is_real_technical_requirement(req)
        ]


    def _is_valid_requirement_text(self, req: Dict[str, Any]) -> bool:
        """
        Reject broken sentence fragments produced by PDF text extraction.
        A valid requirement must be long enough and must contain either:
        - a measurable/technical parameter, or
        - requirement wording such as minimum/must/shall/следва/трябва.
        """
        text_value = str(
            req.get("normalized_requirement")
            or req.get("requirement")
            or req.get("text")
            or req.get("quote")
            or ""
        ).strip()

        text_l = text_value.lower()

        if len(text_l) < 35:
            return False

        # Typical broken tails from wrapped PDF text.
        bad_starts = [
            "в ",
            "и ",
            "или ",
            "както ",
            "като ",
            "за ",
            "на ",
            "от ",
            "с ",
            "по ",
        ]

        if any(text_l.startswith(prefix) for prefix in bad_starts) and len(text_l) < 90:
            return False

        # Fragment without a real predicate/requirement signal.
        requirement_signals = [
            "минимум",
            "минимален",
            "минимална",
            "минимални",
            "трябва",
            "следва",
            "изисква",
            "изискване",
            "да поддържа",
            "да има",
            "да бъде",
            "не по-малко",
            "не по-ниска",
            "поне",
            "сертификат",
            "сертифициран",
            "стандарт",
            "съвместим",
            "pfc",
            "power factor",
            "процесор",
            "cpu",
            "ram",
            "памет",
            "ssd",
            "nvme",
            "gpu",
            "захранване",
            "ethernet",
            "гаранция",
        ]

        has_signal = any(signal in text_l for signal in requirement_signals)
        has_number_or_unit = bool(re.search(r"\d+|gb|tb|ghz|mhz|w\b|ват|core|ядр", text_l, re.IGNORECASE))

        if not has_signal and not has_number_or_unit:
            return False

        return True

    def _filter_valid_requirements(self, requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        valid: List[Dict[str, Any]] = []
        removed = 0

        for req in requirements:
            if self._is_valid_requirement_text(req):
                valid.append(req)
            else:
                removed += 1

        if removed:
            logger.info("Requirement quality filter: removed=%s kept=%s", removed, len(valid))

        # Re-number legacy REQ IDs after filtering so report does not have gaps,
        # but preserve structured position IDs such as POS-1.3 from TechnicalSpecExtractor.
        legacy_idx = 1
        for req in valid:
            if str(req.get("id", "")).startswith("POS-"):
                continue
            req["id"] = f"REQ-{legacy_idx:03d}"
            legacy_idx += 1

        return valid

    def _heuristic_products(self, requirements: List[Dict[str, Any]], product_catalog: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        products: List[Dict[str, Any]] = []
        matrix: List[Dict[str, Any]] = []
        if not requirements or not product_catalog:
            return products, matrix
        hardware_indicators = [
            "cpu", "процесор", "ядра", "ghz", "ram", "памет", "ddr", "ssd", "nvme",
            "gpu", "видеокарта", "захранване", "watt", "raid", "ethernet", "gbe",
            "server", "сървър", "rack", "tower",
        ]
        req_text_combined = " ".join(
            str(r.get("normalized_requirement") or r.get("original_text") or "")
            for r in requirements
        ).lower()
        if not any(kw in req_text_combined for kw in hardware_indicators):
            logger.info("Product matching skipped: no hardware indicators in requirements (food/construction/services tender)")
            return products, matrix

        for product in product_catalog[:10]:
            coverage = [self._coverage_for_requirement(req, product) for req in requirements]
            known = [c for c in coverage if c.get("status") != "unknown"]
            coverage_score = len(known) / max(len(coverage), 1)

            product_text = self._product_text(product)
            product_category = str(product.get("category") or "").lower()
            source = str(product.get("product_source") or "").lower()
            vendor = str(product.get("vendor") or "").lower()
            model = str(product.get("model") or "").lower()

            relevance_score = 0.0
            if product_category in {"server", "servers"} or "server" in product_text:
                relevance_score += 0.2
            if any(v in vendor for v in ["hpe", "hewlett", "dell", "lenovo"]):
                relevance_score += 0.1
            if any(m in model for m in ["proliant", "poweredge", "thinksystem", "dl380", "r760", "sr650"]):
                relevance_score += 0.15
            if source in {"datasheet", "product_bulletin", "hpe_product_bulletin", "quick_specs", "quickspecs"}:
                relevance_score += 0.1

            score = round(min(1.0, max(coverage_score, relevance_score)), 2)
            products.append(
                {
                    "vendor": product.get("vendor", "unknown"),
                    "model": product.get("model", "unknown"),
                    "category": product.get("category"),
                    "product_source": product.get("product_source") or product.get("datasheet_url"),
                    "match_score": score,
                    "coverage": coverage,
                    "overall_comment": "Heuristic product matching. Финалното съответствие трябва да се потвърди с datasheet/BoM конфигурация.",
                }
            )

        for req in requirements:
            row_products: List[Dict[str, Any]] = []

            for product_row in products:
                coverage_item = next(
                    (
                        coverage_entry
                        for coverage_entry in product_row.get("coverage", [])
                        if coverage_entry.get("requirement_id") == req.get("id")
                    ),
                    {},
                )

                row_products.append(
                    {
                        "vendor": product_row.get("vendor"),
                        "model": product_row.get("model"),
                        "product_source": product_row.get("product_source"),
                        "status": coverage_item.get("status", "unknown"),
                        "product_spec": coverage_item.get("product_spec", ""),
                        "comment": coverage_item.get("explanation", "unknown"),
                        "explanation": coverage_item.get("explanation", "unknown"),
                        "evidence": coverage_item.get("evidence"),
                    }
                )

            matrix.append(
                {
                    "requirement_id": req.get("id"),
                    "component": req.get("category") or req.get("component"),
                    "parameter": req.get("parameter"),
                    "requirement": (
                        req.get("normalized_requirement")
                        or req.get("requirement_text")
                        or req.get("text")
                        or req.get("original_text")
                    ),
                    "products": row_products,
                }
            )

        return products, matrix

    def _heuristic_analysis(
        self,
        document: Dict[str, Any],
        chunks: List[Dict[str, Any]],
        sources: List[Dict[str, Any]],
        product_catalog: List[Dict[str, Any]],
        reason: str,
    ) -> Dict[str, Any]:
        title = document.get("title", "Непознато")
        url = document.get("procurement_url", "")
        requirements = self._filter_valid_requirements(
            self._heuristic_extract_requirements(chunks)
        )

        indicators: List[Dict[str, Any]] = []
        for req in requirements:
            ind = self._vendor_lock_indicator_for_requirement(req)
            ind["id"] = f"VL-{len(indicators) + 1:03d}"
            indicators.append(ind)

        if any(str(req.get("id", "")).startswith("POS-") for req in requirements):
            # Position-based requirements are already scoped technical server requirements.
            # Do not drop rows before building the comparison matrix.
            technical_requirements = requirements
        else:
            technical_requirements = self._filter_real_technical_requirements(requirements)
        product_matching_skipped = False
        product_matching_skip_reason = ""

        if requirements and not technical_requirements:
            product_matching_skipped = True
            product_matching_skip_reason = (
                "Не са открити достатъчно технически изисквания за коректно продуктово съпоставяне. "
                "Откритите requirements са процедурни/метаданни, а не техническа спецификация."
            )
            candidate_products, matrix = [], []
        else:
            candidate_products, matrix = self._heuristic_products(technical_requirements, product_catalog)

        high = any(i.get("risk") == "HIGH" for i in indicators)
        medium = any(i.get("risk") == "MEDIUM" for i in indicators)
        risk: RiskLevel = "INSUFFICIENT_EVIDENCE"
        if requirements:
            risk = "HIGH" if high else "MEDIUM" if medium else "LOW"

        why_not_locked: List[Dict[str, Any]] = []
        if requirements and not high:
            evidence = requirements[0].get("source", {})
            why_not_locked.append(
                {
                    "claim": "Не е открита директна референция към конкретен производител/модел в извлечените технически изисквания.",
                    "supporting_requirement_ids": [r.get("id") for r in requirements[:10]],
                    "evidence": evidence,
                }
            )

        residual_risks: List[Dict[str, Any]] = []
        for req in requirements:
            txt = req.get("normalized_requirement", "").lower()
            if any(k in txt for k in ["гаранц", "onsite", "сервиз", "срок", "доставка"]):
                residual_risks.append(
                    {
                        "risk": "Оперативно/търговско ограничение",
                        "reason": "Изискването може да ограничи участници според сервизно покритие, SLA или срок за доставка.",
                        "recommended_action": "Да се провери дали условието е обективно обосновано и дали всички потенциални участници могат да го покрият.",
                        "evidence": req.get("source"),
                    }
                )

        if any(str(req.get("id", "")).startswith("POS-") for req in requirements):
            limitations = [
                "Използван е TechnicalSpecExtractor по позиции 'Поз. X.Y.' вместо generic REQ chunk extraction.",
            ]
        else:
            limitations = [
                f"LLM backends не върнаха валиден резултат ({reason}); използван е deterministic fallback extraction.",
            ]

        if product_matching_skipped:
            limitations.append(f"Product matching skipped: {product_matching_skip_reason}")
        else:
            limitations.append("Product matching е heuristic и трябва да се потвърди с реални datasheets/BoM конфигурации.")
        if not requirements:
            limitations.append("Не са извлечени технически изисквания от наличните chunks.")
        if not product_catalog:
            limitations.append("Няма подаден продуктов каталог; продуктово съответствие не трябва да се приема за доказано.")

        return {
            "summary": {
                "overall_risk_level": risk,
                "vendor_lock_detected": risk in {"MEDIUM", "HIGH"},
                "brief_summary": "Fallback анализ върху извлечените документи. Резултатът е ориентировъчен и подлежи на валидация.",
                "confidence_score": 0.55 if requirements else 0.2,
            },
            "procurement": {"url": url, "title": title},
            "sources_reviewed": sources,
            "requirements": requirements,
            "vendor_lock_indicators": indicators,
            "why_not_locked": why_not_locked,
            "residual_risks": residual_risks,
            "candidate_products": candidate_products,
            "product_comparison_matrix": matrix,
            "recommendations": self._default_recommendations(risk),
            "confidence": {"score": 0.55 if requirements else 0.2, "limitations": limitations},
            "debug": {},
            "hardware_specifications": [],
            "software_requirements": [],
            "certifications_and_standards": [],
            "indirect_indicators": [],
            "compliant_reformulations": [],
        }

    def _post_process(
        self,
        analysis: Dict[str, Any],
        document: Dict[str, Any],
        used_backend: str,
        chunks_count: int,
        product_count: int,
    ) -> Dict[str, Any]:
        title = document.get("title", "Непознато")
        url = document.get("procurement_url", "")
        sources = self._build_sources(document)

        if not analysis:
            analysis = _empty_analysis("Всички LLM backends са недостъпни или върнаха невалиден JSON", title, url)

        analysis.setdefault("summary", {})
        analysis.setdefault("procurement", {"url": url, "title": title})
        analysis.setdefault("sources_reviewed", sources)
        analysis.setdefault("requirements", [])
        if any(str(req.get("id", "")).startswith("POS-") for req in analysis.get("requirements", []) if isinstance(req, dict)):
            analysis["requirements"] = self._filter_position_requirements(analysis.get("requirements", []))
        analysis.setdefault("vendor_lock_indicators", [])
        analysis.setdefault("why_not_locked", [])
        analysis.setdefault("residual_risks", [])
        analysis.setdefault("candidate_products", [])
        analysis.setdefault("product_comparison_matrix", [])
        analysis.setdefault("recommendations", [])
        analysis.setdefault("confidence", {"score": 0.0, "limitations": []})
        analysis.setdefault("debug", {})

        if not analysis.get("sources_reviewed"):
            analysis["sources_reviewed"] = sources

        limitations = analysis["confidence"].setdefault("limitations", [])
        unsupported = 0

        for req in analysis.get("requirements", []):
            if not _source_ok(req):
                req["status"] = "UNSUPPORTED"
                unsupported += 1

        for section in ("vendor_lock_indicators", "why_not_locked", "residual_risks"):
            for item in analysis.get(section, []):
                if item.get("evidence") is None and section == "residual_risks":
                    # residual risk without evidence is allowed only as limitation, not factual conclusion
                    item["status"] = "UNSUPPORTED"
                    unsupported += 1
                elif item.get("evidence") is not None and not _evidence_ok(item):
                    item["status"] = "UNSUPPORTED"
                    unsupported += 1

        for product in analysis.get("candidate_products", []):
            for cov in product.get("coverage", []):
                if cov.get("status") in {"meets", "partially_meets", "does_not_meet"} and not _evidence_ok(cov):
                    cov["status"] = "unknown"
                    cov["explanation"] = (cov.get("explanation") or "") + " Evidence липсва; статусът е променен на unknown."
                    unsupported += 1

        if unsupported:
            limitations.append(f"{unsupported} твърдения нямаха достатъчно evidence и бяха маркирани/понижени.")

        if not analysis.get("sources_reviewed"):
            limitations.append("Няма списък с анализирани документи.")
        if not analysis.get("requirements"):
            limitations.append("Не са извлечени технически изисквания.")
        if product_count == 0 and "Няма подаден продуктов каталог; продуктово съответствие не трябва да се приема за доказано." not in limitations:
            limitations.append("Няма подаден продуктов каталог; продуктово съответствие не трябва да се приема за доказано.")

        risk = self._deterministic_risk(analysis)
        analysis["summary"]["overall_risk_level"] = risk
        analysis["summary"]["vendor_lock_detected"] = risk in {"MEDIUM", "HIGH"}
        analysis["summary"].setdefault("brief_summary", "")
        if not analysis["summary"].get("brief_summary"):
            analysis["summary"]["brief_summary"] = self._default_summary(risk)

        score = self._calculate_confidence(analysis, chunks_count, product_count)
        if used_backend not in {"heuristic_fallback", "technical_spec_extractor", "none"}:
            score = max(score, 0.80)
        analysis["confidence"]["score"] = score
        analysis["summary"]["confidence_score"] = score

        analysis["debug"].update(
            {
                "documents_analyzed": len(sources),
                "chunks_analyzed": chunks_count,
                "product_catalog_items": product_count,
                "backend": used_backend,
            }
        )

        # Keep old renderer compatibility keys.
        analysis.setdefault("hardware_specifications", [])
        analysis.setdefault("software_requirements", [])
        analysis.setdefault("certifications_and_standards", [])
        analysis.setdefault("indirect_indicators", [])
        analysis.setdefault("compliant_reformulations", [])

        if not analysis.get("recommendations"):
            analysis["recommendations"] = self._default_recommendations(risk)

        return analysis

    def _deterministic_risk(self, analysis: Dict[str, Any]) -> RiskLevel:
        requirements = analysis.get("requirements") or []
        indicators = analysis.get("vendor_lock_indicators") or []
        limitations = " ".join(analysis.get("confidence", {}).get("limitations", [])).lower()

        if not requirements:
            return "INSUFFICIENT_EVIDENCE"
        if "не са извлечени технически изисквания" in limitations:
            return "INSUFFICIENT_EVIDENCE"

        if any(i.get("risk") == "HIGH" and i.get("status") != "UNSUPPORTED" for i in indicators):
            return "HIGH"
        if any(i.get("risk") == "MEDIUM" and i.get("status") != "UNSUPPORTED" for i in indicators):
            return "MEDIUM"
        if any(i.get("risk") == "INSUFFICIENT_EVIDENCE" for i in indicators):
            return "INSUFFICIENT_EVIDENCE"
        return "LOW"

    def _calculate_confidence(self, analysis: Dict[str, Any], chunks_count: int, product_count: int) -> float:
        score = 1.0
        if chunks_count == 0:
            score -= 0.5
        if not analysis.get("requirements"):
            score -= 0.4
        if not analysis.get("sources_reviewed"):
            score -= 0.2
        if product_count == 0:
            score -= 0.1

        unsupported = 0
        total = 0
        for section in ("requirements", "vendor_lock_indicators", "why_not_locked", "residual_risks"):
            for item in analysis.get(section, []):
                total += 1
                if item.get("status") == "UNSUPPORTED":
                    unsupported += 1
        if total:
            score -= min(0.3, (unsupported / total) * 0.3)

        risk = analysis.get("summary", {}).get("overall_risk_level")
        if risk == "INSUFFICIENT_EVIDENCE":
            score = min(score, 0.3)
        if any("deterministic fallback" in str(x).lower() for x in analysis.get("confidence", {}).get("limitations", [])):
            score = min(score, 0.65)

        return round(max(0.0, min(1.0, score)), 2)

    @staticmethod
    def _default_summary(risk: str) -> str:
        if risk == "HIGH":
            return "Открити са силни vendor-lock индикатори, подкрепени с evidence."
        if risk == "MEDIUM":
            return "Открити са изисквания с потенциален ограничителен ефект; препоръчва се разяснение."
        if risk == "LOW":
            return "Не са открити съществени vendor-lock индикатори в анализираните документи."
        return "Няма достатъчно evidence за надеждна оценка."

    @staticmethod
    def _default_recommendations(risk: str) -> List[Dict[str, str]]:
        if risk == "HIGH":
            return [
                {
                    "type": "challenge_requirement",
                    "recommendation": "Да се разгледа възможност за разяснение, промяна или оспорване на ограничителните изисквания.",
                    "justification": "Оценката е HIGH въз основа на vendor-lock индикатори.",
                }
            ]
        if risk == "MEDIUM":
            return [
                {
                    "type": "ask_clarification",
                    "recommendation": "Да се поискат разяснения по изискванията с остатъчен риск.",
                    "justification": "Има потенциално ограничителни формулировки.",
                }
            ]
        if risk == "LOW":
            return [
                {
                    "type": "participate",
                    "recommendation": "Може да се продължи с техническо продуктовo съпоставяне и подготовка за участие.",
                    "justification": "Не са открити съществени vendor-lock индикатори.",
                }
            ]
        return [
            {
                "type": "ask_clarification",
                "recommendation": "Да не се взема bid/no-bid решение преди допълнително извличане и проверка на документацията.",
                "justification": "Недостатъчна доказателствена база.",
            }
        ]

    def _extract_text_from_chunks(self, chunks):
        parts = []

        if not chunks:
            return ""

        for chunk in chunks:
            if isinstance(chunk, str):
                parts.append(chunk)
                continue

            if isinstance(chunk, dict):
                for key in ("text", "content", "extracted_text", "raw_text"):
                    value = chunk.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value)

        return "\n".join(parts)

    @staticmethod
    def _component_to_category(component: str) -> str:
        component_l = str(component or "").lower()
        mapping = {
            "cpu": "CPU",
            "memory": "RAM",
            "storage": "Storage",
            "gpu": "GPU",
            "ethernet": "Network",
            "infiniband": "Network",
            "power": "Power",
            "cooling": "Cooling",
            "management": "Management",
            "switch_ports": "Network",
            "general": "Other",
        }
        return mapping.get(component_l, component or "Other")

    def _technical_chunks_for_structured_extraction(self, chunks):
        technical_chunks = [
            c for c in chunks or []
            if isinstance(c, dict)
            and (
                str(c.get("document_type") or "").lower() == "technical_specification"
                or "техничес" in str(c.get("document_name") or "").lower()
                or "спецификация" in str(c.get("document_name") or "").lower()
                or "Поз." in str(c.get("text") or "")
            )
        ]
        return technical_chunks or [c for c in chunks or [] if isinstance(c, dict)]

    def _find_source_for_structured_requirement(self, req, chunks):
        position = str(getattr(req, "position", "") or "")
        raw_text = str(getattr(req, "raw_text", "") or "")
        requirement_text = str(getattr(req, "requirement_text", "") or "")

        position_markers = [
            f"Поз. {position}.",
            f"Поз.{position}.",
            f"Поз. {position}",
            f"Поз.{position}",
        ]

        def build_source(chunk):
            return {
                "document_name": chunk.get("document_name"),
                "page": chunk.get("page"),
                "section": chunk.get("section"),
                "chunk_id": chunk.get("chunk_id"),
                "quote": (raw_text or requirement_text)[:900],
            }

        for chunk in chunks or []:
            text_value = str(chunk.get("text") or "")
            if any(marker in text_value for marker in position_markers):
                return build_source(chunk)

        needle = requirement_text[:100].strip()
        if needle:
            for chunk in chunks or []:
                text_value = str(chunk.get("text") or "")
                if needle in text_value:
                    return build_source(chunk)

        fallback = next((c for c in chunks or [] if isinstance(c, dict)), {})
        return build_source(fallback) if fallback else {
            "document_name": "technical_specification",
            "page": None,
            "section": None,
            "chunk_id": None,
            "quote": (raw_text or requirement_text)[:900],
        }

    @staticmethod
    def _normalize_structured_requirement_fields(req: Any) -> Tuple[str, str, str]:
        """
        Correct component/category/parameter after TechnicalSpecExtractor.

        Bulgarian requirements often contain words such as "процесорите" inside a RAM
        requirement, for example "2 TB RAM памет, разделена по равно между процесорите".
        A simple keyword classifier may incorrectly label that as CPU. This normalizer
        applies a precedence order based on the measurable technical subject.
        """
        text = " ".join(
            str(x)
            for x in [
                getattr(req, "requirement_text", ""),
                getattr(req, "raw_text", ""),
                getattr(req, "parameter", ""),
                getattr(req, "component", ""),
            ]
            if x
        ).lower()

        component = str(getattr(req, "component", "") or "general").lower()
        parameter = str(getattr(req, "parameter", "") or "technical_requirement")

        # Precedence matters: RAM requirements may mention processors; cooling may mention CPUs/GPUs.
        if any(k in text for k in ["ram", "оперативна памет", "памет"]):
            component = "memory"
            if "1200" in text and "gb/s" in text:
                parameter = "ram_bandwidth"
            elif "2 tb" in text or "2tb" in text:
                parameter = "ram_capacity"
            elif "разделена" in text and "процесор" in text:
                parameter = "ram_distribution"

        elif any(k in text for k in ["nvme", "запаметяващ", "ssd", "dwpd", "четене", "писане"]):
            component = "storage"
            if "7.68" in text or "7,68" in text:
                parameter = "nvme_capacity"
            elif "dwpd" in text:
                parameter = "nvme_endurance"
            elif "6,700" in text or "6700" in text or "3,600" in text or "3600" in text:
                parameter = "nvme_performance"
            elif "осем" in text or "(8)" in text:
                parameter = "nvme_count_type"

        elif any(k in text for k in ["direct-to-chip", "d2c", "cold-plate", "dlc", "течно охлаждане", "охлаждан"]):
            component = "cooling"
            parameter = "direct_liquid_cooling"

        elif any(k in text for k in ["infiniband", "xdr", "osfp"]):
            component = "infiniband"
            parameter = "infiniband_controller"

        elif any(k in text for k in ["ethernet", "qsfp112", "qsfp56", "200g"]):
            component = "ethernet"
            parameter = "ethernet_controller"

        elif any(k in text for k in ["gpu", "графич", "ускорител"]):
            component = "gpu"
            parameter = "gpu_configuration"

        elif any(k in text for k in ["захранващ", "захранване", "psu", "220", "240v"]):
            component = "power"
            if "резервирана" in text:
                parameter = "psu_redundancy"
            elif "четири" in text or "(4)" in text:
                parameter = "psu_count"
            elif "220" in text and "240" in text:
                parameter = "power_input"

        elif any(k in text for k in ["процесор", "cpu", "ядра", "ghz", "l3 cache"]):
            component = "cpu"
            if "два (2) процесора" in text or "2) процесора" in text:
                parameter = "cpu_count"
            elif "96" in text and "ядра" in text:
                parameter = "cpu_cores_clock_cache"

        elif any(k in text for k in ["ipmi", "kvm", "отдалечен контрол", "remote management"]):
            component = "management"
            parameter = "remote_management"

        category_map = {
            "cpu": "CPU",
            "processor": "CPU",
            "memory": "RAM",
            "ram": "RAM",
            "storage": "Storage",
            "gpu": "GPU",
            "ethernet": "Network",
            "network": "Network",
            "infiniband": "Network",
            "power": "Power",
            "cooling": "Cooling",
            "management": "Management",
        }
        category = category_map.get(component, "Other")
        return component, category, parameter

    def _extract_structured_technical_requirements(self, chunks):
        technical_chunks = self._technical_chunks_for_structured_extraction(chunks)
        text = self._extract_text_from_chunks(technical_chunks)

        if not text or "Поз." not in text:
            return []

        extractor = TechnicalSpecExtractor()
        server_requirements = extractor.extract_server_requirements(text)

        structured = []
        for req in server_requirements:
            component, category, parameter = self._normalize_structured_requirement_fields(req)
            source = self._find_source_for_structured_requirement(req, technical_chunks)
            requirement_text = req.requirement_text

            structured.append(
                {
                    "id": req.id,
                    "position": req.position,
                    "scope": req.scope,
                    "component": component,
                    "category": category,
                    "parameter": parameter,
                    "text": requirement_text,
                    "requirement_text": requirement_text,
                    "original_text": requirement_text,
                    "normalized_requirement": requirement_text,
                    "mandatory": True,
                    "threshold": self._extract_threshold(requirement_text),
                    "raw_text": req.raw_text,
                    "source": source,
                }
            )

        if structured:
            logger.info(
                "TechnicalSpecExtractor: extracted %s server technical requirements",
                len(structured),
            )

        return structured

    def _filter_position_requirements(self, requirements):
        cleaned = []

        for req in requirements or []:
            if not isinstance(req, dict):
                continue

            req_id = str(req.get("id", ""))
            text = str(
                req.get("requirement_text")
                or req.get("text")
                or req.get("description")
                or ""
            )

            if req_id.startswith("POS-") or "Поз." in text:
                cleaned.append(req)

        return cleaned

    def _select_chunks_for_llm(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Select only the most relevant technical chunks for LLM analysis.
        This prevents local Ollama from receiving huge prompts and timing out.
        """
        max_chunks = int(os.getenv("ZOP_MAX_LLM_CHUNKS", "12"))
        max_chars = int(os.getenv("ZOP_MAX_CHUNK_CHARS", "2500"))

        def score(chunk: Dict[str, Any]) -> int:
            doc_type = str(chunk.get("document_type") or "").lower()
            name = str(chunk.get("document_name") or "").lower()
            text_value = str(chunk.get("text") or "").lower()

            s = 0

            if doc_type == "technical_specification":
                s += 120
            if doc_type == "estimated_value_argumentation":
                s += 50

            if "техническа спецификация" in name:
                s += 100
            if "технически" in name:
                s += 40
            if "спецификация" in name:
                s += 40
            if "позиция" in name and "спецификац" in name:
                s += 80
            if "приложение" in name and ("спецификац" in name or "техн" in name):
                s += 60

            technical_terms = [
                "процесор", "cpu", "ядра", "ghz", "ram", "памет", "ddr4", "ddr5",
                "ssd", "nvme", "hdd", "диск", "storage", "gpu", "видеокарта",
                "nvidia", "rtx", "захранване", "psu", "watt", "ват", "raid",
                "ethernet", "gbe", "sfp", "lto", "лента", "слот", "гаранция",
                "поддръжка", "сертификат", "стандарт", "pfc", "power factor",
                "монитор", "дисплей", "принтер", "скенер", "мфу", "таблет",
                "лаптоп", "преносим", "настолен", "сървър", "мрежов", "wi-fi",
                "bluetooth", "usb", "hdmi", "displayport", "резолюция", "diagonal",
                "диагонал", "батерия", "акумулатор", "клавиатура", "мишка",
            ]
            s += sum(3 for term in technical_terms if term in text_value)

            low_priority_terms = [
                "ценово предложение",
                "образец ценово",
                "техническо предложение",
                "покана за предоставяне",
                "индикативни оферти",
                "график",
                "параметри на поръчката",
            ]
            if any(term in name for term in low_priority_terms):
                s -= 100

            return s

        selected = sorted(chunks, key=score, reverse=True)[:max_chunks]

        slimmed: List[Dict[str, Any]] = []
        for chunk in selected:
            c = dict(chunk)
            txt = str(c.get("text") or "")
            if len(txt) > max_chars:
                c["text"] = txt[:max_chars] + "\n...[TRUNCATED_FOR_LLM]..."
            slimmed.append(c)

        return slimmed

    def analyze(self, document: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(document, dict):
            logger.error("VendorAnalyzer received invalid document object: %r", document)
            title = "Непознато"
            url = ""
            analysis = _empty_analysis(
                "Неуспешно извличане на документацията: document object is None или невалиден.",
                title,
                url,
            )
            analysis.setdefault("limitations", []).append(
                "Анализът е прекратен, защото document processor не върна валиден document object."
            )
            return {
                "procurement_url": url,
                "title": title,
                "analysis": analysis,
                "analyzed_by": "document_extraction_failed",
            }

        title = document.get("title", "Непознато")
        url = document.get("procurement_url", "")

        chunks = self._build_chunks(document)
        sources = self._build_sources(document)
        product_catalog = self._load_product_catalog()

        if not chunks:
            analysis = _empty_analysis("Няма извлечен текст от документите", title, url)
            return {
                "procurement_url": url,
                "title": title,
                "analysis": analysis,
                "analyzed_by": "none",
            }

        structured_requirements = self._extract_structured_technical_requirements(chunks)
        if structured_requirements:
            logger.info(
                "Structured technical requirements found: %s — still attempting LLM analysis",
                len(structured_requirements),
            )

        llm_chunks = self._select_chunks_for_llm(chunks)
        if len(llm_chunks) != len(chunks):
            logger.info(
                "LLM chunk slimming: original=%s selected=%s",
                len(chunks),
                len(llm_chunks),
            )

        prompt = ANALYSIS_PROMPT.format(
            procurement_url=url,
            title=title,
            sources_json=json.dumps(sources, ensure_ascii=False, indent=2),
            chunks_json=json.dumps(llm_chunks, ensure_ascii=False, indent=2),
            product_catalog_json=json.dumps(product_catalog, ensure_ascii=False, indent=2),
        )

        logger.info("Анализ: %s | chunks=%s/%s | products=%s", title[:80], len(llm_chunks), len(chunks), len(product_catalog))
        analysis, used_backend = self._run_with_fallback(prompt)
        if not analysis:
            analysis = self._heuristic_analysis(
                document=document,
                chunks=chunks,
                sources=sources,
                product_catalog=product_catalog,
                reason="all_llm_backends_failed_or_invalid_json",
            )
            used_backend = "heuristic_fallback"
        elif structured_requirements and not analysis.get("requirements"):
            logger.info("LLM returned no requirements — enriching with %s structured requirements", len(structured_requirements))
            analysis["requirements"] = structured_requirements
        analysis = self._post_process(analysis, document, used_backend, len(chunks), len(product_catalog))

        risk = analysis.get("summary", {}).get("overall_risk_level", "?")
        logger.info("Резултат: риск=%s | модел=%s | %s", risk, used_backend, title[:80])

        return {
            "procurement_url": url,
            "title": title,
            "analysis": analysis,
            "analyzed_by": used_backend,
        }
