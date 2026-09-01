"""Gerador do rascunho do Caderno Técnico de Uso de IA (Produto 2, PRD §8).

Monta um documento Markdown exclusivamente a partir do que está de fato
registrado no banco e na auditoria — ferramentas usadas, protocolos executados
(sessões reais, com consultas exatas), critérios de validação e métricas
apuradas, limitações conhecidas e medidas de rastreabilidade. Seções sem dados
declaram explicitamente a ausência; nada é inventado.
"""

import json
from datetime import datetime, timezone

from . import config, metricas

LIMITACOES_CONHECIDAS = [
    "Preenchimento inconsistente dos códigos TPU entre tribunais/unidades pode "
    "gerar falsos negativos no pré-filtro estrutural (PRD §7 e §13); por isso o "
    "filtro estrutural reduz volume mas não decide sozinho.",
    "Rate limit do DataJud (~30 req/min) observado empiricamente, não documentado "
    "oficialmente pelo CNJ (PRD §6).",
    "A extração de inteiro teor (Judit.io) cobra por processo e depende de "
    "credencial comercial; a cobertura de anexos para decisões antigas ou de TRTs "
    "específicos não está confirmada (PRD §13).",
    "A classificação semântica heurística depende de regras lexicais versionadas; "
    "os limiares e termos devem ser validados com a amostra de controle antes do "
    "go/no-go da Fase 3 (PRD §5).",
    "Classificação executada apenas sobre metadados (sem inteiro teor) tem teto de "
    "resultado 'revisar' — inclusão definitiva exige o texto integral.",
]


def _fmt(valor):
    return f"{valor:.1%}" if isinstance(valor, float) else "—"


def gerar(con) -> str:
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sessoes = [dict(l) for l in con.execute(
        "SELECT * FROM sessoes_busca ORDER BY id").fetchall()]
    n_processos = con.execute("SELECT COUNT(*) AS n FROM processos").fetchone()["n"]
    n_eventos = con.execute("SELECT COUNT(*) AS n FROM auditoria").fetchone()["n"]
    m = metricas.calcular(con)

    linhas = [
        "# Caderno Técnico de Uso de Inteligência Artificial — RASCUNHO GERADO",
        "",
        f"Gerado automaticamente em {agora} pelo agente de coleta e filtragem "
        f"jurisprudencial, versão {config.VERSAO_PIPELINE} "
        f"(hash da configuração vigente: `{config.hash_configuracao()}`).",
        "",
        "Este rascunho reúne apenas o que está registrado no banco e na trilha de "
        "auditoria desta instalação. É insumo para o Produto 2 do Edital TST "
        "01/2026 — a redação final cabe à equipe de pesquisa.",
        "",
        "## 1. Descrição das ferramentas utilizadas",
        "",
        "- **DataJud (CNJ)** — API pública de metadados processuais "
        "(REST/Elasticsearch, endpoints por tribunal). Etapa 1: descoberta e filtro.",
        "- **Judit.io** — extração de inteiro teor por número CNJ "
        "(`search_type: lawsuit_cnj`, `with_attachments: true`). Etapa 2, serviço pago.",
        "- **Pré-filtro estrutural** — critérios declarativos sobre classe processual "
        "e assuntos TPU (`config/filtros_estruturais.json`).",
        "- **Classificador semântico heurístico** — regras lexicais versionadas "
        "(`config/regras_semanticas.json`) que distinguem menção profissional "
        "('o médico prestava serviços') de uso adjetivo ('atestado médico').",
        "- **Camada opcional de segunda opinião via LLM** (API da Anthropic), "
        "registrada como camada separada, sem substituir a heurística.",
        "",
        "## 2. Protocolos de utilização (sessões executadas)",
        "",
    ]
    if sessoes:
        for s in sessoes:
            linhas.append(
                f"- Sessão {s['id']} ({s['tipo']}), {s['iniciada_em']}, tribunal "
                f"{s['tribunal']}, pesquisador(a): {s['pesquisador'] or '—'}, "
                f"status: {s['status']}; retornados: {s['total_retornados']}, "
                f"novos: {s['total_novos']}; pipeline {s['versao_pipeline']}, "
                f"config `{s['hash_config']}`."
            )
        linhas.append("")
        linhas.append(
            "A consulta exata (corpo Elasticsearch) de cada sessão está gravada em "
            "`sessoes_busca.consulta_json` e na trilha de auditoria — protocolo "
            "replicável por qualquer pesquisador."
        )
    else:
        linhas.append("Nenhuma sessão de busca registrada até o momento.")
    linhas += [
        "",
        f"Base atual: {n_processos} registro(s) de processo; {n_eventos} evento(s) "
        "de auditoria.",
        "",
        "## 3. Critérios de validação e teste",
        "",
        f"- Precisão mínima exigida: {metricas.PRECISAO_MINIMA:.0%}; cobertura "
        f"mínima: {metricas.COBERTURA_MINIMA:.0%} (PRD §5).",
        f"- Precisão apurada: {_fmt(m['precisao']['valor'])} "
        f"({m['precisao']['verdadeiros_positivos']} relevantes / "
        f"{m['precisao']['total_revisados']} revisados).",
        f"- Cobertura do filtro sobre a amostra de controle: "
        f"{_fmt(m['cobertura']['filtro'])} "
        f"({m['cobertura']['incluidos_pelo_filtro']} de "
        f"{m['cobertura']['total_amostra_controle']} casos de controle).",
    ]
    aviso = m["go_no_go_fase3"]["aviso"]
    if aviso:
        linhas.append(f"- ⚠️ {aviso}")
    go = m["go_no_go_fase3"]["resultado"]
    linhas.append(
        "- Go/no-go Fase 3: "
        + ("sem dados suficientes para apuração." if go is None
           else ("critérios ATENDIDOS." if go else "critérios NÃO atendidos."))
    )
    linhas += ["", "## 4. Limitações identificadas", ""]
    linhas += [f"- {l}" for l in LIMITACOES_CONHECIDAS]
    linhas += [
        "",
        "## 5. Medidas de rastreabilidade, transparência e replicabilidade",
        "",
        "- Toda decisão de inclusão/exclusão (estrutural e semântica) é registrada "
        "com camada, resultado, score, motivos detalhados (regra acionada + trecho "
        "do texto) e versão das regras vigentes.",
        "- Trilha de auditoria em dupla via: tabela `auditoria` (SQLite) e arquivo "
        "apensável `data/auditoria.jsonl`; cada evento carrega versão do pipeline "
        "e hash da configuração.",
        "- Critérios de filtro declarados em arquivos JSON versionados no "
        "repositório (`config/`), com campo `versao` próprio.",
        "- Consulta exata enviada ao DataJud gravada por sessão; solicitações à "
        "Judit registradas com request_id.",
        "- Revisões humanas (rótulos relevante/irrelevante) e amostra de controle "
        "gravadas no banco, base do cálculo de precisão e cobertura.",
        "- Nenhum dado é fabricado: sem credencial da Judit as solicitações ficam "
        "registradas como pendentes; sem texto integral a classificação declara "
        "'aguardando_texto'.",
    ]
    conteudo = "\n".join(linhas) + "\n"
    config.garantir_diretorios()
    caminho = config.DIR_DOCS / "caderno_tecnico_rascunho.md"
    caminho.write_text(conteudo, encoding="utf-8")
    return str(caminho)
