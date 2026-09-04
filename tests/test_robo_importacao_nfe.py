from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import robo_importacao_nfe as robo  # noqa: E402


XML_A = b'<?xml version="1.0" encoding="utf-8"?><nfeProc><NFe id="1"/></nfeProc>'
XML_B = b'<?xml version="1.0" encoding="utf-8"?><nfeProc><NFe id="2"/></nfeProc>'


def escrever_xml(caminho: Path, conteudo: bytes = XML_A) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(conteudo)
    return caminho


class RoboImportacaoNFeTests(unittest.TestCase):
    def test_cadastro_de_exemplo_tem_codigos_e_cnpjs_unicos_e_validos(self) -> None:
        """Invariantes do cadastro, verificadas sobre o CSV de exemplo.

        Antes este teste lia o `clientes_nfe.csv` de producao e conferia as 27
        empresas reais uma a uma. Isso amarrava a suite a um arquivo que nao
        pode ser versionado, entao a checagem passou a ser estrutural: codigo
        unico, CNPJ unico e digito verificador valido. Vale para qualquer
        cadastro, inclusive o de verdade.
        """
        clientes = robo.carregar_clientes(PROJECT / "clientes_nfe.exemplo.csv")
        self.assertTrue(clientes, "o cadastro de exemplo nao pode estar vazio")
        self.assertEqual(len(clientes), len({cliente.codigo for cliente in clientes}))
        self.assertEqual(len(clientes), len({cliente.cnpj for cliente in clientes}))
        self.assertTrue(all(robo.cnpj_valido(cliente.cnpj) for cliente in clientes))
        self.assertTrue(all(cliente.razao_social for cliente in clientes))

    def test_competencia_e_pasta_mensal(self) -> None:
        self.assertEqual(("07.2026", "072026"), robo.interpretar_competencia("07.2026"))
        with self.assertRaises(robo.ErroConfiguracao):
            robo.interpretar_competencia("13.2026")
        with tempfile.TemporaryDirectory() as temporario:
            base = Path(temporario)
            mensal = base / "NOTAS - 07.2026"
            mensal.mkdir()
            self.assertEqual(mensal, robo.localizar_pasta_mensal(base, "07.2026"))
            robo.validar_pasta_mensal_explicita(mensal, "07.2026")
            with self.assertRaises(robo.ErroConfiguracao):
                robo.validar_pasta_mensal_explicita(mensal, "08.2026")

    def test_plano_nao_cria_destino_e_ignora_cte_e_resumo(self) -> None:
        cliente = robo.Cliente("124", "99900002000102", "MODELO SERVICOS ADMINISTRATIVOS LTDA")
        with tempfile.TemporaryDirectory() as temporario:
            raiz = Path(temporario)
            origem = raiz / "NOTAS - 07.2026"
            destino = raiz / "Importacao NFe"
            pasta_empresa = origem / "99900002000102-MODELO_SERVICOS_ADMINISTRATIVOS_LTDA"
            escrever_xml(pasta_empresa / "NFe" / "Entradas" / "entrada.xml")
            (pasta_empresa / "NFe" / "Saidas").mkdir()
            escrever_xml(pasta_empresa / "CTe" / "Recebidos" / "cte.xml")
            (pasta_empresa / "resumo.txt").write_text("resumo", encoding="utf-8")
            (destino / "124-LOJA DO MAURINHO").mkdir(parents=True)

            plano = robo.planejar_execucao(
                (cliente,), origem, destino, "072026"
            )

            self.assertEqual(1, len(plano.acoes))
            acao = plano.acoes[0]
            self.assertEqual("Entradas", acao.categoria)
            self.assertEqual(
                "NFe_Entradas_99900002000102.zip", acao.arquivo_zip.name
            )
            self.assertFalse(acao.pasta_competencia.exists())
            self.assertEqual(("entrada.xml",), tuple(xml.nome for xml in acao.xmls))
            status = {registro.status for registro in plano.registros}
            self.assertIn("SEM_XML", status)
            self.assertIn("CATEGORIA_AUSENTE", status)

    def test_zip_tem_somente_xmls_na_raiz_e_reexecucao_e_idempotente(self) -> None:
        cliente = robo.Cliente("124", "99900002000102", "MODELO SERVICOS ADMINISTRATIVOS LTDA")
        with tempfile.TemporaryDirectory() as temporario:
            raiz = Path(temporario)
            origem = raiz / "NOTAS - 07.2026"
            destino = raiz / "Importacao NFe"
            categoria = (
                origem
                / "99900002000102-MODELO_SERVICOS_ADMINISTRATIVOS_LTDA"
                / "NFe"
                / "Entradas"
            )
            escrever_xml(categoria / "a.xml", XML_A)
            escrever_xml(categoria / "b.XML", XML_B)
            (destino / "124-LOJA DO MAURINHO").mkdir(parents=True)
            plano = robo.planejar_execucao(
                (cliente,), origem, destino, "072026"
            )
            acao = plano.acoes[0]

            primeiro = robo.executar_acao(acao)
            self.assertEqual("CRIADO", primeiro.status)
            self.assertTrue(acao.arquivo_zip.is_file())
            with zipfile.ZipFile(acao.arquivo_zip) as arquivo:
                self.assertEqual(["a.xml", "b.XML"], arquivo.namelist())
                self.assertTrue(all("/" not in nome for nome in arquivo.namelist()))

            segundo = robo.executar_acao(acao)
            self.assertEqual("JA_EXISTENTE_IDENTICO", segundo.status)

    def test_zip_existente_diferente_nunca_e_sobrescrito(self) -> None:
        cliente = robo.Cliente("124", "99900002000102", "MODELO SERVICOS ADMINISTRATIVOS LTDA")
        with tempfile.TemporaryDirectory() as temporario:
            raiz = Path(temporario)
            origem = raiz / "NOTAS - 07.2026"
            destino = raiz / "Importacao NFe"
            categoria = (
                origem
                / "99900002000102-MODELO_SERVICOS_ADMINISTRATIVOS_LTDA"
                / "NFe"
                / "Entradas"
            )
            escrever_xml(categoria / "a.xml", XML_A)
            (destino / "124-LOJA DO MAURINHO").mkdir(parents=True)
            acao = robo.planejar_execucao(
                (cliente,), origem, destino, "072026"
            ).acoes[0]
            acao.pasta_competencia.mkdir()
            with zipfile.ZipFile(
                acao.arquivo_zip, "w", compression=zipfile.ZIP_DEFLATED
            ) as arquivo:
                arquivo.writestr("a.xml", XML_B)
            hash_antes = robo.hash_arquivo(acao.arquivo_zip)

            resultado = robo.executar_acao(acao)

            self.assertEqual("CONFLITO_ZIP_EXISTENTE_DIFERENTE", resultado.status)
            self.assertEqual(hash_antes, robo.hash_arquivo(acao.arquivo_zip))

    def test_origem_alterada_depois_do_plano_e_bloqueada(self) -> None:
        cliente = robo.Cliente("124", "99900002000102", "MODELO SERVICOS ADMINISTRATIVOS LTDA")
        with tempfile.TemporaryDirectory() as temporario:
            raiz = Path(temporario)
            origem = raiz / "NOTAS - 07.2026"
            destino = raiz / "Importacao NFe"
            xml = escrever_xml(
                origem
                / "99900002000102-MODELO_SERVICOS_ADMINISTRATIVOS_LTDA"
                / "NFe"
                / "Entradas"
                / "a.xml",
                XML_A,
            )
            (destino / "124-LOJA DO MAURINHO").mkdir(parents=True)
            acao = robo.planejar_execucao(
                (cliente,), origem, destino, "072026"
            ).acoes[0]
            xml.write_bytes(XML_B)

            resultado = robo.executar_acao(acao)

            self.assertEqual("ERRO_ORIGEM_ALTERADA", resultado.status)
            self.assertFalse(acao.arquivo_zip.exists())

    def test_xml_invalido_ou_subpasta_impede_zip(self) -> None:
        cliente = robo.Cliente("124", "99900002000102", "MODELO SERVICOS ADMINISTRATIVOS LTDA")
        for criar_conteudo in ("invalido", "subpasta"):
            with self.subTest(caso=criar_conteudo), tempfile.TemporaryDirectory() as tmp:
                raiz = Path(tmp)
                origem = raiz / "NOTAS - 07.2026"
                destino = raiz / "Importacao NFe"
                categoria = (
                    origem
                    / "99900002000102-MODELO_SERVICOS_ADMINISTRATIVOS_LTDA"
                    / "NFe"
                    / "Entradas"
                )
                if criar_conteudo == "invalido":
                    escrever_xml(categoria / "quebrado.xml", b"<xml>")
                else:
                    escrever_xml(categoria / "interna" / "a.xml")
                (destino / "124-LOJA DO MAURINHO").mkdir(parents=True)

                plano = robo.planejar_execucao(
                    (cliente,), origem, destino, "072026"
                )

                self.assertFalse(plano.acoes)
                self.assertIn(
                    "ERRO_XML_ORIGEM",
                    {registro.status for registro in plano.registros},
                )

    def test_cliente_sem_destino_e_empresa_nao_cadastrada_sao_ignorados(self) -> None:
        cliente = robo.Cliente("124", "99900002000102", "MODELO SERVICOS ADMINISTRATIVOS LTDA")
        with tempfile.TemporaryDirectory() as temporario:
            raiz = Path(temporario)
            origem = raiz / "NOTAS - 07.2026"
            destino = raiz / "Importacao NFe"
            escrever_xml(
                origem
                / "99900002000102-MODELO_SERVICOS_ADMINISTRATIVOS_LTDA"
                / "NFe"
                / "Entradas"
                / "a.xml"
            )
            escrever_xml(
                origem
                / "99900005000138-TRANSPORTADORA_AMOSTRA_LTDA"
                / "NFe"
                / "Entradas"
                / "b.xml"
            )
            destino.mkdir()

            plano = robo.planejar_execucao(
                (cliente,), origem, destino, "072026"
            )

            self.assertFalse(plano.acoes)
            status = {registro.status for registro in plano.registros}
            self.assertIn("IGNORADO_SEM_PASTA_DESTINO", status)
            self.assertIn("IGNORADO_CLIENTE_NAO_CADASTRADO", status)

    def test_duplicidade_de_origem_e_destino_nunca_e_escolhida(self) -> None:
        cliente = robo.Cliente("124", "99900002000102", "MODELO SERVICOS ADMINISTRATIVOS LTDA")
        with tempfile.TemporaryDirectory() as temporario:
            raiz = Path(temporario)
            origem = raiz / "NOTAS - 07.2026"
            destino = raiz / "Importacao NFe"
            for nome in (
                "99900002000102-MODELO_SERVICOS_ADMINISTRATIVOS_LTDA",
                "99900002000102-LOJA_DUPLICADA",
            ):
                escrever_xml(origem / nome / "NFe" / "Entradas" / "a.xml")
            (destino / "124-CLIENTE A").mkdir(parents=True)
            (destino / "124-CLIENTE B").mkdir()

            plano = robo.planejar_execucao(
                (cliente,), origem, destino, "072026"
            )

            self.assertFalse(plano.acoes)
            status = {registro.status for registro in plano.registros}
            self.assertIn("ERRO_CNPJ_ORIGEM_DUPLICADO", status)
            self.assertIn("ERRO_CODIGO_DESTINO_DUPLICADO", status)
            self.assertIn("ERRO_ORIGEM_AMBIGUA", status)

    def test_lock_de_outra_execucao_nunca_e_removido(self) -> None:
        with tempfile.TemporaryDirectory() as temporario:
            destino = Path(temporario)
            lock = destino / ".robo_importacao_nfe.lock"
            lock.write_text("outra execução", encoding="utf-8")

            with self.assertRaises(robo.ErroConfiguracao):
                with robo.lock_destino(destino):
                    self.fail("Não deveria adquirir o lock")

            self.assertTrue(lock.exists())
            self.assertEqual("outra execução", lock.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
