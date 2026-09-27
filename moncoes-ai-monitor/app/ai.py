from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from common import CONFIG_DIR, data_url, load_json


EventCategory = Literal[
    "normal_activity",
    "background_motion",
    "garage_gate_open",
    "pedestrian_garage_access",
    "forced_access_attempt",
    "energy_room_object_removal",
    "technical_area_access",
    "vandalism",
    "violence_or_harassment",
    "unauthorized_access_suspected",
    "rule_violation",
    "other_safety_risk",
    "uncertain_context",
    "ai_disabled",
]


class Analysis(BaseModel):
    status: Literal["normal", "potential_occurrence", "critical", "uncertain"]
    category: EventCategory = Field(description="Categoria operacional padronizada")
    confidence: float = Field(ge=0.0, le=1.0)
    description: str = Field(description="Descrição apenas do que é visualmente observável")
    rule_reference: str = Field(description="Regra potencialmente relacionada ou 'nenhuma'")
    reasons: list[str] = Field(description="Evidências visuais objetivas")
    needs_human_review: bool


RULES = load_json(CONFIG_DIR / "rules.json")

SYSTEM = """Você é o classificador visual do Condomínio Edifício Monções.
Seja objetivo e descreva somente evidências visuais. Não identifique pessoas e não tente reconhecer rostos.
Não infira identidade, intenção, vínculo, autorização, raça, religião, saúde ou qualquer atributo sensível.
Não aplique advertência ou multa. 'potential_occurrence' significa apenas 'potencial ocorrência — requer revisão humana'.
Considere os quatro quadrantes da imagem como momentos sucessivos do mesmo evento.

REGRAS DE SEVERIDADE:
- 'critical' deve ser usado quando a imagem mostrar sinais ou movimentos razoavelmente compatíveis com perigo ou risco imediato de segurança, inclusive:
  1) possível violência física, agressão, luta, contenção forçada, perseguição ameaçadora, contato físico aparentemente não consentido ou assédio físico;
  2) tentativa aparente de arrombar, forçar, golpear ou manipular portões, portas, fechaduras ou acessos;
  3) dano, depredação ou vandalismo aparente em elevador, portões, portas, paredes, equipamentos ou áreas comuns;
  4) pessoa saindo de sala de energia portando ou retirando objeto/equipamento/material;
  5) portão de garagem que permaneça visivelmente aberto além do ciclo esperado de aproximadamente 8 segundos, após a passagem parecer concluída e sem veículo/obstáculo aparente impedindo o fechamento;
  6) outro risco imediato de segurança claramente visível.
- 'potential_occurrence' deve ser usado para situações que exigem revisão, mas não mostram risco imediato, inclusive:
  1) pedestre entrando ou saindo pelo portão de veículos da garagem;
  2) qualquer pessoa acessando ou permanecendo na área técnica/casa de máquinas;
  3) acesso possivelmente irregular quando a imagem não permite confirmar autorização.
- 'uncertain' é para evidência insuficiente ou ambígua que não se encaixe com segurança nos casos acima.
- 'normal' é para atividade rotineira sem indício relevante.
- Quando o movimento tiver sido acionado apenas por inseto, mudança de luz/sombra, porta do elevador abrindo/fechando sem pessoa, cenário vazio ou qualquer movimento sem pessoa, veículo, animal ou objeto relevante, use status 'normal' e categoria 'background_motion'. Esses eventos são técnicos e não devem ser apresentados aos moradores.

Para interações humanas, aplique um limiar rigoroso para evitar falso positivo sensível:
- Proximidade entre pessoas, abraço, toque breve em ombro/costas/braço, mãos próximas ao corpo, conversa, carinho ou contato social aparentemente calmo NÃO são, isoladamente, evidência de violência ou assédio. Classifique como 'normal' quando não houver outro sinal objetivo de risco.
- Use 'violence_or_harassment' + 'critical' somente quando houver pelo menos um sinal visual forte de coerção ou agressão, como golpe, empurrão, chute, puxão brusco, contenção forçada evidente, pessoa caída após contato, tentativa clara de fuga acompanhada de perseguição física, reação defensiva inequívoca, contato repetido/invasivo acompanhado de resistência visível, ou combinação equivalente de sinais.
- Se houver interação física incomum mas sem sinal forte suficiente, use 'uncertain' e descreva de forma neutra como 'interação entre pessoas que requer revisão', sem mencionar violência ou assédio como hipótese conclusiva.
- Nunca infira relacionamento, consentimento, intenção, culpa ou vínculo entre as pessoas.

Quando houver padrão visual forte compatível com violência ou assédio físico, classifique como 'critical', descrevendo apenas os movimentos observáveis e marcando needs_human_review=true.
Quando houver retirada de objeto de sala de energia, não afirme furto; descreva somente a retirada/transporte observado.
Quando o portão aparecer aberto em apenas um momento, não conclua falha de fechamento. Para 'garage_gate_open' crítico, use a sequência temporal e exija persistência visual após a passagem.
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
Use uma destas categorias exatamente: normal_activity, background_motion, garage_gate_open, pedestrian_garage_access,
forced_access_attempt, energy_room_object_removal, technical_area_access, vandalism,
violence_or_harassment, unauthorized_access_suspected, rule_violation, other_safety_risk,
uncertain_context.

EVENTOS SEM CONTEÚDO RELEVANTE:
Se a sequência estiver vazia ou mostrar apenas inseto, sombra, variação de iluminação, porta do elevador se movendo sozinha ou outro acionamento técnico sem pessoa, veículo, animal ou objeto relevante, use category='background_motion', status='normal', needs_human_review=false.

ATENÇÃO ESPECIAL ÀS INTERAÇÕES HUMANAS:
Um toque breve no ombro, costas ou braço, abraço, aproximação, conversa ou gesto de carinho sem resistência, queda, golpe, contenção forçada ou tentativa de afastamento deve ser tratado como atividade normal. Não converta contato social comum em alerta de violência/assédio.
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
