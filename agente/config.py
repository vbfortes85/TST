"""Configuração central do agente.

Todas as credenciais vêm de variáveis de ambiente — nada sensível fica no código.
A versão do pipeline e o hash dos arquivos de configuração são gravados em cada
evento de auditoria, atendendo à exigência de rastreabilidade/replicabilidade do
Caderno Técnico de Uso de IA (Produto 2 do Edital TST 01/2026).
"""

import hashlib
import json
import os
from pathlib import Path

VERSAO_PIPELINE = "0.1.2"

RAIZ = Path(__file__).resolve().parent.parent
DIR_CONFIG = RAIZ / "config"
DIR_DADOS = Path(os.environ.get("AGENTE_DIR_DADOS", RAIZ / "data"))
DIR_EXPORTS = Path(os.environ.get("AGENTE_DIR_EXPORTS", RAIZ / "exports"))
DIR_DOCS = RAIZ / "docs"

CAMINHO_BANCO = Path(os.environ.get("AGENTE_BANCO", DIR_DADOS / "agente.sqlite3"))
CAMINHO_AUDITORIA = DIR_DADOS / "auditoria.jsonl"

# --- Etapa 1: DataJud (CNJ) -------------------------------------------------
# A chave pública do DataJud é divulgada pelo próprio CNJ na wiki oficial
# (https://datajud-wiki.cnj.jus.br/api-publica/acesso). O valor abaixo é a chave
# pública publicada pelo CNJ; se o CNJ a rotacionar, defina DATAJUD_API_KEY.
DATAJUD_API_KEY = os.environ.get(
    "DATAJUD_API_KEY",
    "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==",
)
DATAJUD_BASE_URL = os.environ.get(
    "DATAJUD_BASE_URL", "https://api-publica.datajud.cnj.jus.br"
)
# Rate limit observado ~30 req/min (não documentado oficialmente — PRD §6).
DATAJUD_RPM = int(os.environ.get("DATAJUD_RPM", "25"))

# Endpoints por tribunal (padrão documentado pelo CNJ: api_publica_<tribunal>).
TRIBUNAIS = {"TST": "api_publica_tst"}
for _n in range(1, 25):
    TRIBUNAIS[f"TRT{_n}"] = f"api_publica_trt{_n}"

# --- Etapa 2: Judit.io ------------------------------------------------------
# Serviço pago; requer credencial comercial (cotação: atendimento@judit.io).
# Sem chave configurada o agente registra as solicitações como pendentes de
# credencial — nunca fabrica documentos.
JUDIT_API_KEY = os.environ.get("JUDIT_API_KEY", "")
JUDIT_BASE_URL = os.environ.get("JUDIT_BASE_URL", "https://requests.prod.judit.io")

# --- Camada semântica opcional via API da Anthropic -------------------------
# Usada apenas se o pacote `anthropic` estiver instalado e houver credencial.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")


def carregar_config(nome: str) -> dict:
    with open(DIR_CONFIG / nome, encoding="utf-8") as f:
        return json.load(f)


def hash_configuracao() -> str:
    """Hash SHA-256 dos arquivos de configuração de filtro/regras.

    Muda sempre que qualquer critério de inclusão/exclusão muda — permite
    associar cada decisão registrada à versão exata dos critérios vigentes.
    """
    h = hashlib.sha256()
    for arquivo in sorted(DIR_CONFIG.glob("*.json")):
        h.update(arquivo.name.encode())
        h.update(arquivo.read_bytes())
    return h.hexdigest()[:16]


def garantir_diretorios() -> None:
    for d in (DIR_DADOS, DIR_EXPORTS, DIR_DOCS):
        d.mkdir(parents=True, exist_ok=True)
