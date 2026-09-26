from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from common import CONFIG_DIR, data_url, load_json


class Analysis(BaseModel):
    status: Literal["normal", "potential_occurrence", "critical", "uncertain"]
    category: str = Field(description="Categoria curta e objetiva")
    confidence: float = Field(ge=0.0, le=1.0)
    description: str = Field(description="Descrição apenas do que é visualmente observável")
    rule_reference: str = Field(description="Regra potencialmente relacionada ou 'nenhuma'")
    reasons: list[str] = Field(description="Evidências visuais objetivas")
    needs_human_review: bool


RULES = load_json(CONFIG_DIR / "rules.json")

SYSTEM = """Você é o classificador visual do Condomínio Edifício Monções.
Seja conservador e objetivo. Não identifique pessoas e não tente reconhecer rostos.
Não infira identidade, intenção, vínculo, autorização, raça, religião, saúde ou qualquer atributo sensível.
Não aplique advertência ou multa. 'potential_occurrence' significa apenas 'potencial ocorrência — requer revisão do síndico'.
Considere os quatro quadrantes da imagem como momentos sucessivos do mesmo evento.
Use somente o que está visualmente observável. Se a evidência não permitir concluir, prefira 'uncertain'.
Para atividade rotineira, use 'normal'. 'critical' é reservado a perigo ou risco de segurança aparente e imediato.
"""


def camera_context(camera: dict) -> str:
    items = []
    for code in camera.get("rules", []):
        rule = RULES.get(code)
        if rule:
            items.append(f"- {rule['reference']}: {rule['text']}")
    discipline = RULES["DISCIPLINE"]
    items.append(f"- {discipline['reference']}: {discipline['text']}")
    return "\n".join(items)


def analyze(sheet: Path, camera: dict, dvr: str, channel: int, stage: str = "triage") -> Analysis:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return Analysis(
            status="uncertain",
            category="ai_disabled",
            confidence=0.0,
            description="API OpenAI não configurada.",
            rule_reference="nenhuma",
            reasons=[],
            needs_human_review=False,
        )

    client = OpenAI(api_key=api_key)
    if stage == "triage":
        model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
        detail = "low"
    else:
        model = os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.6-terra")
        detail = "high"

    prompt = f"""Analise este evento de CFTV.
DVR: {dvr}; câmera física: {channel}; local: {camera['name']}; prioridade: {camera['priority']}.
Regras relevantes:
{camera_context(camera)}
Classifique somente a evidência visual. A imagem é uma montagem 2x2 em ordem temporal.
"""

    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url(sheet), "detail": detail},
                ],
            },
        ],
        text_format=Analysis,
        store=False,
    )
    return response.output_parsed
