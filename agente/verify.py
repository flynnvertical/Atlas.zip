"""Verificación de resultados: controles deterministas + auditor independiente (LLM)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .tools.web import normalize_url

URL_RX = re.compile(r"https?://[^\s<>\]\)\"'`,;]+")


@dataclass
class Verification:
    passed: bool
    issues: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    auditor: str = "no ejecutado"

    def footer(self) -> str:
        if self.passed:
            head = "✅ Verificación superada"
        else:
            head = "⚠️ Verificación NO superada — revisá con cautela"
        lines = [head + f" ({len(self.checks)} controles; auditor: {self.auditor})"]
        lines += [f"• {i}" for i in self.issues[:6]]
        return "\n".join(lines)


def extract_urls(text: str) -> list[str]:
    return [u.rstrip(".") for u in URL_RX.findall(text or "")]


def deterministic_checks(answer: str, seen_urls: set[str], created_files: list[str], workspace: Path,
                         used_web: bool) -> Verification:
    v = Verification(passed=True)
    v.checks.append("respuesta no vacía")
    if not answer or len(answer.strip()) < 2:
        v.passed = False
        v.issues.append("La respuesta final está vacía.")
    urls = extract_urls(answer)
    v.checks.append("fuentes citadas efectivamente consultadas")
    unseen = [u for u in urls if normalize_url(u) not in seen_urls]
    if unseen:
        v.passed = False
        v.issues.append("Se citan URLs que el agente no consultó en este trabajo (posible invención): "
                        + ", ".join(unseen[:5]))
    v.checks.append("archivos declarados existen")
    for f in created_files:
        if not (workspace / f).exists():
            v.passed = False
            v.issues.append(f"El archivo '{f}' no existe en el espacio de trabajo.")
    if used_web:
        v.checks.append("investigación con fuentes")
        if not urls:
            v.issues.append("Se usó internet pero la respuesta no cita ninguna URL.")
            v.passed = False
    return v


AUDITOR_PROMPT = """Sos un AUDITOR independiente y escéptico. No resolvés la tarea: evaluás si la respuesta
cumple los criterios de éxito usando SOLO la evidencia listada. Marcá como problema:
- afirmaciones de hechos, precios, proveedores o disponibilidad sin fuente consultada;
- criterios de éxito no cumplidos o cumplidos sólo en apariencia;
- cálculos sin mostrar o inconsistentes;
- afirmar acciones realizadas que no figuran en la evidencia.
Respondé SOLO JSON: {"cumple": true|false, "problemas": ["..."]}"""


def audit(llm, objective: str, criteria: list[str], answer: str, evidence: dict) -> tuple[bool, list[str]]:
    msgs = [
        {"role": "system", "content": AUDITOR_PROMPT},
        {"role": "user", "content": json.dumps({
            "objetivo": objective[:2000], "criterios_exito": criteria,
            "evidencia": evidence, "respuesta": answer[:6000]}, ensure_ascii=False)},
    ]
    msg = llm.chat(msgs, json_mode=True, temperature=0)
    data = parse_json(msg.get("content") or "")
    if not isinstance(data, dict) or "cumple" not in data:
        return True, ["(auditor devolvió formato inválido; no bloquea)"]
    probs = [str(p) for p in (data.get("problemas") or [])][:6]
    return bool(data["cumple"]), probs


def parse_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?", "", text).rstrip("`").strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None
