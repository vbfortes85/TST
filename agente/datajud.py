"""Etapa 1 — Cliente da API Pública do DataJud (CNJ).

Descoberta e filtro: produz a lista de números CNJ candidatos que alimenta a
Etapa 2 (Judit). Consulta REST/Elasticsearch nos endpoints por tribunal
(api_publica_tst, api_publica_trt1..24), com paginação via search_after e
respeito ao rate limit observado (~30 req/min, não documentado — PRD §6).
"""

import json
import time
import urllib.error
import urllib.request

from . import config

_ultima_chamada = 0.0


class ErroDataJud(Exception):
    pass


def construir_consulta(
    filtros: dict,
    tamanho: int = 100,
    search_after: list | None = None,
    apos_timestamp: str | None = None,
) -> dict:
    """Monta o corpo Elasticsearch da consulta a partir dos filtros declarados.

    O corpo completo é registrado na auditoria de cada sessão — a consulta
    exata é parte do protocolo replicável exigido pelo Caderno Técnico.
    """
    must = []

    codigos = filtros.get("assuntos_codigos_incluir") or []
    padroes = filtros.get("assuntos_padroes_incluir") or []
    if codigos or padroes:
        should = []
        if codigos:
            should.append({"terms": {"assuntos.codigo": codigos}})
        # match sobre o nome do assunto: a análise fina (regex) é refeita
        # localmente pelo pré-filtro estrutural sobre os dados retornados.
        for padrao in padroes:
            termo_simples = (
                padrao.replace("[cç]", "ç")
                .replace("[aã]", "ã")
                .replace("[ií]", "í")
                .replace("[eé]", "é")
            )
            should.append({"match": {"assuntos.nome": termo_simples}})
        must.append({"bool": {"should": should, "minimum_should_match": 1}})

    janela = filtros.get("janela_temporal") or {}
    intervalo = {}
    if janela.get("inicio"):
        intervalo["gte"] = janela["inicio"]
    if janela.get("fim"):
        intervalo["lte"] = janela["fim"]
    if intervalo:
        must.append({"range": {"dataAjuizamento": intervalo}})

    if apos_timestamp:
        must.append({"range": {"@timestamp": {"gt": apos_timestamp}}})

    consulta = {
        "size": tamanho,
        "query": {"bool": {"must": must}} if must else {"match_all": {}},
        "sort": [{"@timestamp": {"order": "asc"}}],
    }
    if search_after:
        consulta["search_after"] = search_after
    return consulta


def _respeitar_rate_limit() -> None:
    global _ultima_chamada
    intervalo_minimo = 60.0 / max(config.DATAJUD_RPM, 1)
    decorrido = time.monotonic() - _ultima_chamada
    if decorrido < intervalo_minimo:
        time.sleep(intervalo_minimo - decorrido)
    _ultima_chamada = time.monotonic()


def buscar(tribunal: str, consulta: dict) -> dict:
    alias = config.TRIBUNAIS.get(tribunal.upper())
    if not alias:
        raise ErroDataJud(f"Tribunal desconhecido: {tribunal!r}. Válidos: TST, TRT1..TRT24.")
    _respeitar_rate_limit()
    url = f"{config.DATAJUD_BASE_URL}/{alias}/_search"
    req = urllib.request.Request(
        url,
        data=json.dumps(consulta).encode("utf-8"),
        headers={
            "Authorization": f"APIKey {config.DATAJUD_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", errors="replace")[:500]
        raise ErroDataJud(f"DataJud HTTP {e.code}: {corpo}") from e
    except urllib.error.URLError as e:
        raise ErroDataJud(f"Falha de conexão com o DataJud: {e.reason}") from e


def extrair_hits(resposta: dict) -> tuple[list, list | None, int]:
    """Retorna (hits, sort do último hit p/ search_after, total informado)."""
    hits = resposta.get("hits", {}).get("hits", [])
    total = resposta.get("hits", {}).get("total", {})
    total_valor = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
    ultimo_sort = hits[-1].get("sort") if hits else None
    return hits, ultimo_sort, total_valor


def normalizar_fonte(fonte: dict) -> dict:
    """Normaliza o _source de um hit do DataJud para o formato interno."""
    classe = fonte.get("classe") or {}
    assuntos_brutos = fonte.get("assuntos") or []
    assuntos = []
    for item in assuntos_brutos:
        # Em algumas respostas os assuntos vêm aninhados em listas.
        if isinstance(item, list):
            assuntos.extend(a for a in item if isinstance(a, dict))
        elif isinstance(item, dict):
            assuntos.append(item)
    orgao = fonte.get("orgaoJulgador") or {}
    return {
        "numero": fonte.get("numeroProcesso", ""),
        "tribunal": fonte.get("tribunal", ""),
        "grau": fonte.get("grau", ""),
        "classe_codigo": str(classe.get("codigo", "")),
        "classe_nome": classe.get("nome", ""),
        "assuntos": [
            {"codigo": str(a.get("codigo", "")), "nome": a.get("nome", "")}
            for a in assuntos
        ],
        "orgao_julgador": orgao.get("nome", "") if isinstance(orgao, dict) else str(orgao),
        "data_ajuizamento": fonte.get("dataAjuizamento", ""),
        "timestamp_fonte": fonte.get("@timestamp", ""),
    }
