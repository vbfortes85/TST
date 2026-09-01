"""Trilha de auditoria (rastreabilidade, transparência e replicabilidade).

Cada evento relevante do pipeline — consulta enviada ao DataJud, decisão de
inclusão/exclusão de cada camada, solicitação à Judit, revisão humana,
exportação — é registrado em dois lugares: na tabela `auditoria` do banco e em
um arquivo JSONL apensável (data/auditoria.jsonl). Todo evento carrega a versão
do pipeline e o hash da configuração vigente, permitindo reconstituir com qual
critério cada decisão foi tomada. Esse registro é insumo direto do Caderno
Técnico de Uso de IA (Produto 2 do Edital TST 01/2026).
"""

import json
from datetime import datetime, timezone

from . import config


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def registrar(con, evento: str, detalhes: dict) -> None:
    registro = {
        "registrada_em": agora(),
        "evento": evento,
        "detalhes": detalhes,
        "versao_pipeline": config.VERSAO_PIPELINE,
        "hash_config": config.hash_configuracao(),
    }
    con.execute(
        "INSERT INTO auditoria (registrada_em, evento, detalhes_json,"
        " versao_pipeline, hash_config) VALUES (?, ?, ?, ?, ?)",
        (
            registro["registrada_em"],
            evento,
            json.dumps(detalhes, ensure_ascii=False),
            registro["versao_pipeline"],
            registro["hash_config"],
        ),
    )
    config.garantir_diretorios()
    with open(config.CAMINHO_AUDITORIA, "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def listar(con, limite: int = 200) -> list:
    linhas = con.execute(
        "SELECT * FROM auditoria ORDER BY id DESC LIMIT ?", (limite,)
    ).fetchall()
    return [dict(l) for l in linhas]
