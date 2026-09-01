"""Pré-filtro estrutural (camada 2 do pipeline, PRD §7).

Opera sobre os metadados do DataJud (classe processual e assuntos TPU) segundo
os critérios declarados em config/filtros_estruturais.json. Limitação conhecida
e documentada (PRD §7/§13): o preenchimento da TPU é inconsistente entre
tribunais, portanto este filtro reduz volume mas não substitui a camada
semântica. Toda decisão retorna a lista de motivos, gravada na auditoria.
"""

import re

from . import config


def carregar_regras() -> dict:
    return config.carregar_config("filtros_estruturais.json")


def avaliar(processo: dict, regras: dict | None = None) -> tuple[str, list[str]]:
    """Avalia um processo normalizado. Retorna (resultado, motivos).

    resultado: 'incluido' quando passa em todos os critérios ativos,
    'excluido' caso contrário. Nenhuma exclusão é destrutiva: o processo
    permanece no banco com a decisão e os motivos registrados.
    """
    regras = regras or carregar_regras()
    motivos = []
    aprovado = True

    if regras.get("aplicar_filtro_classe"):
        padroes = regras.get("classes_aceitas_padroes") or []
        classe = (processo.get("classe_nome") or "").casefold()
        if any(re.search(p, classe) for p in padroes):
            motivos.append(f"classe aceita: {processo.get('classe_nome')!r}")
        else:
            aprovado = False
            motivos.append(
                f"classe {processo.get('classe_nome')!r} fora dos padrões aceitos"
            )

    if regras.get("aplicar_filtro_assunto"):
        codigos_aceitos = set(map(str, regras.get("assuntos_codigos_incluir") or []))
        padroes = regras.get("assuntos_padroes_incluir") or []
        assuntos = processo.get("assuntos") or []
        acertos = []
        for assunto in assuntos:
            nome = (assunto.get("nome") or "").casefold()
            codigo = str(assunto.get("codigo") or "")
            if codigo and codigo in codigos_aceitos:
                acertos.append(f"código TPU {codigo} ({assunto.get('nome')})")
            elif any(re.search(p, nome) for p in padroes):
                acertos.append(f"assunto correspondeu a padrão: {assunto.get('nome')!r}")
        if acertos:
            motivos.extend(acertos)
        else:
            aprovado = False
            nomes = [a.get("nome", "") for a in assuntos] or ["(sem assuntos registrados)"]
            motivos.append(f"nenhum assunto correspondeu aos critérios: {nomes}")

    return ("incluido" if aprovado else "excluido"), motivos
