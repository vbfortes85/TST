"""Etapa 2 — Extração do inteiro teor via Judit.io (PRD §6).

Uma chamada paga por processo ("varejo, um por um" — PRD §6): busca por número
CNJ com with_attachments=true, conforme a documentação oficial (docs.judit.io).
Serviço pago que exige credencial comercial (cotação: atendimento@judit.io).

Compromisso de integridade: sem JUDIT_API_KEY configurada, cada solicitação é
registrada com status 'pendente_credencial' e NENHUM documento é fabricado ou
simulado. O pipeline segue funcional (o restante das camadas opera), e as
solicitações pendentes podem ser reprocessadas quando a credencial existir.
"""

import json
import urllib.error
import urllib.request

from . import config


class ErroJudit(Exception):
    pass


def credencial_configurada() -> bool:
    return bool(config.JUDIT_API_KEY)


def _requisicao(metodo: str, caminho: str, corpo: dict | None = None) -> dict:
    url = f"{config.JUDIT_BASE_URL}{caminho}"
    dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(
        url,
        data=dados,
        headers={"api-key": config.JUDIT_API_KEY, "Content-Type": "application/json"},
        method=metodo,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        corpo_erro = e.read().decode("utf-8", errors="replace")[:500]
        raise ErroJudit(f"Judit HTTP {e.code}: {corpo_erro}") from e
    except urllib.error.URLError as e:
        raise ErroJudit(f"Falha de conexão com a Judit: {e.reason}") from e


def solicitar_inteiro_teor(numero_formatado: str) -> dict:
    """POST /requests com search_type lawsuit_cnj e with_attachments=true.

    Retorna a resposta da Judit (contendo request_id para acompanhamento).
    """
    if not credencial_configurada():
        raise ErroJudit(
            "JUDIT_API_KEY não configurada — solicitação registrada como pendente"
        )
    corpo = {
        "search": {
            "search_type": "lawsuit_cnj",
            "search_key": numero_formatado,
            "with_attachments": True,
        }
    }
    return _requisicao("POST", "/requests", corpo)


def consultar_respostas(request_id: str) -> dict:
    """GET /responses filtrado pelo request_id da solicitação."""
    if not credencial_configurada():
        raise ErroJudit("JUDIT_API_KEY não configurada")
    return _requisicao("GET", f"/responses?request_id={request_id}")


def extrair_textos(resposta: dict) -> list[dict]:
    """Extrai anexos em texto da resposta da Judit, preservando metadados.

    A estrutura exata da resposta depende do contrato do serviço; esta função
    percorre defensivamente os campos e devolve apenas o que existir de fato
    (tipo do documento + texto extraído), sem inventar conteúdo.
    """
    textos = []

    def _percorrer(no, caminho=""):
        if isinstance(no, dict):
            corpo = no.get("extracted_text") or no.get("text") or no.get("content")
            if isinstance(corpo, str) and corpo.strip():
                textos.append({
                    "tipo": no.get("type") or no.get("attachment_type") or "anexo",
                    "texto": corpo,
                    "caminho_origem": caminho,
                })
            for chave, valor in no.items():
                _percorrer(valor, f"{caminho}.{chave}" if caminho else chave)
        elif isinstance(no, list):
            for i, item in enumerate(no):
                _percorrer(item, f"{caminho}[{i}]")

    _percorrer(resposta)
    return textos
