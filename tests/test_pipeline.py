"""Testes de ponta a ponta do pipeline sobre dados SINTÉTICOS.

Os dados de tests/fixtures/decisoes_sinteticas.json são fictícios, criados só
para exercitar as camadas (ingestão, pré-filtro, dedup, semântica, Etapa 2 sem
credencial, revisão, métricas, exportação, auditoria, caderno). Executar com:

    python3 -m unittest discover -s tests -v
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="agente_teste_")
os.environ["AGENTE_DIR_DADOS"] = os.path.join(_TMP, "data")
os.environ["AGENTE_DIR_EXPORTS"] = os.path.join(_TMP, "exports")
os.environ["AGENTE_BANCO"] = os.path.join(_TMP, "data", "teste.sqlite3")

from agente import (caderno, classificador, config, datajud, db, dedup,  # noqa: E402
                    exportador, extracao, filtros, metricas, pipeline)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "decisoes_sinteticas.json").read_text(
        encoding="utf-8"
    )
)


def _normalizados():
    registros = []
    for p in FIXTURES["processos"]:
        registros.append({
            "numero": p["numero"],
            "tribunal": p["tribunal"],
            "grau": p["grau"],
            "classe_codigo": p["classe_codigo"],
            "classe_nome": p["classe_nome"],
            "assuntos": p["assuntos"],
            "orgao_julgador": p["orgao_julgador"],
            "data_ajuizamento": p["data_ajuizamento"],
            "timestamp_fonte": p["timestamp_fonte"],
        })
    return registros


class TestDedup(unittest.TestCase):
    def test_normalizacao_e_formatacao(self):
        self.assertEqual(
            dedup.normalizar_numero("0001111-22.2023.5.04.0001"),
            "00011112220235040001",
        )
        self.assertEqual(
            dedup.formatar_numero("00011112220235040001"),
            "0001111-22.2023.5.04.0001",
        )

    def test_canonico_prefere_grau_mais_alto(self):
        aparicoes = [{"grau": "G1"}, {"grau": "SUP"}, {"grau": "G2"}]
        self.assertEqual(dedup.escolher_canonico(aparicoes)["grau"], "SUP")


class TestFiltroEstrutural(unittest.TestCase):
    def test_aprova_rr_com_assunto_de_vinculo(self):
        resultado, motivos = filtros.avaliar(_normalizados()[0])
        self.assertEqual(resultado, "incluido", motivos)

    def test_barra_classe_e_assunto_fora_dos_criterios(self):
        alvo = next(r for r in _normalizados() if r["classe_nome"] == "Embargos de Declaração")
        resultado, motivos = filtros.avaliar(alvo)
        self.assertEqual(resultado, "excluido")
        self.assertTrue(any("classe" in m for m in motivos))


class TestClassificadorSemantico(unittest.TestCase):
    def test_rotulos_esperados_dos_textos_sinteticos(self):
        for p in FIXTURES["processos"]:
            if "texto_sintetico" not in p or "rotulo_esperado_semantica" not in p:
                continue
            veredicto = classificador.classificar_texto(p["texto_sintetico"])
            self.assertEqual(
                veredicto["resultado"], p["rotulo_esperado_semantica"],
                f"{p['numero']}: score={veredicto['score']} motivos={veredicto['motivos']}",
            )

    def test_atestado_medico_nao_conta_como_profissao(self):
        veredicto = classificador.classificar_texto(
            "O empregado apresentou atestado médico e foi submetido a perícia médica."
        )
        self.assertEqual(veredicto["resultado"], "excluido")
        self.assertTrue(
            any(m.get("regra") == "criterio_necessario" for m in veredicto["motivos"])
        )

    def test_metadados_nunca_geram_inclusao_definitiva(self):
        texto = ("Recurso de Revista — pejotização — médico — subordinação — "
                 "primazia da realidade — art. 442-B — vínculo empregatício")
        veredicto = classificador.classificar_texto(texto, base="metadados")
        self.assertEqual(veredicto["resultado"], "revisar")

    def test_motivos_carregam_trechos_do_texto(self):
        veredicto = classificador.classificar_texto(
            FIXTURES["processos"][0]["texto_sintetico"]
        )
        trechos = [m for m in veredicto["motivos"] if m.get("trecho")]
        self.assertTrue(trechos, "cada regra acionada deve registrar o trecho")


class TestConsultaDataJud(unittest.TestCase):
    def test_corpo_da_consulta(self):
        regras = filtros.carregar_regras()
        consulta = datajud.construir_consulta(regras, tamanho=50)
        self.assertEqual(consulta["size"], 50)
        self.assertIn("query", consulta)
        clausulas = consulta["query"]["bool"]["must"]
        self.assertTrue(any("range" in c and "dataAjuizamento" in c["range"]
                            for c in clausulas), "janela temporal ausente")
        clausula_assuntos = next(c for c in clausulas if "bool" in c)
        for should in clausula_assuntos["bool"]["should"]:
            self.assertIn("match_phrase", should,
                          "assuntos devem usar frase exata (match_phrase), "
                          "não match por palavras soltas")
        incremental = datajud.construir_consulta(
            regras, apos_timestamp="2024-01-01T00:00:00Z")
        self.assertTrue(any("range" in c and "@timestamp" in c.get("range", {})
                            for c in incremental["query"]["bool"]["must"]))

    def test_tribunais_mapeados(self):
        self.assertEqual(config.TRIBUNAIS["TST"], "api_publica_tst")
        self.assertEqual(config.TRIBUNAIS["TRT24"], "api_publica_trt24")
        self.assertEqual(len(config.TRIBUNAIS), 25)


class TestPipelinePontaAPonta(unittest.TestCase):
    """Fluxo completo sobre o banco: ingestão → filtros → Etapa 2 → métricas."""

    @classmethod
    def setUpClass(cls):
        cls.con = db.conectar()
        pipeline.processar_dados_normalizados(
            cls.con, _normalizados(),
            {"tribunal": "TST", "tipo": "teste_sintetico",
             "pesquisador": "suite de testes",
             "aviso": FIXTURES["AVISO"]},
        )

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_01_ingestao_e_dedup(self):
        n = self.con.execute("SELECT COUNT(*) AS n FROM processos").fetchone()["n"]
        self.assertEqual(n, len(FIXTURES["processos"]))
        casos = pipeline.casos_deduplicados(self.con)
        self.assertEqual(len(casos), len(FIXTURES["processos"]) - 1,
                         "os dois registros de mesmo número devem virar um caso")
        duplicado = next(c for c in casos if len(c["aparicoes"]) == 2)
        self.assertEqual(duplicado["grau_canonico"], "SUP")

    def test_02_prefiltro_estrutural_registrado(self):
        excluidos = self.con.execute(
            """SELECT COUNT(*) AS n FROM classificacoes
               WHERE camada = 'estrutural' AND resultado = 'excluido'"""
        ).fetchone()["n"]
        self.assertGreaterEqual(excluidos, 1)

    def test_03_etapa2_sem_credencial_nao_fabrica_dados(self):
        resumo = extracao.solicitar_pendentes(self.con, limite=10)
        self.assertFalse(resumo["credencial_configurada"])
        self.assertEqual(resumo["solicitados"], 0)
        self.assertGreaterEqual(resumo["pendentes_credencial"], 1)
        recebidos = self.con.execute(
            "SELECT COUNT(*) AS n FROM documentos WHERE status = 'recebido'"
        ).fetchone()["n"]
        self.assertEqual(recebidos, 0, "sem credencial nenhum documento pode existir")

    def test_04_classificacao_exige_texto(self):
        resumo = pipeline.executar_classificacao_semantica(self.con)
        self.assertEqual(resumo["classificados"], 0)
        self.assertGreater(resumo["aguardando_texto"], 0)

    def test_05_texto_manual_e_classificacao(self):
        for p in FIXTURES["processos"]:
            if "texto_sintetico" not in p:
                continue
            linha = self.con.execute(
                "SELECT id FROM processos WHERE numero_cnj = ? AND grau = ?",
                (dedup.normalizar_numero(p["numero"]), p["grau"]),
            ).fetchone()
            extracao.registrar_texto_manual(
                self.con, linha["id"], p["texto_sintetico"],
                "fixture sintética da suíte de testes",
            )
        resumo = pipeline.executar_classificacao_semantica(self.con)
        self.assertGreaterEqual(resumo["classificados"], 3)
        incluidos = self.con.execute(
            """SELECT p.numero_cnj FROM processos p JOIN classificacoes c
               ON c.processo_id = p.id
               WHERE c.camada = 'semantica' AND c.resultado = 'incluido'"""
        ).fetchall()
        numeros = {l["numero_cnj"] for l in incluidos}
        self.assertIn(dedup.normalizar_numero("0001111-22.2023.5.04.0001"), numeros)
        self.assertNotIn(dedup.normalizar_numero("0002222-33.2022.5.02.0002"), numeros)

    def test_06_revisao_metricas_e_gonogo(self):
        incluidos = self.con.execute(
            """SELECT DISTINCT processo_id FROM classificacoes
               WHERE camada = 'semantica' AND resultado = 'incluido'"""
        ).fetchall()
        from agente import auditoria as aud
        for linha in incluidos:
            self.con.execute(
                """INSERT OR REPLACE INTO revisoes
                   (processo_id, rotulo, revisor, registrada_em)
                   VALUES (?, 'relevante', 'suite de testes', ?)""",
                (linha["processo_id"], aud.agora()),
            )
        self.con.execute(
            """INSERT OR IGNORE INTO amostra_controle (numero_cnj, origem, registrada_em)
               VALUES (?, 'fixture sintética', ?)""",
            (dedup.normalizar_numero("0001111-22.2023.5.04.0001"), aud.agora()),
        )
        self.con.commit()
        m = metricas.calcular(self.con)
        self.assertEqual(m["precisao"]["valor"], 1.0)
        self.assertEqual(m["cobertura"]["filtro"], 1.0)
        self.assertFalse(m["go_no_go_fase3"]["amostra_suficiente"],
                         "amostra pequena deve disparar o aviso do PRD §5")
        self.assertIsNotNone(m["go_no_go_fase3"]["aviso"])

    def test_07_exportacao(self):
        caminho_csv = exportador.exportar(self.con, "csv")
        caminho_jsonl = exportador.exportar(self.con, "jsonl")
        conteudo = Path(caminho_csv).read_text(encoding="utf-8")
        self.assertIn("0001111-22.2023.5.04.0001", conteudo)
        self.assertIn("numero_cnj_formatado", conteudo)
        self.assertTrue(Path(caminho_jsonl).exists())

    def test_08_auditoria_completa(self):
        eventos = {l["evento"] for l in self.con.execute(
            "SELECT DISTINCT evento FROM auditoria").fetchall()}
        for esperado in ("decisao_filtro", "judit_pendente_credencial",
                         "texto_manual_registrado", "exportacao"):
            self.assertIn(esperado, eventos)
        self.assertTrue(config.CAMINHO_AUDITORIA.exists())
        primeira_linha = config.CAMINHO_AUDITORIA.read_text(
            encoding="utf-8").splitlines()[0]
        registro = json.loads(primeira_linha)
        self.assertIn("versao_pipeline", registro)
        self.assertIn("hash_config", registro)

    def test_09_diagnostico_e_reaplicacao_do_filtro(self):
        diagnostico = pipeline.diagnostico_base(self.con)
        self.assertEqual(diagnostico["total_processos"], len(FIXTURES["processos"]))
        self.assertTrue(diagnostico["classes_mais_frequentes"])
        self.assertTrue(diagnostico["assuntos_mais_frequentes"])
        vigente_antes = dict(diagnostico["filtro_estrutural_vigente"])
        self.assertGreaterEqual(vigente_antes.get("excluido", 0), 1)

        resumo = pipeline.reaplicar_filtro_estrutural(self.con)
        self.assertEqual(resumo["reavaliados"], len(FIXTURES["processos"]))
        depois = pipeline.diagnostico_base(self.con)["filtro_estrutural_vigente"]
        self.assertEqual(depois, vigente_antes,
                         "sem mudança de configuração, reaplicar não altera o vigente")

    def test_10_caderno_tecnico_gerado_dos_registros(self):
        caminho = caderno.gerar(self.con)
        texto = Path(caminho).read_text(encoding="utf-8")
        self.assertIn("Caderno Técnico", texto)
        self.assertIn("Sessão 1", texto)
        self.assertIn("Limitações identificadas", texto)


if __name__ == "__main__":
    unittest.main()
