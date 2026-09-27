from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from common import CONFIG_DIR, data_url, load_json, record_ai_usage

log = logging.getLogger("moncoes-ai")


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
- Na área destinada a bicicletas, bicicletas apenas estacionadas são cenário estático esperado. Se não houver pessoa, animal, deslocamento de bicicleta, acesso à sala de energia ou outra ação relevante, use status 'normal' e categoria 'background_motion'.

Para interações humanas, aplique um limiar rigoroso para evitar falso positivo sensível:
- Proximidade entre pessoas, abraço, toque breve em ombro/costas/braço, mãos próximas ao corpo, conversa, carinho ou contato social aparentemente calmo NÃO são, isoladamente, evidência de violência ou assédio. Classifique como 'normal' quando não houver outro sinal objetivo de risco.
- Use 'violence_or_harassment' + 'critical' somente quando houver pelo menos um sinal visual forte de coerção ou agressão, como golpe, empurrão, chute, puxão brusco, contenção forçada evidente, pessoa caída após contato, tentativa clara de fuga acompanhada de perseguição física, reação defensiva inequívoca, contato repetido/invasivo acompanhado de resistência visível, ou combinação equivalente de sinais.
- Se houver interação física incomum mas sem sinal forte suficiente, use 'uncertain' e descreva de forma neutra como 'interação entre pessoas que requer revisão', sem mencionar violência ou assédio como hipótese conclusiva.
- Nunca infira relacionamento, consentimento, intenção, culpa ou vínculo entre as pessoas.

Quando houver padrão visual forte compatível com violência ou assédio físico, classifique como 'critical', descrevendo apenas os movimentos observáveis e marcando needs_human_review=true.
Quando houver retirada de objeto de sala de energia, não afirme furto; descreva somente a retirada/transporte observado.
Quando o portão aparecer aberto em apenas um momento, não conclua falha de fechamento. Para 'garage_gate_open' crítico, use a sequência temporal e exija persistência visual após a passagem.
"""

SENSITIVE_CATEGORIES = {
    "garage_gate_open",
    "forced_access_attempt",
    "energy_room_object_removal",
    "vandalism",
    "violence_or_harassment",
    "other_safety_risk",
}


def camera_context(camera: dict) -> str:
    items = []
    for code in camera.get("rules", []):
        rule = RULES.get(code)
        if rule:
            items.append(f"- {rule['reference']}: {rule['text']}")
    discipline = RULES["DISCIPLINE"]
    items.append(f"- {discipline['reference']}: {discipline['text']}")
    return "\n".join(items)


def _disabled() -> Analysis:
    return Analysis(
        status="uncertain",
        category="ai_disabled",
        confidence=0.0,
        description="API OpenAI não configurada.",
        rule_reference="nenhuma",
        reasons=[],
        needs_human_review=False,
    )


def _usage_value(obj, name: str) -> int:
    try:
        return int(getattr(obj, name, 0) or 0)
    except Exception:
        return 0


def _record_response_usage(response, stage: str, model: str) -> None:
    try:
        usage = getattr(response, "usage", None)
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        record_ai_usage(
            stage=stage,
            model=model,
            service_tier=getattr(response, "service_tier", None),
            input_tokens=_usage_value(usage, "input_tokens"),
            cached_input_tokens=_usage_value(input_details, "cached_tokens"),
            cache_write_tokens=_usage_value(input_details, "cache_write_tokens"),
            output_tokens=_usage_value(usage, "output_tokens"),
            reasoning_tokens=_usage_value(output_details, "reasoning_tokens"),
        )
    except Exception as exc:
        log.warning("Falha ao registrar telemetria de uso: %s", exc)


def _call(
    client: OpenAI,
    *,
    model: str,
    effort: str,
    detail: str,
    prompt: str,
    sheet: Path,
    stage: str,
) -> Analysis:
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
        reasoning={"effort": effort},
        service_tier=os.getenv("MONCOES_AI_SERVICE_TIER", "default"),
        prompt_cache_key=os.getenv("MONCOES_AI_CACHE_KEY", "moncoes-ai-monitor"),
        store=False,
    )
    _record_response_usage(response, stage, model)
    if response.output_parsed is None:
        raise RuntimeError(f"Resposta estruturada vazia em {stage} com {model}")
    return response.output_parsed


def _call_with_fallback(
    client: OpenAI,
    *,
    model: str,
    fallback_model: str,
    effort: str,
    fallback_effort: str,
    detail: str,
    prompt: str,
    sheet: Path,
    stage: str,
) -> Analysis:
    try:
        return _call(
            client,
            model=model,
            effort=effort,
            detail=detail,
            prompt=prompt,
            sheet=sheet,
            stage=stage,
        )
    except Exception as exc:
        if not fallback_model or fallback_model == model:
            raise
        log.warning(
            "Modelo %s falhou em %s; usando fallback %s: %s",
            model,
            stage,
            fallback_model,
            exc,
        )
        return _call(
            client,
            model=fallback_model,
            effort=fallback_effort,
            detail=detail,
            prompt=prompt,
            sheet=sheet,
            stage=f"{stage}_fallback",
        )


def _needs_escalation(result: Analysis) -> bool:
    # Terra is reserved for cases where a third opinion can materially improve
    # safety. A high-quality Luna review that concludes "normal" no longer
    # escalates merely because confidence is below an arbitrary threshold.
    if result.status in {"critical", "uncertain"}:
        return True
    if (
        result.status == "potential_occurrence"
        and result.category in SENSITIVE_CATEGORIES
    ):
        return True
    return False


def analyze(sheet: Path, camera: dict, dvr: str, channel: int, stage: str = "triage") -> Analysis:
    try:
        client = OpenAI()
    except Exception:
        return _disabled()

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

    if stage == "triage":
        result = _call_with_fallback(
            client,
            model=os.getenv("MONCOES_AI_TRIAGE_MODEL", "gpt-6-luna"),
            fallback_model=os.getenv("MONCOES_AI_TRIAGE_FALLBACK", "gpt-5.6-luna"),
            effort=os.getenv("MONCOES_AI_TRIAGE_REASONING", "none"),
            fallback_effort="none",
            detail="low",
            prompt=prompt,
            sheet=sheet,
            stage="triage",
        )

        # Safety guard: low-confidence normal classifications receive one
        # stronger Luna pass before the event is allowed to disappear as normal.
        guard_threshold = float(
            os.getenv("MONCOES_AI_TRIAGE_GUARD_CONFIDENCE", "0.82")
        )
        if result.status == "normal" and result.confidence < guard_threshold:
            try:
                result = _call_with_fallback(
                    client,
                    model=os.getenv("MONCOES_AI_REVIEW_MODEL", "gpt-6-luna"),
                    fallback_model=os.getenv("MONCOES_AI_REVIEW_FALLBACK", "gpt-5.6-terra"),
                    effort=os.getenv("MONCOES_AI_REVIEW_REASONING", "high"),
                    fallback_effort="medium",
                    detail="high",
                    prompt=prompt,
                    sheet=sheet,
                    stage="triage_guard",
                )
            except Exception as exc:
                log.warning("Guarda de triagem falhou; mantendo triagem inicial: %s", exc)
        return result

    if stage == "review":
        result = _call_with_fallback(
            client,
            model=os.getenv("MONCOES_AI_REVIEW_MODEL", "gpt-6-luna"),
            fallback_model=os.getenv("MONCOES_AI_REVIEW_FALLBACK", "gpt-5.6-terra"),
            effort=os.getenv("MONCOES_AI_REVIEW_REASONING", "high"),
            fallback_effort="medium",
            detail="high",
            prompt=prompt,
            sheet=sheet,
            stage="review",
        )

        if _needs_escalation(result):
            try:
                return _call(
                    client,
                    model=os.getenv("MONCOES_AI_ESCALATION_MODEL", "gpt-5.6-terra"),
                    effort=os.getenv("MONCOES_AI_ESCALATION_REASONING", "medium"),
                    detail="high",
                    prompt=prompt,
                    sheet=sheet,
                    stage="escalation",
                )
            except Exception as exc:
                log.warning(
                    "Escalonamento Terra falhou; mantendo revisão Luna: %s",
                    exc,
                )
        return result

    if stage == "escalation":
        return _call(
            client,
            model=os.getenv("MONCOES_AI_ESCALATION_MODEL", "gpt-5.6-terra"),
            effort=os.getenv("MONCOES_AI_ESCALATION_REASONING", "medium"),
            detail="high",
            prompt=prompt,
            sheet=sheet,
            stage="escalation",
        )

    raise ValueError(f"estágio IA desconhecido: {stage}")
