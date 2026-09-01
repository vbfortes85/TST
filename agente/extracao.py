"""Fluxo da Etapa 2: solicitação e recebimento de inteiro teor via Judit.

Sem credencial, as solicitações ficam registradas como 'pendente_credencial'
(nenhum documento é fabricado) e podem ser reprocessadas depois. O custo é
"varejo": uma chamada paga por processo (PRD §6) — por isso a extração parte
apenas dos casos aprovados no pré-filtro estrutural e deduplicados.
"""

from . import auditoria, judit, pipeline


def solicitar_pendentes(con, limite: int = 10) -> dict:
    """Solicita inteiro teor dos casos aprovados no filtro estrutural.

    Usa a aparição canônica de cada caso deduplicado, evitando pagar duas
    vezes pelo mesmo processo em graus diferentes.
    """
    casos = pipeline.casos_deduplicados(con)
    aprovados = []
    for caso in casos:
        estrutural = con.execute(
            "SELECT resultado FROM classificacoes WHERE processo_id = ?"
            " AND camada = 'estrutural' ORDER BY id DESC LIMIT 1",
            (caso["canonico_id"],),
        ).fetchone()
        ja_tem = con.execute(
            "SELECT 1 FROM documentos WHERE processo_id = ?"
            " AND status IN ('solicitado', 'recebido') LIMIT 1",
            (caso["canonico_id"],),
        ).fetchone()
        if estrutural and estrutural["resultado"] == "incluido" and not ja_tem:
            aprovados.append(caso)

    resumo = {"solicitados": 0, "pendentes_credencial": 0, "erros": 0,
              "credencial_configurada": judit.credencial_configurada()}
    for caso in aprovados[:limite]:
        processo_id = caso["canonico_id"]
        if not judit.credencial_configurada():
            con.execute(
                """INSERT INTO documentos (processo_id, fonte, status, detalhe, atualizado_em)
                   VALUES (?, 'judit', 'pendente_credencial', ?, ?)""",
                (processo_id,
                 "JUDIT_API_KEY ausente — solicitação registrada, nenhum dado simulado"
                 " (cotação: atendimento@judit.io)",
                 auditoria.agora()),
            )
            auditoria.registrar(con, "judit_pendente_credencial",
                                {"processo_id": processo_id,
                                 "numero": caso["numero_formatado"]})
            resumo["pendentes_credencial"] += 1
            continue
        try:
            resposta = judit.solicitar_inteiro_teor(caso["numero_formatado"])
            request_id = str(resposta.get("request_id") or resposta.get("id") or "")
            con.execute(
                """INSERT INTO documentos (processo_id, fonte, status, request_id, atualizado_em)
                   VALUES (?, 'judit', 'solicitado', ?, ?)""",
                (processo_id, request_id, auditoria.agora()),
            )
            auditoria.registrar(con, "judit_solicitacao", {
                "processo_id": processo_id, "numero": caso["numero_formatado"],
                "request_id": request_id,
            })
            resumo["solicitados"] += 1
        except judit.ErroJudit as e:
            con.execute(
                """INSERT INTO documentos (processo_id, fonte, status, detalhe, atualizado_em)
                   VALUES (?, 'judit', 'erro', ?, ?)""",
                (processo_id, str(e), auditoria.agora()),
            )
            auditoria.registrar(con, "judit_erro",
                                {"processo_id": processo_id, "erro": str(e)})
            resumo["erros"] += 1
    con.commit()
    return resumo


def receber_respostas(con) -> dict:
    """Consulta as respostas das solicitações em aberto e grava os textos."""
    solicitados = con.execute(
        "SELECT * FROM documentos WHERE status = 'solicitado' AND request_id != ''"
    ).fetchall()
    resumo = {"consultados": 0, "recebidos": 0, "erros": 0}
    for doc in solicitados:
        resumo["consultados"] += 1
        try:
            resposta = judit.consultar_respostas(doc["request_id"])
            textos = judit.extrair_textos(resposta)
            if textos:
                principal = max(textos, key=lambda t: len(t["texto"]))
                con.execute(
                    """UPDATE documentos SET status = 'recebido', tipo_documento = ?,
                       conteudo_texto = ?, atualizado_em = ? WHERE id = ?""",
                    (principal["tipo"], principal["texto"], auditoria.agora(), doc["id"]),
                )
                auditoria.registrar(con, "judit_recebido", {
                    "documento_id": doc["id"], "processo_id": doc["processo_id"],
                    "anexos_com_texto": len(textos),
                    "tamanho_texto_principal": len(principal["texto"]),
                })
                resumo["recebidos"] += 1
        except judit.ErroJudit as e:
            auditoria.registrar(con, "judit_erro_consulta",
                                {"documento_id": doc["id"], "erro": str(e)})
            resumo["erros"] += 1
    con.commit()
    return resumo


def registrar_texto_manual(con, processo_id: int, texto: str, origem: str) -> int:
    """Registra inteiro teor obtido manualmente (ex.: PoC Jusbrasil, PRD §6.1).

    Mantém a rastreabilidade: a origem declarada fica gravada no documento e na
    auditoria, separando amostra manual da base de produção via API.
    """
    cur = con.execute(
        """INSERT INTO documentos (processo_id, fonte, status, tipo_documento,
           conteudo_texto, detalhe, atualizado_em)
           VALUES (?, 'manual', 'recebido', 'acordao', ?, ?, ?)""",
        (processo_id, texto, f"origem declarada: {origem}", auditoria.agora()),
    )
    auditoria.registrar(con, "texto_manual_registrado", {
        "processo_id": processo_id, "origem": origem, "tamanho": len(texto),
    })
    con.commit()
    return cur.lastrowid
