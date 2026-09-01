"""Entrega/exportação (camada 5 do pipeline, PRD §7).

Gera a planilha de metadados (CSV, colunas alinhadas ao registro por acórdão do
PRD §6.1) e o dump completo (JSONL) que alimentam a análise quantitativa dos
Produtos 3 e 4. Cada exportação é registrada na auditoria.
"""

import csv
import json
from datetime import datetime, timezone

from . import auditoria, config

COLUNAS_CSV = [
    "numero_cnj_formatado",
    "tribunal",
    "grau",
    "classe_processual",
    "orgao_julgador",
    "data_ajuizamento",
    "assuntos",
    "filtro_estrutural",
    "classificacao_semantica",
    "score_semantico",
    "base_classificacao",
    "revisao_humana",
    "status_inteiro_teor",
    "sessao_origem",
    "data_coleta",
]


def _linhas_base(con) -> list[dict]:
    sql = """
    SELECT p.*,
        (SELECT resultado FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'estrutural' ORDER BY c.id DESC LIMIT 1) AS estrutural,
        (SELECT resultado FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'semantica' ORDER BY c.id DESC LIMIT 1) AS semantica,
        (SELECT score FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'semantica' ORDER BY c.id DESC LIMIT 1) AS score_semantica,
        (SELECT base FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'semantica' ORDER BY c.id DESC LIMIT 1) AS base_semantica,
        (SELECT rotulo FROM revisoes r WHERE r.processo_id = p.id) AS revisao,
        (SELECT status FROM documentos d WHERE d.processo_id = p.id
            ORDER BY d.id DESC LIMIT 1) AS status_documento
    FROM processos p ORDER BY p.numero_cnj, p.id
    """
    return [dict(l) for l in con.execute(sql).fetchall()]


def exportar(con, formato: str = "csv") -> str:
    """Exporta a base. Retorna o caminho do arquivo gerado."""
    config.garantir_diretorios()
    carimbo = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    linhas = _linhas_base(con)

    if formato == "jsonl":
        caminho = config.DIR_EXPORTS / f"base_completa_{carimbo}.jsonl"
        with open(caminho, "w", encoding="utf-8") as f:
            for linha in linhas:
                f.write(json.dumps(linha, ensure_ascii=False) + "\n")
    elif formato == "csv":
        caminho = config.DIR_EXPORTS / f"planilha_metadados_{carimbo}.csv"
        with open(caminho, "w", encoding="utf-8", newline="") as f:
            escritor = csv.writer(f)
            escritor.writerow(COLUNAS_CSV)
            for linha in linhas:
                assuntos = "; ".join(
                    a.get("nome", "") for a in json.loads(linha.get("assuntos_json") or "[]")
                )
                escritor.writerow([
                    linha.get("numero_formatado"),
                    linha.get("tribunal"),
                    linha.get("grau"),
                    linha.get("classe_nome"),
                    linha.get("orgao_julgador"),
                    linha.get("data_ajuizamento"),
                    assuntos,
                    linha.get("estrutural") or "",
                    linha.get("semantica") or "",
                    linha.get("score_semantica") if linha.get("score_semantica") is not None else "",
                    linha.get("base_semantica") or "",
                    linha.get("revisao") or "",
                    linha.get("status_documento") or "",
                    linha.get("sessao_id"),
                    linha.get("criado_em"),
                ])
    else:
        raise ValueError(f"Formato desconhecido: {formato!r} (use 'csv' ou 'jsonl')")

    auditoria.registrar(con, "exportacao", {
        "formato": formato,
        "arquivo": str(caminho),
        "total_linhas": len(linhas),
    })
    return str(caminho)
