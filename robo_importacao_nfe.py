from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
import uuid
import zipfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree


VERSAO = "1.0.0"
NOME_ROBO = "Robô de Importação NFe"
PADRAO_COMPETENCIA = re.compile(r"^(0[1-9]|1[0-2])\.(\d{4})$")
PADRAO_NOME_PASTA_MENSAL = re.compile(
    r"^NOTAS?\s*-\s*(0[1-9]|1[0-2])\.(\d{4})$",
    re.IGNORECASE,
)
PADRAO_PASTA_ORIGEM = re.compile(r"^(\d{14})-(.+)$")
PADRAO_PASTA_DESTINO = re.compile(r"^(\d+)-(.+)$")
CATEGORIAS = ("Entradas", "Saidas", "Canceladas")
STATUS_COM_PROBLEMA = ("ERRO_", "CONFLITO_")

logger = logging.getLogger("robo_importacao_nfe")


class ErroConfiguracao(RuntimeError):
    pass


@dataclass(frozen=True)
class Configuracao:
    pasta_origem_base: Path
    pasta_destino_raiz: Path
    arquivo_clientes: Path
    pasta_relatorios: Path


@dataclass(frozen=True)
class Cliente:
    codigo: str
    cnpj: str
    razao_social: str


@dataclass(frozen=True)
class ArquivoXML:
    caminho: Path
    nome: str
    tamanho: int
    sha256: str

    def identidade(self) -> tuple[str, int, str]:
        return self.nome.casefold(), self.tamanho, self.sha256


@dataclass(frozen=True)
class AcaoCompactacao:
    cliente: Cliente
    categoria: str
    pasta_origem: Path
    pasta_cliente_destino: Path
    pasta_competencia: Path
    arquivo_zip: Path
    xmls: tuple[ArquivoXML, ...]
    observacao: str = ""


@dataclass(frozen=True)
class Registro:
    codigo_cliente: str
    cnpj: str
    empresa: str
    categoria: str
    quantidade_xml: int
    status: str
    detalhe: str
    arquivo_zip: str = ""


@dataclass(frozen=True)
class Plano:
    acoes: tuple[AcaoCompactacao, ...]
    registros: tuple[Registro, ...]


def somente_digitos(valor: str) -> str:
    return "".join(caractere for caractere in valor if caractere.isdigit())


def cnpj_valido(cnpj: str) -> bool:
    cnpj = somente_digitos(cnpj)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False

    def calcular(base: str, pesos: tuple[int, ...]) -> int:
        soma = sum(int(digito) * peso for digito, peso in zip(base, pesos))
        resto = soma % 11
        return 0 if resto < 2 else 11 - resto

    primeiro = calcular(cnpj[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    segundo = calcular(
        cnpj[:12] + str(primeiro),
        (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
    )
    return cnpj[-2:] == f"{primeiro}{segundo}"


def normalizar_nome(valor: str) -> str:
    sem_acentos = "".join(
        caractere
        for caractere in unicodedata.normalize("NFKD", valor)
        if not unicodedata.combining(caractere)
    )
    return "".join(caractere for caractere in sem_acentos.casefold() if caractere.isalnum())


def hash_arquivo(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


def resolver_caminho(valor: str, base: Path) -> Path:
    expandido = os.path.expandvars(os.path.expanduser(valor))
    caminho = Path(expandido)
    if not caminho.is_absolute():
        caminho = base / caminho
    return caminho.resolve(strict=False)


def carregar_configuracao(caminho_config: Path) -> Configuracao:
    if not caminho_config.is_file():
        raise ErroConfiguracao(f"Arquivo de configuração não encontrado: {caminho_config}")

    try:
        dados = json.loads(caminho_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as erro:
        raise ErroConfiguracao(
            f"Não foi possível ler a configuração {caminho_config}: {erro}"
        ) from erro

    campos = {
        "pasta_origem_base",
        "pasta_destino_raiz",
        "arquivo_clientes",
        "pasta_relatorios",
    }
    ausentes = sorted(campo for campo in campos if not str(dados.get(campo, "")).strip())
    if ausentes:
        raise ErroConfiguracao(
            "Campos obrigatórios ausentes na configuração: " + ", ".join(ausentes)
        )

    base = caminho_config.parent.resolve()
    return Configuracao(
        pasta_origem_base=resolver_caminho(dados["pasta_origem_base"], base),
        pasta_destino_raiz=resolver_caminho(dados["pasta_destino_raiz"], base),
        arquivo_clientes=resolver_caminho(dados["arquivo_clientes"], base),
        pasta_relatorios=resolver_caminho(dados["pasta_relatorios"], base),
    )


def interpretar_competencia(valor: str) -> tuple[str, str]:
    valor = valor.strip()
    correspondencia = PADRAO_COMPETENCIA.fullmatch(valor)
    if not correspondencia:
        raise ErroConfiguracao(
            f"Competência inválida: '{valor}'. Use MM.AAAA, por exemplo 07.2026."
        )
    mes, ano = correspondencia.groups()
    return valor, f"{mes}{ano}"


def localizar_pasta_mensal(base: Path, competencia: str) -> Path:
    candidatos = [
        base / f"NOTAS - {competencia}",
        base / f"NOTA - {competencia}",
    ]
    existentes = [caminho for caminho in candidatos if caminho.is_dir()]
    if not existentes:
        esperados = " ou ".join(str(caminho) for caminho in candidatos)
        raise ErroConfiguracao(f"Pasta mensal não encontrada. Esperado: {esperados}")
    if len(existentes) > 1:
        raise ErroConfiguracao(
            "Foram encontradas duas pastas para a mesma competência: "
            + " e ".join(str(caminho) for caminho in existentes)
            + ". Mantenha somente uma."
        )
    return existentes[0]


def validar_pasta_mensal_explicita(pasta: Path, competencia: str) -> None:
    correspondencia = PADRAO_NOME_PASTA_MENSAL.fullmatch(pasta.name)
    if not correspondencia:
        raise ErroConfiguracao(
            "A pasta de origem informada não segue o padrão "
            f"'NOTA(S) - MM.AAAA': {pasta}"
        )
    competencia_da_pasta = f"{correspondencia.group(1)}.{correspondencia.group(2)}"
    if competencia_da_pasta != competencia:
        raise ErroConfiguracao(
            f"A competência informada ({competencia}) não corresponde à pasta "
            f"de origem ({competencia_da_pasta})."
        )


def carregar_clientes(caminho_csv: Path) -> tuple[Cliente, ...]:
    if not caminho_csv.is_file():
        raise ErroConfiguracao(f"Cadastro de clientes não encontrado: {caminho_csv}")

    try:
        with caminho_csv.open("r", encoding="utf-8-sig", newline="") as arquivo:
            leitor = csv.DictReader(arquivo, delimiter=";")
            colunas = {"codigo_cliente", "cnpj", "razao_social"}
            if not colunas.issubset(set(leitor.fieldnames or [])):
                raise ErroConfiguracao(
                    f"Cadastro precisa ter as colunas {sorted(colunas)}. "
                    f"Encontradas: {leitor.fieldnames}"
                )

            clientes: list[Cliente] = []
            problemas: list[str] = []
            for numero_linha, linha in enumerate(leitor, start=2):
                codigo = str(linha.get("codigo_cliente", "")).strip()
                cnpj = somente_digitos(str(linha.get("cnpj", "")))
                razao = str(linha.get("razao_social", "")).strip()
                if not codigo.isdigit():
                    problemas.append(f"linha {numero_linha}: código inválido '{codigo}'")
                if not cnpj_valido(cnpj):
                    problemas.append(f"linha {numero_linha}: CNPJ inválido '{cnpj}'")
                if not razao:
                    problemas.append(f"linha {numero_linha}: razão social vazia")
                clientes.append(Cliente(codigo=codigo, cnpj=cnpj, razao_social=razao))
    except OSError as erro:
        raise ErroConfiguracao(f"Falha lendo o cadastro {caminho_csv}: {erro}") from erro

    if not clientes:
        raise ErroConfiguracao("O cadastro de clientes está vazio.")

    codigos = Counter(cliente.codigo for cliente in clientes)
    cnpjs = Counter(cliente.cnpj for cliente in clientes)
    for codigo, quantidade in codigos.items():
        if quantidade > 1:
            problemas.append(f"código duplicado no cadastro: {codigo}")
    for cnpj, quantidade in cnpjs.items():
        if quantidade > 1:
            problemas.append(f"CNPJ duplicado no cadastro: {cnpj}")

    if problemas:
        raise ErroConfiguracao(
            "Cadastro de clientes inválido:\n- " + "\n- ".join(problemas)
        )
    return tuple(clientes)


def _registro_pasta(
    status: str,
    detalhe: str,
    *,
    codigo: str = "",
    cnpj: str = "",
    empresa: str = "",
    categoria: str = "",
) -> Registro:
    return Registro(
        codigo_cliente=codigo,
        cnpj=cnpj,
        empresa=empresa,
        categoria=categoria,
        quantidade_xml=0,
        status=status,
        detalhe=detalhe,
    )


def descobrir_pastas_origem(
    raiz: Path,
) -> tuple[dict[str, Path], set[str], tuple[Registro, ...]]:
    mapa: dict[str, Path] = {}
    duplicados: set[str] = set()
    registros: list[Registro] = []

    for pasta in sorted(raiz.iterdir(), key=lambda item: item.name.casefold()):
        if not pasta.is_dir():
            continue
        correspondencia = PADRAO_PASTA_ORIGEM.fullmatch(pasta.name)
        if not correspondencia:
            registros.append(
                _registro_pasta(
                    "IGNORADO_PASTA_ORIGEM_FORA_PADRAO",
                    f"Pasta ignorada: {pasta.name}",
                )
            )
            continue

        cnpj = correspondencia.group(1)
        if not cnpj_valido(cnpj):
            registros.append(
                _registro_pasta(
                    "IGNORADO_CNPJ_ORIGEM_INVALIDO",
                    f"Pasta com CNPJ inválido ignorada: {pasta.name}",
                    cnpj=cnpj,
                )
            )
            continue

        if cnpj in mapa or cnpj in duplicados:
            primeira = mapa.pop(cnpj, None)
            duplicados.add(cnpj)
            caminhos = [str(caminho) for caminho in (primeira, pasta) if caminho]
            registros.append(
                _registro_pasta(
                    "ERRO_CNPJ_ORIGEM_DUPLICADO",
                    "Mais de uma pasta para o mesmo CNPJ: " + " | ".join(caminhos),
                    cnpj=cnpj,
                )
            )
            continue
        mapa[cnpj] = pasta

    return mapa, duplicados, tuple(registros)


def descobrir_pastas_destino(
    raiz: Path,
) -> tuple[dict[str, Path], set[str], tuple[Registro, ...]]:
    mapa: dict[str, Path] = {}
    duplicados: set[str] = set()
    registros: list[Registro] = []

    for pasta in sorted(raiz.iterdir(), key=lambda item: item.name.casefold()):
        if not pasta.is_dir():
            continue
        correspondencia = PADRAO_PASTA_DESTINO.fullmatch(pasta.name)
        if not correspondencia:
            registros.append(
                _registro_pasta(
                    "IGNORADO_PASTA_DESTINO_FORA_PADRAO",
                    f"Pasta ignorada no destino: {pasta.name}",
                )
            )
            continue

        codigo = str(int(correspondencia.group(1)))
        if codigo in mapa or codigo in duplicados:
            primeira = mapa.pop(codigo, None)
            duplicados.add(codigo)
            caminhos = [str(caminho) for caminho in (primeira, pasta) if caminho]
            registros.append(
                _registro_pasta(
                    "ERRO_CODIGO_DESTINO_DUPLICADO",
                    "Mais de uma pasta para o mesmo código: " + " | ".join(caminhos),
                    codigo=codigo,
                )
            )
            continue
        mapa[codigo] = pasta

    return mapa, duplicados, tuple(registros)


def localizar_subpasta_unica(pasta: Path, nome_esperado: str) -> tuple[Path | None, str]:
    alvo = normalizar_nome(nome_esperado)
    encontradas = [
        item
        for item in pasta.iterdir()
        if item.is_dir() and normalizar_nome(item.name) == alvo
    ]
    if not encontradas:
        return None, ""
    if len(encontradas) > 1:
        return None, (
            f"Mais de uma pasta equivalente a '{nome_esperado}' em {pasta}: "
            + ", ".join(item.name for item in encontradas)
        )
    return encontradas[0], ""


def inventariar_xmls(pasta: Path) -> tuple[tuple[ArquivoXML, ...], tuple[str, ...], str]:
    xmls: list[ArquivoXML] = []
    ignorados: list[str] = []
    subpastas: list[str] = []
    nomes_vistos: set[str] = set()

    try:
        itens = sorted(pasta.iterdir(), key=lambda item: item.name.casefold())
    except OSError as erro:
        return (), (), f"Não foi possível ler {pasta}: {erro}"

    for item in itens:
        if item.is_dir():
            subpastas.append(item.name)
            continue
        if item.is_symlink():
            return (), (), f"Atalho/link não permitido na pasta de XMLs: {item.name}"
        if not item.is_file() or item.suffix.casefold() != ".xml":
            ignorados.append(item.name)
            continue

        chave_nome = item.name.casefold()
        if chave_nome in nomes_vistos:
            return (), (), f"Nome de XML duplicado ignorando maiúsculas: {item.name}"
        nomes_vistos.add(chave_nome)

        try:
            tamanho = item.stat().st_size
            if tamanho <= 0:
                return (), (), f"XML vazio: {item.name}"
            ElementTree.parse(item)
            xmls.append(
                ArquivoXML(
                    caminho=item,
                    nome=item.name,
                    tamanho=tamanho,
                    sha256=hash_arquivo(item),
                )
            )
        except (OSError, ElementTree.ParseError) as erro:
            return (), (), f"XML inválido ou ilegível '{item.name}': {erro}"

    if subpastas:
        return (
            (),
            tuple(ignorados),
            "Subpastas não são permitidas dentro da categoria: "
            + ", ".join(subpastas),
        )
    return tuple(xmls), tuple(ignorados), ""


def nome_zip(categoria: str, cnpj: str) -> str:
    return f"NFe_{categoria}_{cnpj}.zip"


def planejar_execucao(
    clientes: tuple[Cliente, ...],
    pasta_origem: Path,
    pasta_destino: Path,
    competencia_mmaaaa: str,
) -> Plano:
    origem_por_cnpj, cnpjs_duplicados, registros_origem = descobrir_pastas_origem(
        pasta_origem
    )
    destino_por_codigo, codigos_duplicados, registros_destino = (
        descobrir_pastas_destino(pasta_destino)
    )
    clientes_por_cnpj = {cliente.cnpj: cliente for cliente in clientes}
    registros: list[Registro] = [*registros_origem, *registros_destino]
    acoes: list[AcaoCompactacao] = []

    for cnpj, pasta in origem_por_cnpj.items():
        if cnpj not in clientes_por_cnpj:
            registros.append(
                _registro_pasta(
                    "IGNORADO_CLIENTE_NAO_CADASTRADO",
                    f"Empresa da origem não pertence aos clientes ativos: {pasta.name}",
                    cnpj=cnpj,
                )
            )

    for cliente in sorted(clientes, key=lambda item: int(item.codigo)):
        if cliente.cnpj in cnpjs_duplicados:
            registros.append(
                _registro_pasta(
                    "ERRO_ORIGEM_AMBIGUA",
                    "CNPJ possui mais de uma pasta na origem; cliente não processado.",
                    codigo=cliente.codigo,
                    cnpj=cliente.cnpj,
                    empresa=cliente.razao_social,
                )
            )
            continue
        pasta_cliente_origem = origem_por_cnpj.get(cliente.cnpj)
        if pasta_cliente_origem is None:
            registros.append(
                _registro_pasta(
                    "SEM_MOVIMENTO",
                    "Nenhuma pasta foi gerada para este cliente na competência.",
                    codigo=cliente.codigo,
                    cnpj=cliente.cnpj,
                    empresa=cliente.razao_social,
                )
            )
            continue

        if cliente.codigo in codigos_duplicados:
            registros.append(
                _registro_pasta(
                    "ERRO_DESTINO_AMBIGUO",
                    "Código possui mais de uma pasta no destino; cliente não processado.",
                    codigo=cliente.codigo,
                    cnpj=cliente.cnpj,
                    empresa=cliente.razao_social,
                )
            )
            continue

        pasta_cliente_destino = destino_por_codigo.get(cliente.codigo)
        if pasta_cliente_destino is None:
            registros.append(
                _registro_pasta(
                    "IGNORADO_SEM_PASTA_DESTINO",
                    "Cliente possui arquivos na origem, mas não possui pasta em "
                    "Importação NFe. A pasta não foi criada.",
                    codigo=cliente.codigo,
                    cnpj=cliente.cnpj,
                    empresa=cliente.razao_social,
                )
            )
            continue

        pasta_nfe, erro_nfe = localizar_subpasta_unica(pasta_cliente_origem, "NFe")
        if erro_nfe:
            registros.append(
                _registro_pasta(
                    "ERRO_PASTA_NFE_AMBIGUA",
                    erro_nfe,
                    codigo=cliente.codigo,
                    cnpj=cliente.cnpj,
                    empresa=cliente.razao_social,
                )
            )
            continue
        if pasta_nfe is None:
            registros.append(
                _registro_pasta(
                    "SEM_NFE",
                    "A empresa não possui pasta NFe nesta competência.",
                    codigo=cliente.codigo,
                    cnpj=cliente.cnpj,
                    empresa=cliente.razao_social,
                )
            )
            continue

        for categoria in CATEGORIAS:
            pasta_categoria, erro_categoria = localizar_subpasta_unica(
                pasta_nfe, categoria
            )
            if erro_categoria:
                registros.append(
                    _registro_pasta(
                        "ERRO_CATEGORIA_AMBIGUA",
                        erro_categoria,
                        codigo=cliente.codigo,
                        cnpj=cliente.cnpj,
                        empresa=cliente.razao_social,
                        categoria=categoria,
                    )
                )
                continue
            if pasta_categoria is None:
                registros.append(
                    _registro_pasta(
                        "CATEGORIA_AUSENTE",
                        "Categoria não existe na origem; nenhum ZIP necessário.",
                        codigo=cliente.codigo,
                        cnpj=cliente.cnpj,
                        empresa=cliente.razao_social,
                        categoria=categoria,
                    )
                )
                continue

            xmls, ignorados, erro_xml = inventariar_xmls(pasta_categoria)
            if erro_xml:
                registros.append(
                    _registro_pasta(
                        "ERRO_XML_ORIGEM",
                        erro_xml,
                        codigo=cliente.codigo,
                        cnpj=cliente.cnpj,
                        empresa=cliente.razao_social,
                        categoria=categoria,
                    )
                )
                continue
            if not xmls:
                detalhe = "Pasta sem XML; nenhum ZIP necessário."
                if ignorados:
                    detalhe += " Arquivos não XML ignorados: " + ", ".join(ignorados)
                registros.append(
                    _registro_pasta(
                        "SEM_XML",
                        detalhe,
                        codigo=cliente.codigo,
                        cnpj=cliente.cnpj,
                        empresa=cliente.razao_social,
                        categoria=categoria,
                    )
                )
                continue

            observacao = ""
            if ignorados:
                observacao = "Arquivos não XML ignorados: " + ", ".join(ignorados)
            pasta_competencia = pasta_cliente_destino / competencia_mmaaaa
            destino_zip = pasta_competencia / nome_zip(categoria, cliente.cnpj)
            acoes.append(
                AcaoCompactacao(
                    cliente=cliente,
                    categoria=categoria,
                    pasta_origem=pasta_categoria,
                    pasta_cliente_destino=pasta_cliente_destino,
                    pasta_competencia=pasta_competencia,
                    arquivo_zip=destino_zip,
                    xmls=xmls,
                    observacao=observacao,
                )
            )

    return Plano(acoes=tuple(acoes), registros=tuple(registros))


def _identidades(xmls: tuple[ArquivoXML, ...]) -> tuple[tuple[str, int, str], ...]:
    return tuple(sorted((xml.identidade() for xml in xmls), key=lambda item: item[0]))


def manifesto_zip(caminho_zip: Path) -> tuple[tuple[str, int, str], ...]:
    itens: list[tuple[str, int, str]] = []
    nomes: set[str] = set()
    try:
        with zipfile.ZipFile(caminho_zip, "r") as arquivo:
            if arquivo.testzip() is not None:
                raise ValueError("CRC inválido")
            for info in arquivo.infolist():
                if info.is_dir():
                    raise ValueError(f"ZIP contém pasta: {info.filename}")
                if Path(info.filename).name != info.filename:
                    raise ValueError(f"ZIP contém subpasta: {info.filename}")
                if Path(info.filename).suffix.casefold() != ".xml":
                    raise ValueError(f"ZIP contém arquivo não XML: {info.filename}")
                chave = info.filename.casefold()
                if chave in nomes:
                    raise ValueError(f"ZIP contém nome duplicado: {info.filename}")
                nomes.add(chave)
                conteudo = arquivo.read(info)
                if not conteudo:
                    raise ValueError(f"ZIP contém XML vazio: {info.filename}")
                ElementTree.fromstring(conteudo)
                itens.append(
                    (chave, len(conteudo), hashlib.sha256(conteudo).hexdigest())
                )
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, ValueError) as erro:
        raise ValueError(f"ZIP inválido '{caminho_zip}': {erro}") from erro
    return tuple(sorted(itens, key=lambda item: item[0]))


def executar_acao(acao: AcaoCompactacao) -> Registro:
    atuais, ignorados, erro = inventariar_xmls(acao.pasta_origem)
    if erro:
        return Registro(
            codigo_cliente=acao.cliente.codigo,
            cnpj=acao.cliente.cnpj,
            empresa=acao.cliente.razao_social,
            categoria=acao.categoria,
            quantidade_xml=0,
            status="ERRO_XML_ORIGEM_ALTERADO",
            detalhe=erro,
            arquivo_zip=str(acao.arquivo_zip),
        )
    if _identidades(atuais) != _identidades(acao.xmls):
        return Registro(
            codigo_cliente=acao.cliente.codigo,
            cnpj=acao.cliente.cnpj,
            empresa=acao.cliente.razao_social,
            categoria=acao.categoria,
            quantidade_xml=len(atuais),
            status="ERRO_ORIGEM_ALTERADA",
            detalhe="Os XMLs mudaram depois da conferência. Execute novamente.",
            arquivo_zip=str(acao.arquivo_zip),
        )

    try:
        acao.pasta_competencia.mkdir(exist_ok=True)
    except OSError as erro_mkdir:
        return Registro(
            codigo_cliente=acao.cliente.codigo,
            cnpj=acao.cliente.cnpj,
            empresa=acao.cliente.razao_social,
            categoria=acao.categoria,
            quantidade_xml=len(acao.xmls),
            status="ERRO_CRIAR_COMPETENCIA",
            detalhe=str(erro_mkdir),
            arquivo_zip=str(acao.arquivo_zip),
        )

    esperado = _identidades(acao.xmls)
    if acao.arquivo_zip.exists():
        try:
            existente = manifesto_zip(acao.arquivo_zip)
        except ValueError as erro_zip:
            return Registro(
                codigo_cliente=acao.cliente.codigo,
                cnpj=acao.cliente.cnpj,
                empresa=acao.cliente.razao_social,
                categoria=acao.categoria,
                quantidade_xml=len(acao.xmls),
                status="CONFLITO_ZIP_EXISTENTE_INVALIDO",
                detalhe=str(erro_zip),
                arquivo_zip=str(acao.arquivo_zip),
            )
        if existente == esperado:
            return Registro(
                codigo_cliente=acao.cliente.codigo,
                cnpj=acao.cliente.cnpj,
                empresa=acao.cliente.razao_social,
                categoria=acao.categoria,
                quantidade_xml=len(acao.xmls),
                status="JA_EXISTENTE_IDENTICO",
                detalhe="O ZIP já contém exatamente os mesmos XMLs; nada foi alterado.",
                arquivo_zip=str(acao.arquivo_zip),
            )
        return Registro(
            codigo_cliente=acao.cliente.codigo,
            cnpj=acao.cliente.cnpj,
            empresa=acao.cliente.razao_social,
            categoria=acao.categoria,
            quantidade_xml=len(acao.xmls),
            status="CONFLITO_ZIP_EXISTENTE_DIFERENTE",
            detalhe="Já existe um ZIP com o mesmo nome e conteúdo diferente. "
            "Nenhum arquivo foi sobrescrito.",
            arquivo_zip=str(acao.arquivo_zip),
        )

    temporario = acao.pasta_competencia / (
        f".{acao.arquivo_zip.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with zipfile.ZipFile(
            temporario,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as arquivo:
            for xml in acao.xmls:
                arquivo.write(xml.caminho, arcname=xml.nome)

        if manifesto_zip(temporario) != esperado:
            raise ValueError("O ZIP criado não corresponde aos XMLs de origem.")

        if acao.arquivo_zip.exists():
            raise FileExistsError(
                "O ZIP apareceu no destino durante a execução; não foi sobrescrito."
            )
        os.replace(temporario, acao.arquivo_zip)
        detalhe = "ZIP criado e validado."
        if ignorados:
            detalhe += " Arquivos não XML ignorados: " + ", ".join(ignorados)
        return Registro(
            codigo_cliente=acao.cliente.codigo,
            cnpj=acao.cliente.cnpj,
            empresa=acao.cliente.razao_social,
            categoria=acao.categoria,
            quantidade_xml=len(acao.xmls),
            status="CRIADO",
            detalhe=detalhe,
            arquivo_zip=str(acao.arquivo_zip),
        )
    except (OSError, ValueError, zipfile.BadZipFile) as erro_execucao:
        return Registro(
            codigo_cliente=acao.cliente.codigo,
            cnpj=acao.cliente.cnpj,
            empresa=acao.cliente.razao_social,
            categoria=acao.categoria,
            quantidade_xml=len(acao.xmls),
            status="ERRO_CRIAR_ZIP",
            detalhe=str(erro_execucao),
            arquivo_zip=str(acao.arquivo_zip),
        )
    finally:
        try:
            if temporario.exists():
                temporario.unlink()
        except OSError:
            logger.exception("Não foi possível remover o temporário %s", temporario)


@contextmanager
def lock_destino(pasta_destino: Path) -> Iterator[None]:
    caminho_lock = pasta_destino / ".robo_importacao_nfe.lock"
    descritor: int | None = None
    adquirido = False
    try:
        descritor = os.open(
            caminho_lock,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        adquirido = True
        conteudo = (
            f"pid={os.getpid()}\n"
            f"inicio={datetime.now().astimezone().isoformat()}\n"
            f"computador={os.environ.get('COMPUTERNAME', '')}\n"
        ).encode("utf-8")
        os.write(descritor, conteudo)
        os.close(descritor)
        descritor = None
        yield
    except FileExistsError as erro:
        raise ErroConfiguracao(
            "Já existe outra execução ou um lock antigo no destino: "
            f"{caminho_lock}. Confirme que nenhum robô está rodando antes de "
            "remover esse arquivo."
        ) from erro
    finally:
        if descritor is not None:
            os.close(descritor)
        if adquirido:
            try:
                if caminho_lock.exists():
                    caminho_lock.unlink()
            except OSError:
                logger.exception("Não foi possível remover o lock %s", caminho_lock)


def configurar_logging(pasta_relatorios: Path, identificador: str) -> Path:
    pasta_relatorios.mkdir(parents=True, exist_ok=True)
    caminho_log = pasta_relatorios / f"execucao_{identificador}.log"
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatador = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    arquivo = logging.FileHandler(caminho_log, encoding="utf-8")
    arquivo.setFormatter(formatador)
    logger.addHandler(arquivo)
    return caminho_log


def registro_da_acao_simulada(acao: AcaoCompactacao) -> Registro:
    return Registro(
        codigo_cliente=acao.cliente.codigo,
        cnpj=acao.cliente.cnpj,
        empresa=acao.cliente.razao_social,
        categoria=acao.categoria,
        quantidade_xml=len(acao.xmls),
        status="PRONTO_SIMULACAO",
        detalhe=acao.observacao or "ZIP pronto para ser criado.",
        arquivo_zip=str(acao.arquivo_zip),
    )


def salvar_relatorios(
    pasta_relatorios: Path,
    identificador: str,
    registros: tuple[Registro, ...],
    *,
    competencia: str,
    origem: Path,
    destino: Path,
    simulacao: bool,
    caminho_log: Path,
) -> tuple[Path, Path]:
    pasta_relatorios.mkdir(parents=True, exist_ok=True)
    caminho_csv = pasta_relatorios / f"relatorio_{identificador}.csv"
    caminho_json = pasta_relatorios / f"manifesto_{identificador}.json"
    campos = [
        "codigo_cliente",
        "cnpj",
        "empresa",
        "categoria",
        "quantidade_xml",
        "status",
        "detalhe",
        "arquivo_zip",
    ]
    with caminho_csv.open("w", encoding="utf-8-sig", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=campos, delimiter=";")
        escritor.writeheader()
        for registro in registros:
            escritor.writerow(asdict(registro))

    contagens = Counter(registro.status for registro in registros)
    manifesto = {
        "robo": NOME_ROBO,
        "versao": VERSAO,
        "execucao": identificador,
        "competencia": competencia,
        "origem": str(origem),
        "destino": str(destino),
        "simulacao": simulacao,
        "log": str(caminho_log),
        "contagens_status": dict(sorted(contagens.items())),
        "registros": [asdict(registro) for registro in registros],
    }
    caminho_json.write_text(
        json.dumps(manifesto, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return caminho_csv, caminho_json


def imprimir_plano(
    plano: Plano,
    *,
    competencia: str,
    origem: Path,
    destino: Path,
) -> None:
    total_xml = sum(len(acao.xmls) for acao in plano.acoes)
    print("\n" + "=" * 72)
    print(f"{NOME_ROBO} v{VERSAO} — CONFERÊNCIA")
    print("=" * 72)
    print(f"Competência: {competencia}")
    print(f"Origem:      {origem}")
    print(f"Destino:     {destino}")
    print(f"ZIPs planejados: {len(plano.acoes)}")
    print(f"XMLs validados:  {total_xml}")
    print("-" * 72)
    for acao in plano.acoes:
        print(
            f"[{acao.cliente.codigo}] {acao.cliente.razao_social} | "
            f"{acao.categoria}: {len(acao.xmls)} XML(s) -> {acao.arquivo_zip}"
        )
    if not plano.acoes:
        print("Nenhum ZIP precisa ser criado.")

    contagens = Counter(registro.status for registro in plano.registros)
    if contagens:
        print("-" * 72)
        print("Outras situações encontradas:")
        for status, quantidade in sorted(contagens.items()):
            print(f"  {status}: {quantidade}")
    print("=" * 72)


def imprimir_resultado(registros: tuple[Registro, ...]) -> None:
    contagens = Counter(registro.status for registro in registros)
    print("\n" + "=" * 72)
    print("RESULTADO FINAL")
    print("=" * 72)
    for status, quantidade in sorted(contagens.items()):
        print(f"{status}: {quantidade}")
    print("=" * 72)


def possui_problema(registros: tuple[Registro, ...]) -> bool:
    return any(
        registro.status.startswith(STATUS_COM_PROBLEMA) for registro in registros
    )


def criar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=NOME_ROBO)
    parser.add_argument(
        "--competencia",
        help="Competência no formato MM.AAAA, por exemplo 07.2026.",
    )
    parser.add_argument(
        "--origem",
        help="Pasta mensal exata. Se omitida, procura NOTA(S) - MM.AAAA em Downloads.",
    )
    parser.add_argument(
        "--destino",
        help="Raiz de Importação NFe. Se omitida, usa config.json.",
    )
    parser.add_argument(
        "--config",
        help="Arquivo JSON de configuração.",
    )
    parser.add_argument(
        "--simular",
        action="store_true",
        help="Confere tudo e gera relatório, sem criar pastas ou ZIPs no destino.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argumentos = criar_parser().parse_args(argv)
    pasta_script = Path(__file__).resolve().parent
    caminho_config = (
        Path(argumentos.config).resolve()
        if argumentos.config
        else pasta_script / "config.json"
    )

    try:
        config = carregar_configuracao(caminho_config)
        competencia_informada = argumentos.competencia
        if not competencia_informada:
            competencia_informada = input(
                "Informe a competência (MM.AAAA, exemplo 07.2026): "
            )
        competencia, competencia_mmaaaa = interpretar_competencia(
            competencia_informada
        )

        if argumentos.origem:
            pasta_origem = Path(argumentos.origem).expanduser().resolve(strict=False)
            if not pasta_origem.is_dir():
                raise ErroConfiguracao(
                    f"Pasta de origem não encontrada: {pasta_origem}"
                )
            validar_pasta_mensal_explicita(pasta_origem, competencia)
        else:
            pasta_origem = localizar_pasta_mensal(
                config.pasta_origem_base, competencia
            )

        pasta_destino = (
            Path(argumentos.destino).expanduser().resolve(strict=False)
            if argumentos.destino
            else config.pasta_destino_raiz
        )
        if not pasta_destino.is_dir():
            raise ErroConfiguracao(
                f"Pasta raiz de destino não encontrada: {pasta_destino}"
            )

        clientes = carregar_clientes(config.arquivo_clientes)
        identificador = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        caminho_log = configurar_logging(config.pasta_relatorios, identificador)
        logger.info(
            "Início. Competência=%s Origem=%s Destino=%s Simulação=%s",
            competencia,
            pasta_origem,
            pasta_destino,
            argumentos.simular,
        )
        logger.info("Cadastro validado: %d clientes ativos.", len(clientes))

        plano = planejar_execucao(
            clientes,
            pasta_origem,
            pasta_destino,
            competencia_mmaaaa,
        )
        imprimir_plano(
            plano,
            competencia=competencia,
            origem=pasta_origem,
            destino=pasta_destino,
        )

        if argumentos.simular:
            registros = plano.registros + tuple(
                registro_da_acao_simulada(acao) for acao in plano.acoes
            )
            caminho_csv, caminho_json = salvar_relatorios(
                config.pasta_relatorios,
                identificador,
                registros,
                competencia=competencia,
                origem=pasta_origem,
                destino=pasta_destino,
                simulacao=True,
                caminho_log=caminho_log,
            )
            imprimir_resultado(registros)
            print(f"Relatório: {caminho_csv}")
            print(f"Manifesto: {caminho_json}")
            logger.info("Simulação concluída. Relatório=%s", caminho_csv)
            return 2 if possui_problema(registros) else 0

        if not plano.acoes:
            resposta = ""
        else:
            print(
                "\nNenhuma alteração foi feita ainda. Para criar os ZIPs acima, "
                "digite PROCESSAR."
            )
            resposta = input("Confirmação: ").strip().upper()

        if plano.acoes and resposta != "PROCESSAR":
            registros = plano.registros + tuple(
                Registro(
                    codigo_cliente=acao.cliente.codigo,
                    cnpj=acao.cliente.cnpj,
                    empresa=acao.cliente.razao_social,
                    categoria=acao.categoria,
                    quantidade_xml=len(acao.xmls),
                    status="CANCELADO_PELO_USUARIO",
                    detalhe="Plano conferido, mas não autorizado.",
                    arquivo_zip=str(acao.arquivo_zip),
                )
                for acao in plano.acoes
            )
        else:
            resultados_acoes: list[Registro] = []
            if plano.acoes:
                with lock_destino(pasta_destino):
                    for indice, acao in enumerate(plano.acoes, start=1):
                        logger.info(
                            "Processando %d/%d: código=%s categoria=%s XMLs=%d",
                            indice,
                            len(plano.acoes),
                            acao.cliente.codigo,
                            acao.categoria,
                            len(acao.xmls),
                        )
                        resultado = executar_acao(acao)
                        resultados_acoes.append(resultado)
                        logger.info(
                            "Resultado código=%s categoria=%s status=%s detalhe=%s",
                            resultado.codigo_cliente,
                            resultado.categoria,
                            resultado.status,
                            resultado.detalhe,
                        )
            registros = plano.registros + tuple(resultados_acoes)

        caminho_csv, caminho_json = salvar_relatorios(
            config.pasta_relatorios,
            identificador,
            registros,
            competencia=competencia,
            origem=pasta_origem,
            destino=pasta_destino,
            simulacao=False,
            caminho_log=caminho_log,
        )
        imprimir_resultado(registros)
        print(f"Relatório: {caminho_csv}")
        print(f"Manifesto: {caminho_json}")
        logger.info("Execução concluída. Relatório=%s", caminho_csv)
        return 2 if possui_problema(registros) else 0

    except ErroConfiguracao as erro:
        print(f"\nERRO: {erro}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nExecução cancelada pelo usuário.", file=sys.stderr)
        return 130
    except Exception as erro:
        logger.exception("Erro inesperado")
        print(f"\nERRO INESPERADO: {erro}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
