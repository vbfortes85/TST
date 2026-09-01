"""Servidor HTTP do protótipo (stdlib apenas — sem dependências externas).

Expõe a API JSON consumida pelo front-end simples em agente/static/index.html.
Iniciar com: python3 run.py  (porta padrão 8000, variável PORTA para trocar).
"""

import json
import re
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import (auditoria, caderno, classificador, config, db, dedup,
               exportador, extracao, judit, metricas, pipeline)

DIR_STATIC = Path(__file__).resolve().parent / "static"


def _status(con) -> dict:
    contagens = {}
    for camada in ("estrutural", "semantica"):
        linhas = con.execute(
            """SELECT resultado, COUNT(*) AS n FROM classificacoes c
               WHERE camada = ? AND id = (SELECT MAX(id) FROM classificacoes c2
                   WHERE c2.processo_id = c.processo_id AND c2.camada = ?)
               GROUP BY resultado""", (camada, camada)).fetchall()
        contagens[camada] = {l["resultado"]: l["n"] for l in linhas}
    docs = con.execute(
        "SELECT status, COUNT(*) AS n FROM documentos GROUP BY status").fetchall()
    return {
        "versao_pipeline": config.VERSAO_PIPELINE,
        "hash_config": config.hash_configuracao(),
        "total_processos": con.execute("SELECT COUNT(*) AS n FROM processos").fetchone()["n"],
        "total_casos_deduplicados": len(pipeline.casos_deduplicados(con)),
        "classificacoes": contagens,
        "documentos": {l["status"]: l["n"] for l in docs},
        "judit_credencial": judit.credencial_configurada(),
        "llm_disponivel": classificador.llm_disponivel(),
        "tribunais": sorted(config.TRIBUNAIS.keys()),
        "eventos_auditoria": con.execute("SELECT COUNT(*) AS n FROM auditoria").fetchone()["n"],
    }


def _listar_processos(con, params: dict) -> list:
    limite = min(int(params.get("limite", 100)), 1000)
    filtro_estado = params.get("estado")
    sql = """
    SELECT p.id, p.numero_formatado, p.tribunal, p.grau, p.classe_nome,
           p.orgao_julgador, p.data_ajuizamento, p.assuntos_json,
        (SELECT resultado FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'estrutural' ORDER BY c.id DESC LIMIT 1) AS estrutural,
        (SELECT motivos_json FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'estrutural' ORDER BY c.id DESC LIMIT 1) AS estrutural_motivos,
        (SELECT resultado FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'semantica' ORDER BY c.id DESC LIMIT 1) AS semantica,
        (SELECT score FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'semantica' ORDER BY c.id DESC LIMIT 1) AS score,
        (SELECT motivos_json FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'semantica' ORDER BY c.id DESC LIMIT 1) AS semantica_motivos,
        (SELECT resultado FROM classificacoes c WHERE c.processo_id = p.id
            AND c.camada = 'semantica_llm' ORDER BY c.id DESC LIMIT 1) AS semantica_llm,
        (SELECT rotulo FROM revisoes r WHERE r.processo_id = p.id) AS revisao,
        (SELECT status FROM documentos d WHERE d.processo_id = p.id
            ORDER BY d.id DESC LIMIT 1) AS documento
    FROM processos p ORDER BY p.id DESC LIMIT ?
    """
    linhas = [dict(l) for l in con.execute(sql, (limite,)).fetchall()]
    for l in linhas:
        l["assuntos"] = [a.get("nome", "") for a in json.loads(l.pop("assuntos_json") or "[]")]
        for chave in ("estrutural_motivos", "semantica_motivos"):
            if l.get(chave):
                l[chave] = json.loads(l[chave])
    if filtro_estado:
        campo, _, valor = filtro_estado.partition(":")
        linhas = [l for l in linhas if (l.get(campo) or "") == valor]
    return linhas


class Manipulador(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silencia o log padrão por requisição
        pass

    def _json(self, dados, codigo=200):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo(self) -> dict:
        tamanho = int(self.headers.get("Content-Length") or 0)
        if not tamanho:
            return {}
        return json.loads(self.rfile.read(tamanho).decode("utf-8"))

    def _params(self) -> dict:
        if "?" not in self.path:
            return {}
        from urllib.parse import parse_qs, urlparse
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def do_GET(self):
        rota = self.path.split("?")[0]
        try:
            if rota in ("/", "/index.html"):
                conteudo = (DIR_STATIC / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(conteudo)))
                self.end_headers()
                self.wfile.write(conteudo)
                return
            with db.sessao() as con:
                if rota == "/api/status":
                    return self._json(_status(con))
                if rota == "/api/processos":
                    return self._json(_listar_processos(con, self._params()))
                if rota == "/api/casos":
                    return self._json(pipeline.casos_deduplicados(con))
                if rota == "/api/diagnostico":
                    return self._json(pipeline.diagnostico_base(con))
                if rota == "/api/metricas":
                    return self._json(metricas.calcular(con))
                if rota == "/api/auditoria":
                    limite = int(self._params().get("limite", 100))
                    eventos = auditoria.listar(con, limite)
                    for e in eventos:
                        e["detalhes"] = json.loads(e.pop("detalhes_json"))
                    return self._json(eventos)
            self._json({"erro": f"rota desconhecida: {rota}"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"erro": str(e)}, 500)

    def do_POST(self):
        rota = self.path.split("?")[0]
        try:
            corpo = self._corpo()
            with db.sessao() as con:
                if rota == "/api/coleta":
                    resultado = pipeline.executar_coleta(
                        con,
                        tribunal=corpo.get("tribunal", "TST"),
                        max_paginas=int(corpo.get("max_paginas", 3)),
                        tamanho_pagina=int(corpo.get("tamanho_pagina", 100)),
                        pesquisador=corpo.get("pesquisador", ""),
                        incremental=bool(corpo.get("incremental", False)),
                    )
                    return self._json(resultado)
                if rota == "/api/filtro/reaplicar":
                    return self._json(pipeline.reaplicar_filtro_estrutural(con))
                if rota == "/api/classificacao":
                    return self._json(pipeline.executar_classificacao_semantica(
                        con,
                        usar_metadados=bool(corpo.get("usar_metadados", False)),
                        usar_llm=bool(corpo.get("usar_llm", False)),
                        limite=int(corpo.get("limite", 500)),
                    ))
                if rota == "/api/extracao":
                    return self._json(extracao.solicitar_pendentes(
                        con, limite=int(corpo.get("limite", 10))))
                if rota == "/api/extracao/respostas":
                    return self._json(extracao.receber_respostas(con))
                if rota == "/api/texto-manual":
                    doc_id = extracao.registrar_texto_manual(
                        con, int(corpo["processo_id"]), corpo["texto"],
                        corpo.get("origem", "não informada"))
                    return self._json({"documento_id": doc_id})
                if rota == "/api/revisao":
                    rotulo = corpo.get("rotulo")
                    if rotulo not in ("relevante", "irrelevante"):
                        return self._json({"erro": "rotulo deve ser 'relevante' ou 'irrelevante'"}, 400)
                    con.execute(
                        """INSERT INTO revisoes (processo_id, rotulo, revisor, observacao, registrada_em)
                           VALUES (?, ?, ?, ?, ?) ON CONFLICT(processo_id) DO UPDATE SET
                           rotulo = excluded.rotulo, revisor = excluded.revisor,
                           observacao = excluded.observacao, registrada_em = excluded.registrada_em""",
                        (int(corpo["processo_id"]), rotulo, corpo.get("revisor", ""),
                         corpo.get("observacao", ""), auditoria.agora()))
                    auditoria.registrar(con, "revisao_humana", {
                        "processo_id": int(corpo["processo_id"]), "rotulo": rotulo,
                        "revisor": corpo.get("revisor", "")})
                    return self._json({"ok": True})
                if rota == "/api/controle":
                    numeros = corpo.get("numeros", [])
                    inseridos = 0
                    for numero in numeros:
                        norm = dedup.normalizar_numero(numero)
                        if not norm:
                            continue
                        existe = con.execute(
                            "SELECT 1 FROM amostra_controle WHERE numero_cnj = ?",
                            (norm,)).fetchone()
                        if existe:
                            continue
                        con.execute(
                            """INSERT INTO amostra_controle
                               (numero_cnj, origem, registrada_em) VALUES (?, ?, ?)""",
                            (norm, corpo.get("origem", ""), auditoria.agora()))
                        inseridos += 1
                    auditoria.registrar(con, "amostra_controle_atualizada", {
                        "informados": len(numeros), "inseridos": inseridos,
                        "origem": corpo.get("origem", "")})
                    total = con.execute(
                        "SELECT COUNT(*) AS n FROM amostra_controle").fetchone()["n"]
                    return self._json({"inseridos": inseridos, "total_amostra": total})
                if rota == "/api/exportacao":
                    caminho = exportador.exportar(con, corpo.get("formato", "csv"))
                    return self._json({"arquivo": caminho})
                if rota == "/api/caderno":
                    caminho = caderno.gerar(con)
                    return self._json({"arquivo": caminho,
                                       "conteudo": Path(caminho).read_text(encoding="utf-8")})
            self._json({"erro": f"rota desconhecida: {rota}"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"erro": str(e)}, 500)


def iniciar(porta: int = 8000):
    config.garantir_diretorios()
    servidor = ThreadingHTTPServer(("0.0.0.0", porta), Manipulador)
    print(f"Agente de coleta jurisprudencial — http://localhost:{porta}")
    print(f"Pipeline v{config.VERSAO_PIPELINE} | config {config.hash_configuracao()}")
    print(f"Judit: {'credencial configurada' if judit.credencial_configurada() else 'SEM credencial (solicitações ficarão pendentes)'}")
    servidor.serve_forever()
