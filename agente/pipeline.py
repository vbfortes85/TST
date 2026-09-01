"""Orquestração do pipeline: coleta → pré-filtro → dedup → extração → semântica.

Cada função opera sobre o banco e registra tudo na auditoria. A coleta funciona
por sessão de busca (registro por sessão do PRD §6.1); a atualização incremental
(camada 6, PRD §7) reaproveita o maior @timestamp já visto por consulta para
captar apenas decisões novas, sem reprocessar a base inteira.
"""

import hashlib
import json

from . import auditoria, classificador, config, datajud, db, dedup, filtros


def _chave_incremental(tribunal: str, filtros_dict: dict) -> str:
    h = hashlib.sha256(json.dumps(filtros_dict, sort_keys=True).encode()).hexdigest()[:12]
    return f"{tribunal.upper()}:{h}"


def _upsert_processo(con, norm: dict, sessao_id: int) -> tuple[int, bool]:
    """Insere o processo se inédito (numero+tribunal+grau). Retorna (id, novo?)."""
    numero = dedup.normalizar_numero(norm["numero"])
    existente = con.execute(
        "SELECT id FROM processos WHERE numero_cnj = ? AND tribunal = ? AND grau = ?",
        (numero, norm["tribunal"], norm["grau"]),
    ).fetchone()
    if existente:
        return existente["id"], False
    cur = con.execute(
        """INSERT INTO processos (numero_cnj, numero_formatado, tribunal, grau,
           classe_codigo, classe_nome, assuntos_json, orgao_julgador,
           data_ajuizamento, timestamp_fonte, fonte, raw_json, sessao_id, criado_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            numero,
            dedup.formatar_numero(norm["numero"]) or norm["numero"],
            norm["tribunal"],
            norm["grau"],
            norm["classe_codigo"],
            norm["classe_nome"],
            json.dumps(norm["assuntos"], ensure_ascii=False),
            norm["orgao_julgador"],
            norm["data_ajuizamento"],
            norm["timestamp_fonte"],
            "datajud",
            json.dumps(norm, ensure_ascii=False),
            sessao_id,
            auditoria.agora(),
        ),
    )
    return cur.lastrowid, True


def _registrar_classificacao(con, processo_id: int, camada: str, resultado: str,
                             motivos, versao: str, score=None, base=None) -> None:
    con.execute(
        """INSERT INTO classificacoes (processo_id, camada, resultado, score, base,
           motivos_json, versao_regras, registrada_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (processo_id, camada, resultado, score, base,
         json.dumps(motivos, ensure_ascii=False), versao, auditoria.agora()),
    )
    auditoria.registrar(con, "decisao_filtro", {
        "processo_id": processo_id,
        "camada": camada,
        "resultado": resultado,
        "score": score,
        "versao_regras": versao,
    })


def executar_coleta(con, tribunal: str, max_paginas: int = 3,
                    tamanho_pagina: int = 100, pesquisador: str = "",
                    incremental: bool = False) -> dict:
    """Etapa 1 completa: DataJud → normalização → dedup/upsert → pré-filtro."""
    regras = filtros.carregar_regras()
    chave = _chave_incremental(tribunal, regras)

    apos_timestamp = None
    if incremental:
        estado = con.execute(
            "SELECT ultimo_timestamp FROM estado_incremental WHERE chave = ?", (chave,)
        ).fetchone()
        apos_timestamp = estado["ultimo_timestamp"] if estado else None

    consulta_inicial = datajud.construir_consulta(
        regras, tamanho=tamanho_pagina, apos_timestamp=apos_timestamp
    )
    cur = con.execute(
        """INSERT INTO sessoes_busca (iniciada_em, tribunal, filtros_json,
           consulta_json, pesquisador, versao_pipeline, hash_config, tipo)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (auditoria.agora(), tribunal.upper(), json.dumps(regras, ensure_ascii=False),
         json.dumps(consulta_inicial, ensure_ascii=False), pesquisador,
         config.VERSAO_PIPELINE, config.hash_configuracao(),
         "incremental" if incremental else "coleta"),
    )
    sessao_id = cur.lastrowid
    auditoria.registrar(con, "sessao_iniciada", {
        "sessao_id": sessao_id, "tribunal": tribunal.upper(),
        "incremental": incremental, "apos_timestamp": apos_timestamp,
        "consulta": consulta_inicial,
    })
    con.commit()

    total_retornados = total_novos = 0
    maior_timestamp = apos_timestamp or ""
    search_after = None
    try:
        for pagina in range(max_paginas):
            consulta = datajud.construir_consulta(
                regras, tamanho=tamanho_pagina,
                search_after=search_after, apos_timestamp=apos_timestamp,
            )
            resposta = datajud.buscar(tribunal, consulta)
            hits, search_after, total_informado = datajud.extrair_hits(resposta)
            auditoria.registrar(con, "consulta_datajud", {
                "sessao_id": sessao_id, "pagina": pagina + 1,
                "hits_retornados": len(hits), "total_informado_pela_api": total_informado,
            })
            if not hits:
                break
            for hit in hits:
                norm = datajud.normalizar_fonte(hit.get("_source", {}))
                if not norm["numero"]:
                    continue
                total_retornados += 1
                processo_id, novo = _upsert_processo(con, norm, sessao_id)
                if novo:
                    total_novos += 1
                    resultado, motivos = filtros.avaliar(norm, regras)
                    _registrar_classificacao(
                        con, processo_id, "estrutural", resultado, motivos,
                        regras.get("versao", "?"),
                    )
                if norm["timestamp_fonte"] and norm["timestamp_fonte"] > maior_timestamp:
                    maior_timestamp = norm["timestamp_fonte"]
            con.commit()
            if len(hits) < tamanho_pagina:
                break
        status, erro = "concluida", None
    except datajud.ErroDataJud as e:
        status, erro = "erro", str(e)
        auditoria.registrar(con, "erro_coleta", {"sessao_id": sessao_id, "erro": erro})

    con.execute(
        "UPDATE sessoes_busca SET total_retornados = ?, total_novos = ?,"
        " status = ?, erro = ? WHERE id = ?",
        (total_retornados, total_novos, status, erro, sessao_id),
    )
    if maior_timestamp:
        con.execute(
            """INSERT INTO estado_incremental (chave, tribunal, filtros_json,
               ultimo_timestamp, ultima_execucao) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(chave) DO UPDATE SET ultimo_timestamp = excluded.ultimo_timestamp,
               ultima_execucao = excluded.ultima_execucao""",
            (chave, tribunal.upper(), json.dumps(filtros.carregar_regras(), ensure_ascii=False),
             maior_timestamp, auditoria.agora()),
        )
    con.commit()
    return {
        "sessao_id": sessao_id, "status": status, "erro": erro,
        "total_retornados": total_retornados, "total_novos": total_novos,
        "incremental": incremental,
    }


def processar_dados_normalizados(con, registros: list[dict], sessao_meta: dict) -> dict:
    """Ingesta registros já normalizados (uso em testes e cargas manuais).

    Mesmo caminho de dedup + pré-filtro da coleta via API, sem chamada de rede.
    """
    regras = filtros.carregar_regras()
    cur = con.execute(
        """INSERT INTO sessoes_busca (iniciada_em, tribunal, filtros_json,
           consulta_json, pesquisador, versao_pipeline, hash_config, tipo, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'concluida')""",
        (auditoria.agora(), sessao_meta.get("tribunal", "?"),
         json.dumps(regras, ensure_ascii=False),
         json.dumps(sessao_meta, ensure_ascii=False),
         sessao_meta.get("pesquisador", ""), config.VERSAO_PIPELINE,
         config.hash_configuracao(), sessao_meta.get("tipo", "carga_manual")),
    )
    sessao_id = cur.lastrowid
    novos = 0
    for norm in registros:
        processo_id, novo = _upsert_processo(con, norm, sessao_id)
        if novo:
            novos += 1
            resultado, motivos = filtros.avaliar(norm, regras)
            _registrar_classificacao(con, processo_id, "estrutural", resultado,
                                     motivos, regras.get("versao", "?"))
    con.execute(
        "UPDATE sessoes_busca SET total_retornados = ?, total_novos = ? WHERE id = ?",
        (len(registros), novos, sessao_id),
    )
    con.commit()
    return {"sessao_id": sessao_id, "total_retornados": len(registros), "total_novos": novos}


def reaplicar_filtro_estrutural(con) -> dict:
    """Reavalia o filtro estrutural sobre TODA a base com a configuração atual.

    Permite calibrar config/filtros_estruturais.json sem recoletar: cada
    processo ganha uma nova linha de classificação (o histórico anterior é
    preservado — a decisão vigente é sempre a mais recente).
    """
    regras = filtros.carregar_regras()
    linhas = con.execute(
        "SELECT id, classe_nome, assuntos_json FROM processos"
    ).fetchall()
    resumo = {"reavaliados": 0, "incluido": 0, "excluido": 0,
              "versao_regras": regras.get("versao", "?"),
              "hash_config": config.hash_configuracao()}
    for linha in linhas:
        norm = {
            "classe_nome": linha["classe_nome"],
            "assuntos": json.loads(linha["assuntos_json"] or "[]"),
        }
        resultado, motivos = filtros.avaliar(norm, regras)
        _registrar_classificacao(con, linha["id"], "estrutural", resultado,
                                 motivos, regras.get("versao", "?"))
        resumo["reavaliados"] += 1
        resumo[resultado] += 1
    auditoria.registrar(con, "filtro_estrutural_reaplicado", resumo)
    con.commit()
    return resumo


def diagnostico_base(con, topo: int = 15) -> dict:
    """Distribuições reais da base coletada, para calibração dos filtros.

    Mostra o que de fato veio do DataJud (classes e assuntos mais frequentes)
    e por que as exclusões estruturais aconteceram (classe, assunto ou ambos).
    """
    classes = [dict(l) for l in con.execute(
        """SELECT classe_nome, COUNT(*) AS n FROM processos
           GROUP BY classe_nome ORDER BY n DESC LIMIT ?""", (topo,)).fetchall()]

    contagem_assuntos: dict[str, int] = {}
    for linha in con.execute("SELECT assuntos_json FROM processos").fetchall():
        for assunto in json.loads(linha["assuntos_json"] or "[]"):
            nome = assunto.get("nome") or "(sem nome)"
            codigo = str(assunto.get("codigo") or "")
            chave = f"{nome} [TPU {codigo}]" if codigo else nome
            contagem_assuntos[chave] = contagem_assuntos.get(chave, 0) + 1
    assuntos = sorted(contagem_assuntos.items(), key=lambda kv: -kv[1])[:topo]

    vigentes = con.execute("""
        SELECT c.resultado, c.motivos_json FROM classificacoes c
        WHERE c.camada = 'estrutural'
          AND c.id = (SELECT MAX(c2.id) FROM classificacoes c2
                      WHERE c2.processo_id = c.processo_id
                        AND c2.camada = 'estrutural')
    """).fetchall()
    resultados = {"incluido": 0, "excluido": 0}
    motivos_exclusao = {"classe": 0, "assunto": 0, "classe_e_assunto": 0}
    for linha in vigentes:
        resultados[linha["resultado"]] = resultados.get(linha["resultado"], 0) + 1
        if linha["resultado"] == "excluido":
            motivos = json.loads(linha["motivos_json"])
            por_classe = any(isinstance(m, str) and "fora dos padrões" in m
                             for m in motivos)
            por_assunto = any(isinstance(m, str) and "nenhum assunto" in m
                              for m in motivos)
            if por_classe and por_assunto:
                motivos_exclusao["classe_e_assunto"] += 1
            elif por_classe:
                motivos_exclusao["classe"] += 1
            elif por_assunto:
                motivos_exclusao["assunto"] += 1
    return {
        "total_processos": con.execute(
            "SELECT COUNT(*) AS n FROM processos").fetchone()["n"],
        "classes_mais_frequentes": classes,
        "assuntos_mais_frequentes": [
            {"assunto": nome, "n": n} for nome, n in assuntos],
        "filtro_estrutural_vigente": resultados,
        "motivos_de_exclusao": motivos_exclusao,
    }


def relatorio_diagnostico(con) -> str:
    """Relatório em texto para depuração remota da calibração dos filtros.

    Reúne versão, últimas sessões (com a consulta exata enviada ao DataJud),
    distribuições da base e amostras reais de registros incluídos/excluídos
    com seus motivos — tudo copiável em um bloco só.
    """
    linhas = [
        "=== RELATÓRIO DE DIAGNÓSTICO DO AGENTE ===",
        f"pipeline: v{config.VERSAO_PIPELINE} | hash config: {config.hash_configuracao()}",
        "",
        "--- Últimas sessões de busca ---",
    ]
    sessoes = con.execute(
        "SELECT * FROM sessoes_busca ORDER BY id DESC LIMIT 3").fetchall()
    if not sessoes:
        linhas.append("(nenhuma sessão registrada)")
    for s in sessoes:
        linhas.append(
            f"sessão {s['id']} ({s['tipo']}) {s['iniciada_em']} {s['tribunal']}"
            f" status={s['status']} retornados={s['total_retornados']}"
            f" novos={s['total_novos']} erro={s['erro'] or '-'}")
        linhas.append(f"  consulta enviada: {s['consulta_json']}")

    diag = diagnostico_base(con)
    linhas += [
        "",
        "--- Base ---",
        f"total de processos: {diag['total_processos']}",
        f"filtro estrutural vigente: {json.dumps(diag['filtro_estrutural_vigente'])}",
        f"motivos de exclusão: {json.dumps(diag['motivos_de_exclusao'])}",
        "classes mais frequentes: " + json.dumps(
            diag["classes_mais_frequentes"], ensure_ascii=False),
        "assuntos mais frequentes: " + json.dumps(
            diag["assuntos_mais_frequentes"], ensure_ascii=False),
        "",
        "--- Amostras reais (metadados + decisão do filtro) ---",
    ]
    amostras = con.execute("""
        SELECT p.numero_formatado, p.classe_nome, p.grau, p.assuntos_json,
               c.resultado, c.motivos_json
        FROM processos p JOIN classificacoes c ON c.processo_id = p.id
        WHERE c.camada = 'estrutural'
          AND c.id = (SELECT MAX(c2.id) FROM classificacoes c2
                      WHERE c2.processo_id = p.id AND c2.camada = 'estrutural')
        ORDER BY (c.resultado = 'excluido'), p.id LIMIT 6
    """).fetchall()
    if not amostras:
        linhas.append("(base vazia)")
    for a in amostras:
        linhas.append(
            f"[{a['resultado']}] {a['numero_formatado']} | classe={a['classe_nome']!r}"
            f" grau={a['grau']} | assuntos={a['assuntos_json']}")
        linhas.append(f"  motivos: {a['motivos_json']}")
    return "\n".join(linhas)


def casos_deduplicados(con) -> list[dict]:
    """Agrupa aparições pelo número CNJ e elege a aparição canônica (dedup)."""
    linhas = [dict(l) for l in con.execute("SELECT * FROM processos").fetchall()]
    grupos: dict[str, list[dict]] = {}
    for linha in linhas:
        grupos.setdefault(linha["numero_cnj"], []).append(linha)
    casos = []
    for numero, aparicoes in grupos.items():
        canonico = dedup.escolher_canonico(aparicoes)
        casos.append({
            "numero_cnj": numero,
            "numero_formatado": canonico["numero_formatado"],
            "canonico_id": canonico["id"],
            "grau_canonico": canonico["grau"],
            "aparicoes": [
                {"id": a["id"], "tribunal": a["tribunal"], "grau": a["grau"]}
                for a in aparicoes
            ],
        })
    return casos


def executar_classificacao_semantica(con, usar_metadados: bool = False,
                                     usar_llm: bool = False, limite: int = 500) -> dict:
    """Camada semântica sobre os aprovados no pré-filtro estrutural.

    Usa o texto integral quando disponível (documentos recebidos da Judit);
    sem texto, ou marca 'aguardando_texto' ou — se usar_metadados=True —
    classifica sobre metadados com teto de resultado em 'revisar'.
    """
    regras = classificador.carregar_regras()
    candidatos = con.execute("""
        SELECT p.* FROM processos p
        WHERE p.id IN (
            SELECT c.processo_id FROM classificacoes c
            WHERE c.camada = 'estrutural' AND c.resultado = 'incluido')
        AND p.id NOT IN (
            SELECT c.processo_id FROM classificacoes c
            WHERE c.camada = 'semantica' AND c.resultado != 'aguardando_texto')
        LIMIT ?""", (limite,)).fetchall()

    resumo = {"classificados": 0, "aguardando_texto": 0, "com_llm": 0}
    llm_ok = usar_llm and classificador.llm_disponivel()
    for proc in candidatos:
        doc = con.execute(
            "SELECT conteudo_texto FROM documentos WHERE processo_id = ?"
            " AND status = 'recebido' AND conteudo_texto IS NOT NULL"
            " ORDER BY id DESC LIMIT 1", (proc["id"],)
        ).fetchone()
        if doc and doc["conteudo_texto"]:
            texto, base = doc["conteudo_texto"], "texto_integral"
        elif usar_metadados:
            assuntos = " ; ".join(
                a.get("nome", "") for a in json.loads(proc["assuntos_json"] or "[]")
            )
            texto = f"{proc['classe_nome']} — {assuntos} — {proc['orgao_julgador']}"
            base = "metadados"
        else:
            ja_aguardando = con.execute(
                "SELECT 1 FROM classificacoes WHERE processo_id = ? AND camada = 'semantica'"
                " AND resultado = 'aguardando_texto' LIMIT 1", (proc["id"],)
            ).fetchone()
            if not ja_aguardando:
                _registrar_classificacao(
                    con, proc["id"], "semantica", "aguardando_texto",
                    [{"regra": "sem_texto_integral",
                      "detalhe": "classificação plena exige inteiro teor (Etapa 2)"}],
                    regras["versao"],
                )
            resumo["aguardando_texto"] += 1
            continue

        veredicto = classificador.classificar_texto(texto, regras, base=base)
        _registrar_classificacao(
            con, proc["id"], "semantica", veredicto["resultado"], veredicto["motivos"],
            veredicto["versao_regras"], score=veredicto["score"], base=veredicto["base"],
        )
        resumo["classificados"] += 1
        if llm_ok and base == "texto_integral":
            veredicto_llm = classificador.classificar_llm(texto)
            _registrar_classificacao(
                con, proc["id"], "semantica_llm", veredicto_llm["resultado"],
                veredicto_llm["motivos"], veredicto_llm["versao_regras"],
                base=veredicto_llm["base"],
            )
            resumo["com_llm"] += 1
    con.commit()
    resumo["llm_disponivel"] = classificador.llm_disponivel()
    return resumo
