from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ChecklistField:
    value: str = ""
    confidence: float = 0.0
    source_document: str = ""
    evidence: str = ""
    checked: Optional[bool] = None


@dataclass
class ChecklistData:
    fields: Dict[str, ChecklistField] = field(default_factory=dict)

    @staticmethod
    def from_dict(payload: Dict[str, Any]) -> "ChecklistData":
        fields: Dict[str, ChecklistField] = {}

        for key, raw in payload.items():
            if isinstance(raw, dict):
                fields[key] = ChecklistField(
                    value=str(raw.get("value") or "").strip(),
                    confidence=float(raw.get("confidence") or 0.0),
                    source_document=str(raw.get("source_document") or "").strip(),
                    evidence=str(raw.get("evidence") or "").strip(),
                    checked=raw.get("checked"),
                )
            else:
                fields[key] = ChecklistField(value=str(raw or "").strip(), confidence=1.0)

        return ChecklistData(fields=fields)

    def get_value(self, key: str) -> str:
        field = self.fields.get(key)
        return field.value if field else ""

    def get_checked_label(self, key: str, threshold: float = 0.85) -> str:
        field = self.fields.get(key)
        if not field:
            return ""

        if field.checked is True:
            return "Проверено"
        if field.checked is False:
            return "Непроверено"
        if field.value and field.confidence >= threshold:
            return "Проверено"
        if field.value:
            return "За проверка"
        return ""
