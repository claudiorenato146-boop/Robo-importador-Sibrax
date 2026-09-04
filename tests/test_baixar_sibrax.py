from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PASTA_PROJETO = Path(__file__).resolve().parents[1]
if str(PASTA_PROJETO) not in sys.path:
    sys.path.insert(0, str(PASTA_PROJETO))

from baixar_sibrax import (  # noqa: E402
    ErroSibrax,
    _arquivo_sibrax_da_competencia,
    _separar_empresa_e_cnpj,
    carregar_configuracao_sibrax,
    carregar_env,
    extrair_zip_seguro,
    preparar_pasta_mensal,
)


class TesteConfiguracaoSibrax(unittest.TestCase):
    def test_reconhece_nome_real_do_zip_sibrax(self) -> None:
        caminho = Path("EMPRESA_[07-2026]_[SIBRAX].zip")
        self.assertTrue(_arquivo_sibrax_da_competencia(caminho, "07.2026"))
        self.assertFalse(_arquivo_sibrax_da_competencia(caminho, "06.2026"))
        self.assertFalse(
            _arquivo_sibrax_da_competencia(Path("outro_07-2026.zip"), "07.2026")
        )

    def test_separa_nome_e_cnpj_da_configuracao(self) -> None:
        nome, cnpj = _separar_empresa_e_cnpj(
            "EMPRESA DE TESTE LTDA / 12.345.678/0001-99"
        )
        self.assertEqual(nome, "EMPRESADETESTELTDA")
        self.assertEqual(cnpj, "12345678000199")

    def test_carrega_env_com_aspas_e_comentarios(self) -> None:
        with tempfile.TemporaryDirectory() as temporaria:
            caminho = Path(temporaria) / ".env"
            caminho.write_text(
                "\n".join(
                    (
                        "# comentário",
                        "SIBRAX_USUARIO=usuario@exemplo.com",
                        'SIBRAX_SENHA="senha com espaço"',
                        "SIBRAX_EMPRESA='00.000.000/0001-00'",
                    )
                ),
                encoding="utf-8",
            )
            dados = carregar_env(caminho)
            self.assertEqual(dados["SIBRAX_SENHA"], "senha com espaço")
            self.assertEqual(dados["SIBRAX_EMPRESA"], "00.000.000/0001-00")

    def test_bloqueia_credencial_ausente(self) -> None:
        with tempfile.TemporaryDirectory() as temporaria:
            caminho = Path(temporaria) / ".env"
            caminho.write_text(
                "SIBRAX_USUARIO=\nSIBRAX_SENHA=\nSIBRAX_EMPRESA=\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ErroSibrax, "SIBRAX_USUARIO"):
                carregar_configuracao_sibrax(caminho)


class TesteExtracaoSibrax(unittest.TestCase):
    def test_bloqueia_zip_com_travessia_de_pasta(self) -> None:
        with tempfile.TemporaryDirectory() as temporaria:
            raiz = Path(temporaria)
            pacote = raiz / "malicioso.zip"
            with zipfile.ZipFile(pacote, "w") as zip_saida:
                zip_saida.writestr("../fora.xml", "<xml />")

            with self.assertRaisesRegex(ErroSibrax, "caminho inseguro"):
                extrair_zip_seguro(pacote, raiz / "extraido")
            self.assertFalse((raiz / "fora.xml").exists())

    def test_prepara_pasta_mensal_sem_subpasta_externa(self) -> None:
        with tempfile.TemporaryDirectory() as temporaria:
            raiz = Path(temporaria)
            pacote = raiz / "notas.zip"
            nome_cliente = "12345678000199-EMPRESA_TESTE"
            with zipfile.ZipFile(pacote, "w") as zip_saida:
                zip_saida.writestr(
                    f"NOTAS - 07.2026/{nome_cliente}/NFe/Entradas/nota.xml",
                    "<nfe />",
                )

            destino = preparar_pasta_mensal(
                pacote,
                "07.2026",
                raiz / "Downloads",
                raiz / "trabalho",
            )
            esperado = destino / nome_cliente / "NFe" / "Entradas" / "nota.xml"
            self.assertTrue(esperado.is_file())
            self.assertFalse((destino / "NOTAS - 07.2026").exists())

    def test_conflito_nao_copia_nenhum_arquivo_novo(self) -> None:
        with tempfile.TemporaryDirectory() as temporaria:
            raiz = Path(temporaria)
            downloads = raiz / "Downloads"
            destino = (
                downloads
                / "NOTAS - 07.2026"
                / "12345678000199-EMPRESA_TESTE"
                / "NFe"
                / "Entradas"
            )
            destino.mkdir(parents=True)
            (destino / "conflito.xml").write_text("antigo", encoding="utf-8")

            pacote = raiz / "notas.zip"
            with zipfile.ZipFile(pacote, "w") as zip_saida:
                base = "12345678000199-EMPRESA_TESTE/NFe/Entradas"
                zip_saida.writestr(f"{base}/conflito.xml", "novo")
                zip_saida.writestr(f"{base}/novo.xml", "novo")

            with self.assertRaisesRegex(ErroSibrax, "Nada foi sobrescrito"):
                preparar_pasta_mensal(
                    pacote,
                    "07.2026",
                    downloads,
                    raiz / "trabalho",
                )
            self.assertEqual(
                (destino / "conflito.xml").read_text(encoding="utf-8"), "antigo"
            )
            self.assertFalse((destino / "novo.xml").exists())


if __name__ == "__main__":
    unittest.main()
