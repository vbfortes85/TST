"""Esquema SQLite e acesso ao banco.

O banco guarda o estado completo do pipeline: sessões de busca, processos,
decisões de cada camada de filtro, documentos de inteiro teor, revisões humanas,
amostra de controle e estado da atualização incremental.
"""

import sqlite3
from contextlib import contextmanager

from . import config

ESQUEMA = """
CREATE TABLE IF NOT EXISTS sessoes_busca (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iniciada_em TEXT NOT NULL,
    tribunal TEXT NOT NULL,
    filtros_json TEXT NOT NULL,
    consulta_json TEXT,
    total_retornados INTEGER DEFAULT 0,
    total_novos INTEGER DEFAULT 0,
    pesquisador TEXT,
    versao_pipeline TEXT NOT NULL,
    hash_config TEXT NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'coleta',      -- coleta | incremental
    status TEXT NOT NULL DEFAULT 'em_andamento', -- em_andamento | concluida | erro
    erro TEXT
);

CREATE TABLE IF NOT EXISTS processos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_cnj TEXT NOT NULL,                 -- 20 dígitos, normalizado
    numero_formatado TEXT NOT NULL,
    tribunal TEXT NOT NULL,
    grau TEXT,
    classe_codigo TEXT,
    classe_nome TEXT,
    assuntos_json TEXT,
    orgao_julgador TEXT,
    data_ajuizamento TEXT,
    timestamp_fonte TEXT,                     -- @timestamp do DataJud
    fonte TEXT NOT NULL DEFAULT 'datajud',
    raw_json TEXT,
    sessao_id INTEGER REFERENCES sessoes_busca(id),
    criado_em TEXT NOT NULL,
    UNIQUE (numero_cnj, tribunal, grau)
);

CREATE TABLE IF NOT EXISTS classificacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    processo_id INTEGER NOT NULL REFERENCES processos(id),
    camada TEXT NOT NULL,          -- estrutural | semantica | semantica_llm
    resultado TEXT NOT NULL,       -- incluido | excluido | revisar | aguardando_texto
    score REAL,
    base TEXT,                     -- texto_integral | metadados
    motivos_json TEXT NOT NULL,
    versao_regras TEXT NOT NULL,
    registrada_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    processo_id INTEGER NOT NULL REFERENCES processos(id),
    fonte TEXT NOT NULL DEFAULT 'judit',
    status TEXT NOT NULL,          -- pendente_credencial | solicitado | recebido | erro
    request_id TEXT,
    tipo_documento TEXT,
    conteudo_texto TEXT,
    detalhe TEXT,
    atualizado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    processo_id INTEGER NOT NULL REFERENCES processos(id),
    rotulo TEXT NOT NULL,          -- relevante | irrelevante
    revisor TEXT,
    observacao TEXT,
    registrada_em TEXT NOT NULL,
    UNIQUE (processo_id)
);

CREATE TABLE IF NOT EXISTS amostra_controle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_cnj TEXT NOT NULL UNIQUE,          -- 20 dígitos, normalizado
    origem TEXT,                              -- ex.: PoC manual Jusbrasil
    registrada_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estado_incremental (
    chave TEXT PRIMARY KEY,                   -- tribunal + hash dos filtros
    tribunal TEXT NOT NULL,
    filtros_json TEXT NOT NULL,
    ultimo_timestamp TEXT,
    ultima_execucao TEXT
);

CREATE TABLE IF NOT EXISTS auditoria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registrada_em TEXT NOT NULL,
    evento TEXT NOT NULL,
    detalhes_json TEXT NOT NULL,
    versao_pipeline TEXT NOT NULL,
    hash_config TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processos_numero ON processos(numero_cnj);
CREATE INDEX IF NOT EXISTS idx_classificacoes_processo ON classificacoes(processo_id);
"""


def conectar() -> sqlite3.Connection:
    config.garantir_diretorios()
    con = sqlite3.connect(config.CAMINHO_BANCO)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(ESQUEMA)
    return con


@contextmanager
def sessao():
    con = conectar()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
