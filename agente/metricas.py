"""Métricas de validação do filtro (PRD §5 e §10).

Precisão: entre os processos classificados como incluídos pela camada semântica
e já revisados por humano, a fração rotulada 'relevante'.

Cobertura: sobre a amostra de controle (números CNJ sabidamente relevantes,
p.ex. da PoC manual no Jusbrasil), mede-se (a) cobertura de captura — quantos
estão na base coletada — e (b) cobertura do filtro — quantos foram classificados
como incluídos. O critério go/no-go da Fase 3 (PRD §5) usa precisão ≥ 85% e
cobertura ≥ 90%, com amostra mínima de 200–300 processos (ou 10% da base).
"""

from . import dedup

PRECISAO_MINIMA = 0.85
COBERTURA_MINIMA = 0.90
AMOSTRA_MINIMA_RECOMENDADA = 200


def calcular(con) -> dict:
    incluidos_revisados = con.execute("""
        SELECT r.rotulo, COUNT(*) AS n FROM revisoes r
        JOIN processos p ON p.id = r.processo_id
        WHERE p.id IN (
            SELECT c.processo_id FROM classificacoes c
            WHERE c.camada = 'semantica' AND c.resultado = 'incluido'
              AND c.id = (SELECT MAX(c2.id) FROM classificacoes c2
                          WHERE c2.processo_id = c.processo_id AND c2.camada = 'semantica')
        )
        GROUP BY r.rotulo
    """).fetchall()
    contagens = {l["rotulo"]: l["n"] for l in incluidos_revisados}
    vp = contagens.get("relevante", 0)
    fp = contagens.get("irrelevante", 0)
    total_revisados = vp + fp
    precisao = (vp / total_revisados) if total_revisados else None

    controle = [dict(l) for l in con.execute("SELECT * FROM amostra_controle").fetchall()]
    capturados = 0
    filtrados = 0
    for item in controle:
        numero = dedup.normalizar_numero(item["numero_cnj"])
        na_base = con.execute(
            "SELECT id FROM processos WHERE numero_cnj = ?", (numero,)
        ).fetchall()
        if na_base:
            capturados += 1
            ids = tuple(l["id"] for l in na_base)
            marcadores = ",".join("?" for _ in ids)
            incluido = con.execute(
                f"""SELECT 1 FROM classificacoes c WHERE c.processo_id IN ({marcadores})
                    AND c.camada = 'semantica' AND c.resultado = 'incluido'
                    AND c.id = (SELECT MAX(c2.id) FROM classificacoes c2
                                WHERE c2.processo_id = c.processo_id
                                AND c2.camada = 'semantica') LIMIT 1""",
                ids,
            ).fetchone()
            if incluido:
                filtrados += 1
    total_controle = len(controle)
    cobertura_captura = (capturados / total_controle) if total_controle else None
    cobertura_filtro = (filtrados / total_controle) if total_controle else None

    amostra_suficiente = total_revisados >= AMOSTRA_MINIMA_RECOMENDADA and \
        total_controle >= AMOSTRA_MINIMA_RECOMENDADA
    go = None
    if precisao is not None and cobertura_filtro is not None:
        go = precisao >= PRECISAO_MINIMA and cobertura_filtro >= COBERTURA_MINIMA

    return {
        "precisao": {
            "valor": precisao,
            "verdadeiros_positivos": vp,
            "falsos_positivos": fp,
            "total_revisados": total_revisados,
            "minimo_exigido": PRECISAO_MINIMA,
        },
        "cobertura": {
            "captura": cobertura_captura,
            "filtro": cobertura_filtro,
            "capturados": capturados,
            "incluidos_pelo_filtro": filtrados,
            "total_amostra_controle": total_controle,
            "minimo_exigido": COBERTURA_MINIMA,
        },
        "go_no_go_fase3": {
            "resultado": go,
            "amostra_minima_recomendada": AMOSTRA_MINIMA_RECOMENDADA,
            "amostra_suficiente": amostra_suficiente,
            "aviso": None if amostra_suficiente else (
                "Amostra insuficiente para decisão confiável de go/no-go "
                f"(PRD §5 recomenda {AMOSTRA_MINIMA_RECOMENDADA}–300 processos "
                "ou 10% da base filtrada, o que for maior)."
            ),
        },
    }
