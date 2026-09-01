# Agente de Coleta e Filtragem Jurisprudencial — Pejotização nos Serviços de Saúde

Protótipo funcional do agente descrito no **PRD v0.1** (Edital de Chamamento
Público TST n.º 01/2026 — Eixo III, Pejotização das Relações de Trabalho).
Implementa o pipeline completo de duas etapas — **DataJud (descoberta/filtro) →
Judit.io (inteiro teor)** — com as seis camadas do PRD §7 e a trilha de
auditoria exigida pelo Caderno Técnico de Uso de IA (Produto 2, PRD §8).

## Como executar

Requisitos: **apenas Python 3.10+** (biblioteca padrão — nenhuma dependência
obrigatória).

```bash
python3 run.py            # abre em http://localhost:8000
python3 run.py 8080       # porta alternativa
```

Testes (pipeline completo sobre dados sintéticos rotulados como tais):

```bash
python3 -m unittest discover -s tests -v
```

## Variáveis de ambiente

| Variável | Obrigatória? | Função |
|---|---|---|
| `DATAJUD_API_KEY` | Não | Chave da API pública do DataJud. O padrão é a chave pública divulgada pelo CNJ (wiki oficial do DataJud); defina apenas se o CNJ a rotacionar. |
| `JUDIT_API_KEY` | Para a Etapa 2 | Credencial comercial da Judit.io (cotação: atendimento@judit.io). **Sem ela, as solicitações de inteiro teor ficam registradas como `pendente_credencial` — nenhum documento é simulado.** |
| `ANTHROPIC_API_KEY` | Não | Habilita a segunda opinião opcional via LLM na classificação semântica (requer também `pip install anthropic`). |
| `DATAJUD_RPM` | Não | Limite de requisições/min ao DataJud (padrão 25; limite observado ~30, PRD §6). |
| `PORTA` | Não | Porta do servidor (padrão 8000). |

## Entregas do PRD §7 ↔ implementação

| Camada do PRD | Módulo | Observações |
|---|---|---|
| 1. Ingestão (DataJud) | `agente/datajud.py` | TST + TRT1–24, paginação `search_after`, rate limit, consulta exata gravada por sessão. |
| 2. Pré-filtro estrutural (TPU) | `agente/filtros.py` + `config/filtros_estruturais.json` | Critérios declarativos versionados. A lista de códigos TPU inicia vazia de propósito: deve ser preenchida a partir de dados reais, validada com a coordenação — nenhum código foi presumido. |
| 3. Inteiro teor (Judit) | `agente/judit.py`, `agente/extracao.py` | `search_type: lawsuit_cnj` + `with_attachments: true`, uma chamada por processo (aparição canônica pós-dedup, para não pagar duas vezes o mesmo caso). Também aceita registro manual de texto (PoC Jusbrasil, PRD §6.1) com origem declarada. |
| 4. Classificação semântica | `agente/classificador.py` + `config/regras_semanticas.json` | Distingue “o médico prestava serviços” (profissão) de “atestado médico” (adjetivo). Cada decisão registra regra acionada + trecho do texto. Sem inteiro teor, o teto é `revisar`. Camada LLM opcional registrada separadamente. |
| 5. Dedup + entrega | `agente/dedup.py`, `agente/exportador.py` | Agrupamento pelo número CNJ único (Res. 65/2008); CSV com as colunas do registro por acórdão (PRD §6.1) + JSONL completo. |
| 6. Atualização incremental | `agente/pipeline.py` (`estado_incremental`) | Nova coleta filtra por `@timestamp` posterior ao último visto — sem reprocessar a base. |
| Rastreabilidade (Produto 2) | `agente/auditoria.py`, `agente/caderno.py` | Auditoria em dupla via (SQLite + `data/auditoria.jsonl`), com versão do pipeline e hash da configuração em cada evento; geração do rascunho do Caderno Técnico só a partir de registros reais. |
| Validação 85%/90% (PRD §5) | `agente/metricas.py` | Precisão via revisão humana dos incluídos; cobertura via amostra de controle; go/no-go da Fase 3 com aviso de amostra insuficiente (<200). |

## Compromissos de integridade

- **Nenhum dado é inventado.** Sem credencial da Judit, nada é extraído nem
  simulado; falhas de rede/API são registradas e exibidas verbatim; a
  classificação sem texto integral se declara `aguardando_texto`.
- Os dados em `tests/fixtures/` são **sintéticos**, rotulados como tais, e
  servem apenas para exercitar o pipeline nos testes.
- Toda decisão de inclusão/exclusão é auditável: camada, resultado, score,
  motivos (regra + trecho), versão das regras e hash da configuração.

## Estrutura

```
run.py                     # inicia o servidor (stdlib apenas)
agente/                    # módulos do pipeline (ver tabela acima)
agente/static/index.html   # front-end simples (abas 1–6 do fluxo)
config/                    # critérios de filtro versionados (JSON)
tests/                     # suíte de ponta a ponta (dados sintéticos)
data/                      # banco SQLite + auditoria.jsonl (gerados; fora do git)
exports/                   # planilhas exportadas (geradas; fora do git)
docs/caderno_tecnico_rascunho.md  # gerado sob demanda
```

## Status e pendências herdadas do PRD

- Preço da extração de anexos da Judit **não confirmado** — cotação pendente
  (PRD §6/§13); o protótipo limita solicitações por rodada para controle de custo.
- Termos de busca e códigos TPU a **validar com a coordenação acadêmica**
  (PRD §6.1/§14) — por isso são configuração, não código.
- Limiares da classificação semântica (`config/regras_semanticas.json`) são um
  ponto de partida a calibrar com a amostra de controle antes do go/no-go da
  Fase 3 (PRD §5).
