"""Classificação semântica (camada 4 do pipeline, PRD §7) — o coração do agente.

Responde à pergunta central do PRD: no texto, "médico"/"enfermeiro" é a pessoa
que trabalhava, ou apenas adjetivo ("atestado médico", "perícia médica")?

Duas camadas, ambas auditáveis:

1. Heurística determinística (padrão, sem dependências): regras versionadas em
   config/regras_semanticas.json. Cada decisão devolve as regras acionadas com
   os trechos do texto que as acionaram — replicável e explicável, como exige
   o Caderno Técnico.

2. Opcional, via API da Anthropic (se o pacote `anthropic` estiver instalado e
   houver credencial): segunda opinião registrada como camada separada
   ('semantica_llm'), nunca substituindo silenciosamente a heurística.

A classificação plena exige o texto integral (Etapa 2). Sobre metadados apenas,
o resultado máximo é 'revisar' — nunca 'incluido' definitivo sem texto.
"""

import json
import re

from . import config

TRECHO_CONTEXTO = 60


def carregar_regras() -> dict:
    return config.carregar_config("regras_semanticas.json")


def _ocorrencias(padrao: str, texto: str) -> list[dict]:
    achados = []
    for m in re.finditer(padrao, texto, flags=re.IGNORECASE):
        ini = max(0, m.start() - TRECHO_CONTEXTO)
        fim = min(len(texto), m.end() + TRECHO_CONTEXTO)
        achados.append({"trecho": "…" + texto[ini:fim].strip() + "…", "posicao": m.start()})
    return achados


def classificar_texto(texto: str, regras: dict | None = None, base: str = "texto_integral") -> dict:
    """Classifica um texto. Retorna resultado, score e motivos detalhados."""
    regras = regras or carregar_regras()
    pesos = regras["pesos"]
    motivos = []
    score = 0.0

    # 1. Posições cobertas por colocações negativas ("atestado médico" etc.)
    intervalos_negativos = []
    for padrao in regras["colocacoes_negativas"]:
        for m in re.finditer(padrao, texto, flags=re.IGNORECASE):
            intervalos_negativos.append((m.start(), m.end()))
            score += pesos["colocacao_negativa"]
            motivos.append({
                "regra": "colocacao_negativa",
                "padrao": padrao,
                "peso": pesos["colocacao_negativa"],
                "trecho": texto[max(0, m.start() - TRECHO_CONTEXTO):m.end() + TRECHO_CONTEXTO].strip(),
            })

    def _dentro_de_negativa(posicao: int) -> bool:
        return any(ini <= posicao < fim for ini, fim in intervalos_negativos)

    # 2. Menções de profissão FORA de colocação negativa — critério necessário.
    profissao_em_contexto = 0
    for padrao in regras["profissoes"]:
        for m in re.finditer(padrao, texto, flags=re.IGNORECASE):
            if not _dentro_de_negativa(m.start()):
                profissao_em_contexto += 1
                motivos.append({
                    "regra": "profissao_em_contexto",
                    "padrao": padrao,
                    "peso": pesos["profissao_em_contexto"],
                    "trecho": texto[max(0, m.start() - TRECHO_CONTEXTO):m.end() + TRECHO_CONTEXTO].strip(),
                })
    if profissao_em_contexto:
        score += pesos["profissao_em_contexto"]  # conta uma vez; evita inflar por repetição

    # 3. Marcadores jurídicos do fenômeno (PRD §6.1).
    for chave, peso in (("marcadores_fortes", pesos["marcador_forte"]),
                        ("marcadores_positivos", pesos["marcador_positivo"])):
        for padrao in regras[chave]:
            achados = _ocorrencias(padrao, texto)
            if achados:
                score += peso  # presença do marcador, não frequência
                motivos.append({
                    "regra": chave.rstrip("s"),
                    "padrao": padrao,
                    "peso": peso,
                    "trecho": achados[0]["trecho"],
                    "ocorrencias": len(achados),
                })

    limiares = regras["limiares"]
    if profissao_em_contexto == 0:
        resultado = "excluido"
        motivos.append({
            "regra": "criterio_necessario",
            "detalhe": "nenhuma menção de profissão de saúde fora de colocação negativa"
            " (profissão aparece apenas como adjetivo, ou não aparece)",
        })
    elif score >= limiares["incluir"]:
        resultado = "incluido"
    elif score >= limiares["revisar"]:
        resultado = "revisar"
    else:
        resultado = "excluido"

    # Sem inteiro teor, o resultado nunca é inclusão definitiva.
    if base == "metadados" and resultado == "incluido":
        resultado = "revisar"
        motivos.append({
            "regra": "limite_base_metadados",
            "detalhe": "classificação sobre metadados apenas: inclusão definitiva"
            " exige o texto integral (Etapa 2)",
        })

    return {
        "resultado": resultado,
        "score": round(score, 2),
        "base": base,
        "motivos": motivos,
        "versao_regras": regras["versao"],
    }


# --- Camada opcional via API da Anthropic -----------------------------------

def llm_disponivel() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    import os
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


_PROMPT_LLM = """Você é um assistente de triagem jurisprudencial. Analise o texto de decisão \
trabalhista abaixo e responda APENAS com um objeto JSON, sem nenhum outro texto, no formato:
{"relevante": true/false, "confianca": "alta"/"media"/"baixa", "justificativa": "..."}

Critério de relevância: a decisão trata de pejotização de profissional de SAÚDE \
(médico/médica ou enfermeiro/enfermeira) — isto é, discute vínculo de emprego, \
subordinação, primazia da realidade ou art. 442-B da CLT em contratação desses \
profissionais como pessoa jurídica ou autônomo. Menções a "atestado médico", \
"perícia médica" ou "laudo médico" NÃO tornam a decisão relevante por si só.

Texto da decisão:
---
{TEXTO}
---"""


def classificar_llm(texto: str) -> dict:
    """Segunda opinião via API da Anthropic. Só é chamada se llm_disponivel()."""
    import anthropic

    client = anthropic.Anthropic()
    resposta = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": _PROMPT_LLM.replace("{TEXTO}", texto[:30000])}],
    )
    partes = [b.text for b in resposta.content if b.type == "text"]
    bruto = "\n".join(partes).strip()
    try:
        inicio, fim = bruto.index("{"), bruto.rindex("}") + 1
        dados = json.loads(bruto[inicio:fim])
    except (ValueError, json.JSONDecodeError):
        return {
            "resultado": "revisar",
            "score": None,
            "base": "texto_integral",
            "motivos": [{"regra": "llm_resposta_nao_estruturada", "resposta": bruto[:500]}],
            "versao_regras": f"llm:{config.ANTHROPIC_MODEL}",
        }
    return {
        "resultado": "incluido" if dados.get("relevante") else "excluido",
        "score": None,
        "base": "texto_integral",
        "motivos": [{
            "regra": "llm",
            "modelo": config.ANTHROPIC_MODEL,
            "confianca": dados.get("confianca"),
            "justificativa": dados.get("justificativa"),
        }],
        "versao_regras": f"llm:{config.ANTHROPIC_MODEL}",
    }
