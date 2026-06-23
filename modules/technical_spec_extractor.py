import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List


@dataclass
class TechnicalRequirement:
    id: str
    position: str
    scope: str
    component: str
    parameter: str
    requirement_text: str
    raw_text: str


class TechnicalSpecExtractor:
    """
    Extracts structured technical requirements from Bulgarian technical specifications.

    Rules:
    - Extract only "Поз. X.Y." anchored requirements.
    - Merge text from the current position until the next position.
    - 1.x is server scope.
    - 2.x is switch scope.
    """

    POSITION_RE = re.compile(
        r"(Поз\.\s*(?P<num>\d+(?:\.\d+)+)\s*\.)",
        re.IGNORECASE | re.UNICODE,
    )

    def extract(self, text: str) -> List[TechnicalRequirement]:
        normalized_text = self._normalize_text(text)
        chunks = self._split_by_position(normalized_text)

        requirements: List[TechnicalRequirement] = []

        for pos, raw in chunks:
            req = TechnicalRequirement(
                id=f"POS-{pos}",
                position=pos,
                scope=self._detect_scope(pos, raw),
                component=self._detect_component(raw),
                parameter=self._detect_parameter(raw),
                requirement_text=self._clean_requirement_text(raw),
                raw_text=raw,
            )
            requirements.append(req)

        return requirements

    def extract_server_requirements(self, text: str) -> List[TechnicalRequirement]:
        return [r for r in self.extract(text) if r.scope == "server"]

    def extract_switch_requirements(self, text: str) -> List[TechnicalRequirement]:
        return [r for r in self.extract(text) if r.scope == "switch"]

    def to_dicts(self, requirements: List[TechnicalRequirement]) -> List[Dict[str, Any]]:
        return [asdict(r) for r in requirements]

    def _normalize_text(self, text: str) -> str:
        if not text:
            return ""

        text = text.replace("\xa0", " ")
        text = text.replace("–", "-")
        text = text.replace("—", "-")
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s{2,}", " ", text)

        return text.strip()

    def _split_by_position(self, text: str) -> List[tuple[str, str]]:
        matches = list(self.POSITION_RE.finditer(text))
        result: List[tuple[str, str]] = []

        for i, match in enumerate(matches):
            pos = match.group("num")
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

            chunk = text[start:end].strip()
            chunk = self._fix_common_spacing_issues(chunk)
            result.append((pos, chunk))

        return result

    def _fix_common_spacing_issues(self, text: str) -> str:
        text = re.sub(r"([а-яА-Я])\s+-\s+([а-яА-Я])", r"\1-\2", text)
        text = re.sub(r"(\d+)\s*,\s*(\d+)", r"\1,\2", text)
        text = re.sub(r"(\d+)\s*\.\s*(\d+)", r"\1.\2", text)
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()

    def _detect_scope(self, pos: str, text: str) -> str:
        if pos.startswith("1."):
            return "server"

        if pos.startswith("2."):
            return "switch"

        low = text.lower()

        if any(k in low for k in ["сървър", "процесор", "cpu", "gpu", "ram", "nvme"]):
            return "server"

        if any(k in low for k in ["комутатор", "osfp порт", "switch"]):
            return "switch"

        return "unknown"

    def _detect_component(self, text: str) -> str:
        low = text.lower()

        # Order is critical.
        # Storage before memory: "запаметяващи" contains "памет".
        if any(k in low for k in [
            "nvme",
            "ssd",
            "hdd",
            "запаметяващ",
            "запаметяващи",
            "запаметяващо",
            "диск",
            "дисков",
            "устройства за съхранение",
            "storage",
            "dwpd",
            "скорост за четене",
            "скорост за писане",
            "четене",
            "писане",
            "7.68tb",
            "7,68tb",
        ]):
            return "storage"

        # Memory before CPU: RAM requirements often mention "между процесорите".
        if any(k in low for k in [
            "ram",
            "оперативна памет",
            "паметта на системата",
            "ram памет",
            "ddr",
            "dimm",
            "rdimm",
            "lrdimm",
            "1200 gb/s",
            "2 tb ram",
            "2tb ram",
        ]):
            return "memory"

        if any(k in low for k in [
            "gpu",
            "графич",
            "ускорител",
            "ускорители",
            "cuda",
            "nvidia",
            "h100",
            "h200",
            "a100",
            "b200",
        ]):
            return "gpu"

        if any(k in low for k in [
            "infiniband",
            "xdr",
            "osfp",
            "800g",
        ]):
            return "infiniband"

        if any(k in low for k in [
            "ethernet",
            "qsfp112",
            "qsfp56",
            "200g",
            "мрежови контролер",
            "мрежов контролер",
            "network",
            "nic",
        ]):
            return "ethernet"

        if any(k in low for k in [
            "охлаждан",
            "direct-to-chip",
            "direct -to-chip",
            "d2c",
            "cold-plate",
            "cold -plate",
            "dlc",
            "течно охлаждане",
            "радиатори",
        ]):
            return "cooling"

        if any(k in low for k in [
            "ipmi",
            "kvm",
            "отдалечен контрол",
            "отдалечен достъп",
            "интерфейс за контрол",
            "управление",
            "remote management",
            "ilo",
            "idrac",
            "xclarity",
        ]):
            return "management"

        if any(k in low for k in [
            "захранващ",
            "захранване",
            "захранващи модули",
            "psu",
            "power supply",
            "220",
            "240v",
            "220-240v",
            "220 -240v",
            "pfc",
        ]):
            return "power"

        if any(k in low for k in [
            "процесор",
            "процесори",
            "cpu",
            "ядра",
            "ядро",
            "ghz",
            "mhz",
            "l3 cache",
            "cache",
            "epyc",
            "xeon",
        ]):
            return "cpu"

        if any(k in low for k in [
            "порт",
            "портове",
            "rj45",
            "de-9",
            "serial",
            "сериен порт",
            "комутатор",
        ]):
            return "switch_ports"

        return "general"

    def _detect_parameter(self, text: str) -> str:
        low = text.lower()

        # Storage first to avoid false RAM matches from "запаметяващи".
        if "pci" in low and "nvme" in low:
            return "nvme_count_type"

        if any(k in low for k in ["7.68tb", "7,68tb", "7.68 tb", "7,68 tb"]):
            return "nvme_capacity"

        if "dwpd" in low:
            return "nvme_endurance"

        if any(k in low for k in ["6,700", "6700", "6 700"]) and any(k in low for k in ["3,600", "3600", "3 600"]):
            return "nvme_performance"

        # RAM before CPU because RAM requirements often mention processors.
        if any(k in low for k in ["2 tb ram", "2tb ram", "2 tb ram памет", "2tb ram памет"]):
            return "ram_capacity"

        if "1200 gb/s" in low:
            return "ram_bandwidth"

        if "ram" in low and any(k in low for k in ["информация", "производител", "модел", "скорост"]):
            return "ram_disclosure"

        if "захранващи модула" in low and ("четири" in low or "(4)" in low or "4 " in low):
            return "psu_count"

        if "резервирана конфигурация" in low:
            return "psu_redundancy"

        if "220" in low and "240" in low:
            return "power_input"

        if "два (2) процесора" in low or "2) процесора" in low or "два процесора" in low:
            return "cpu_count"

        if "96" in low and "ядра" in low:
            return "cpu_cores_clock_cache"

        if "gpu" in low or "графич" in low or "ускорител" in low:
            return "gpu_configuration"

        if "200g ethernet" in low:
            return "ethernet_controller"

        if "qsfp112" in low or "qsfp56" in low:
            return "ethernet_ports_bandwidth"

        if "infiniband" in low or "xdr" in low:
            return "infiniband_controller"

        if any(k in low for k in ["direct-to-chip", "direct -to-chip", "d2c", "cold-plate", "cold -plate", "dlc"]):
            return "direct_liquid_cooling"

        if "30 градуса" in low:
            return "ambient_temperature"

        if "ipmi" in low or "kvm" in low:
            return "remote_management"

        if "72" in low and "osfp" in low:
            return "switch_osfp_ports"

        if "rj45" in low or "de-9" in low:
            return "switch_management_port"

        return "technical_requirement"

    def _clean_requirement_text(self, text: str) -> str:
        text = re.sub(
            r"^Поз\.\s*\d+(?:\.\d+)+\s*\.\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()
