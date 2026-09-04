from __future__ import annotations

import argparse
import sys
from pathlib import Path

import robo_importacao_nfe
from baixar_sibrax import ErroSibrax, baixar_notas_sibrax


def criar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Baixa o lote do Sibrax e prepara os ZIPs para o Domínio."
    )
    parser.add_argument(
        "--competencia",
        help="Competência no formato MM.AAAA, por exemplo 07.2026.",
    )
    parser.add_argument(
        "--simular",
        action="store_true",
        help="Baixa o lote e simula a preparação, sem gravar ZIPs no destino.",
    )
    parser.add_argument(
        "--somente-baixar",
        action="store_true",
        help="Faz apenas o download do Sibrax.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argumentos = criar_parser().parse_args(argv)
    competencia = argumentos.competencia
    if not competencia:
        competencia = input(
            "Informe a competência (MM.AAAA, exemplo 07.2026): "
        ).strip()

    pasta_script = Path(__file__).resolve().parent
    try:
        resultado = baixar_notas_sibrax(competencia, pasta_script)
    except ErroSibrax as erro:
        print(f"\nERRO NO SIBRAX: {erro}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nExecução cancelada pelo usuário.", file=sys.stderr)
        return 130

    print("\nDownload concluído.")
    print(f"Empresas selecionadas: {resultado.empresas_selecionadas}")
    print(f"Pasta mensal: {resultado.pasta_mensal}")
    if argumentos.somente_baixar:
        return 0

    argumentos_processamento = [
        "--competencia",
        competencia,
        "--origem",
        str(resultado.pasta_mensal),
    ]
    if argumentos.simular:
        argumentos_processamento.append("--simular")
    return robo_importacao_nfe.main(argumentos_processamento)


if __name__ == "__main__":
    sys.exit(main())

