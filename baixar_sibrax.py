from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


URL_LOGIN_PADRAO = "https://emissor.sibrax.com.br/app/entrar"
ARQUIVOS_TEMPORARIOS = {".crdownload", ".part", ".tmp"}


class ErroSibrax(RuntimeError):
    """Erro esperado durante o download no portal Sibrax."""


@dataclass(frozen=True)
class ConfiguracaoSibrax:
    url: str
    usuario: str
    senha: str
    empresa: str
    timeout_segundos: int


@dataclass(frozen=True)
class ResultadoDownload:
    arquivo_baixado: Path
    pasta_mensal: Path
    empresas_selecionadas: int


def _sem_acentos(valor: str) -> str:
    normalizado = unicodedata.normalize("NFKD", valor)
    return "".join(letra for letra in normalizado if not unicodedata.combining(letra))


def _texto_comparavel(valor: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", _sem_acentos(valor).upper())


def _cnpj_somente_digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor)


def carregar_env(caminho: Path) -> dict[str, str]:
    """Lê um .env simples sem incluir dependência externa."""
    dados: dict[str, str] = {}
    if not caminho.is_file():
        raise ErroSibrax(
            f"Arquivo de credenciais não encontrado: {caminho}. "
            "Copie o .env.example para .env e preencha os campos."
        )

    for numero, linha_original in enumerate(
        caminho.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        linha = linha_original.strip()
        if not linha or linha.startswith("#"):
            continue
        if linha.lower().startswith("export "):
            linha = linha[7:].lstrip()
        if "=" not in linha:
            raise ErroSibrax(f"Linha {numero} inválida no arquivo .env.")
        chave, valor = linha.split("=", 1)
        chave = chave.strip()
        valor = valor.strip()
        if (
            len(valor) >= 2
            and valor[0] == valor[-1]
            and valor[0] in {'"', "'"}
        ):
            valor = valor[1:-1]
        dados[chave] = valor

    return dados


def carregar_configuracao_sibrax(caminho_env: Path) -> ConfiguracaoSibrax:
    dados = carregar_env(caminho_env)
    url = dados.get("SIBRAX_URL", "").strip() or URL_LOGIN_PADRAO
    usuario = dados.get("SIBRAX_USUARIO", "").strip()
    senha = dados.get("SIBRAX_SENHA", "")
    empresa = dados.get("SIBRAX_EMPRESA", "").strip()

    ausentes = [
        nome
        for nome, valor in (
            ("SIBRAX_USUARIO", usuario),
            ("SIBRAX_SENHA", senha),
            ("SIBRAX_EMPRESA", empresa),
        )
        if not valor
    ]
    if ausentes:
        raise ErroSibrax(
            "Preencha no arquivo .env: " + ", ".join(ausentes) + "."
        )

    timeout_texto = dados.get("SIBRAX_TIMEOUT_SEGUNDOS", "900").strip()
    try:
        timeout = int(timeout_texto)
    except ValueError as erro:
        raise ErroSibrax("SIBRAX_TIMEOUT_SEGUNDOS precisa ser um número inteiro.") from erro
    if not 60 <= timeout <= 3600:
        raise ErroSibrax(
            "SIBRAX_TIMEOUT_SEGUNDOS precisa ficar entre 60 e 3600."
        )

    if not url.lower().startswith(("https://", "http://")):
        raise ErroSibrax("SIBRAX_URL precisa começar com https:// ou http://.")

    return ConfiguracaoSibrax(
        url=url,
        usuario=usuario,
        senha=senha,
        empresa=empresa,
        timeout_segundos=timeout,
    )


def _hash_arquivo(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


def _garantir_destino_seguro(destino: Path, membro: str) -> Path:
    relativo = Path(membro.replace("\\", "/"))
    if relativo.is_absolute() or ".." in relativo.parts:
        raise ErroSibrax(f"O arquivo baixado contém caminho inseguro: {membro}")
    resolvido = (destino / relativo).resolve()
    try:
        resolvido.relative_to(destino.resolve())
    except ValueError as erro:
        raise ErroSibrax(f"O arquivo baixado contém caminho inseguro: {membro}") from erro
    return resolvido


def extrair_zip_seguro(arquivo_zip: Path, destino: Path) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(arquivo_zip, "r") as pacote:
            membros = pacote.infolist()
            if not membros:
                raise ErroSibrax("O ZIP baixado está vazio.")
            for membro in membros:
                _garantir_destino_seguro(destino, membro.filename)
                if membro.is_dir():
                    continue
                if membro.file_size > 200 * 1024 * 1024:
                    raise ErroSibrax(
                        f"Arquivo inesperadamente grande dentro do ZIP: {membro.filename}"
                    )
            pacote.extractall(destino)
    except zipfile.BadZipFile as erro:
        raise ErroSibrax(f"O download não é um ZIP válido: {arquivo_zip.name}") from erro


def _encontrar_raiz_das_empresas(pasta_extraida: Path) -> Path:
    candidatas = [pasta_extraida]
    candidatas.extend(
        pasta for pasta in pasta_extraida.rglob("*") if pasta.is_dir()
    )
    for candidata in candidatas:
        filhos = [pasta for pasta in candidata.iterdir() if pasta.is_dir()]
        quantidade_clientes = sum(
            1 for pasta in filhos if re.match(r"^\d{14}-", pasta.name)
        )
        if quantidade_clientes:
            return candidata
    raise ErroSibrax(
        "O ZIP foi baixado, mas não contém pastas de empresas no padrão "
        "CNPJ-NOME esperado."
    )


def _mesclar_arvore_sem_sobrescrever(origem: Path, destino: Path) -> None:
    conflitos: list[str] = []

    for arquivo in sorted(caminho for caminho in origem.rglob("*") if caminho.is_file()):
        relativa = arquivo.relative_to(origem)
        alvo = destino / relativa
        if not alvo.exists():
            continue
        if not alvo.is_file() or arquivo.stat().st_size != alvo.stat().st_size:
            conflitos.append(str(relativa))
            continue
        if _hash_arquivo(arquivo) != _hash_arquivo(alvo):
            conflitos.append(str(relativa))

    if conflitos:
        amostra = ", ".join(conflitos[:5])
        complemento = "" if len(conflitos) <= 5 else f" e mais {len(conflitos) - 5}"
        raise ErroSibrax(
            "A pasta mensal já contém arquivos diferentes com o mesmo nome: "
            f"{amostra}{complemento}. Nada foi sobrescrito."
        )

    destino.mkdir(parents=True, exist_ok=True)
    for pasta in sorted(caminho for caminho in origem.rglob("*") if caminho.is_dir()):
        relativa = pasta.relative_to(origem)
        (destino / relativa).mkdir(parents=True, exist_ok=True)

    for arquivo in sorted(caminho for caminho in origem.rglob("*") if caminho.is_file()):
        relativa = arquivo.relative_to(origem)
        alvo = destino / relativa
        if alvo.exists():
            continue
        alvo.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(arquivo, alvo)


def preparar_pasta_mensal(
    arquivo_baixado: Path,
    competencia: str,
    downloads_usuario: Path,
    pasta_trabalho: Path,
) -> Path:
    if not zipfile.is_zipfile(arquivo_baixado):
        raise ErroSibrax(
            f"Formato de download não reconhecido: {arquivo_baixado.name}. "
            "Era esperado um arquivo ZIP."
        )

    extraida = pasta_trabalho / "extraido"
    extrair_zip_seguro(arquivo_baixado, extraida)
    raiz_empresas = _encontrar_raiz_das_empresas(extraida)
    pasta_mensal = downloads_usuario / f"NOTAS - {competencia}"
    _mesclar_arvore_sem_sobrescrever(raiz_empresas, pasta_mensal)
    return pasta_mensal


def _arquivo_sibrax_da_competencia(caminho: Path, competencia: str) -> bool:
    if caminho.suffix.lower() != ".zip":
        return False
    mes, ano = competencia.split(".")
    nome = _sem_acentos(caminho.stem).upper()
    formatos_periodo = {
        f"{mes}-{ano}",
        f"{mes}.{ano}",
        f"{mes}_{ano}",
        f"{mes}{ano}",
    }
    return "SIBRAX" in nome and any(periodo in nome for periodo in formatos_periodo)


def _inventario_downloads(pasta: Path) -> dict[Path, tuple[int, int]]:
    inventario: dict[Path, tuple[int, int]] = {}
    for caminho in pasta.iterdir():
        if not caminho.is_file():
            continue
        try:
            estado = caminho.stat()
        except OSError:
            continue
        inventario[caminho] = (estado.st_size, estado.st_mtime_ns)
    return inventario


def _aguardar_download(
    pasta: Path,
    existentes: dict[Path, tuple[int, int]],
    timeout_segundos: int,
    competencia: str,
) -> Path:
    limite = time.monotonic() + timeout_segundos
    tamanho_anterior: dict[Path, int] = {}
    estavel: dict[Path, int] = {}

    while time.monotonic() < limite:
        arquivos: set[Path] = set()
        for caminho in pasta.iterdir():
            if not caminho.is_file():
                continue
            try:
                estado = caminho.stat()
            except OSError:
                continue
            estado_atual = (estado.st_size, estado.st_mtime_ns)
            if caminho not in existentes or existentes[caminho] != estado_atual:
                arquivos.add(caminho)

        temporarios = {
            caminho
            for caminho in arquivos
            if caminho.suffix.lower() in ARQUIVOS_TEMPORARIOS
        }
        finalizados = sorted(
            (
                caminho
                for caminho in arquivos - temporarios
                if _arquivo_sibrax_da_competencia(caminho, competencia)
            ),
            key=lambda caminho: caminho.stat().st_mtime,
            reverse=True,
        )

        for arquivo in finalizados:
            tamanho = arquivo.stat().st_size
            if tamanho > 0 and tamanho_anterior.get(arquivo) == tamanho:
                estavel[arquivo] = estavel.get(arquivo, 0) + 1
            else:
                estavel[arquivo] = 0
            tamanho_anterior[arquivo] = tamanho
            if estavel[arquivo] >= 2 and not temporarios:
                return arquivo
        time.sleep(1)

    raise ErroSibrax(
        "O Sibrax não concluiu o ZIP da competência "
        f"{competencia} em {timeout_segundos} segundos. Era esperado um nome "
        "como EMPRESA_[MM-AAAA]_[SIBRAX].zip na pasta Downloads."
    )


def _importar_selenium():
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as erro:
        raise ErroSibrax(
            "A biblioteca Selenium não está instalada. Execute "
            "INSTALAR_DEPENDENCIAS.bat uma vez neste computador."
        ) from erro

    return webdriver, TimeoutException, WebDriverException, By, EC, WebDriverWait


def _separar_empresa_e_cnpj(valor: str) -> tuple[str, str]:
    correspondencia_cnpj = re.search(
        r"(\d{2}\D*\d{3}\D*\d{3}\D*\d{4}\D*\d{2})\s*$", valor
    )
    cnpj = (
        _cnpj_somente_digitos(correspondencia_cnpj.group(1))
        if correspondencia_cnpj
        else ""
    )
    nome = valor[: correspondencia_cnpj.start()].rstrip(" /-|")
    return _texto_comparavel(nome), cnpj


def _selecionar_empresa_inicial(driver, By, empresa: str) -> None:
    limite = time.monotonic() + 30
    nome_procurado, cnpj_procurado = _separar_empresa_e_cnpj(empresa)
    ultimo_total = 0

    while time.monotonic() < limite:
        links_empresas = driver.find_elements(By.CSS_SELECTOR, "table td > a")
        ultimo_total = len(links_empresas)
        correspondencias = []

        for link in links_empresas:
            try:
                celula = link.find_element(By.XPATH, "..")
                linha = celula.find_element(By.XPATH, "..")
                nome_link = _texto_comparavel(link.text)
                cnpj_linha = _cnpj_somente_digitos(linha.text)
            except Exception:
                continue

            nome_confere = bool(
                nome_procurado
                and (
                    nome_link == nome_procurado
                    or nome_procurado in nome_link
                )
            )
            cnpj_confere = bool(
                len(cnpj_procurado) == 14
                and cnpj_procurado in cnpj_linha
            )
            if nome_confere and (not cnpj_procurado or cnpj_confere):
                correspondencias.append(celula)
            elif not nome_procurado and cnpj_confere:
                correspondencias.append(celula)

        if len(correspondencias) == 1:
            escolhido = correspondencias[0]
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", escolhido
            )
            try:
                escolhido.click()
            except Exception:
                driver.execute_script("arguments[0].click();", escolhido)
            return
        if len(correspondencias) > 1:
            raise ErroSibrax(
                "Mais de uma linha corresponde à empresa inicial. "
                "Use o nome completo e o CNPJ no arquivo .env."
            )
        time.sleep(0.5)

    raise ErroSibrax(
        "A empresa inicial não apareceu entre os "
        f"{ultimo_total} nomes da tabela. Confira SIBRAX_EMPRESA no arquivo .env."
    )


def _abrir_download_xmls(driver, By, WebDriverWait, EC) -> None:
    espera = WebDriverWait(driver, 30)
    menu = espera.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#menu_escritorio"))
    )
    link_download = menu.find_element(
        By.CSS_SELECTOR, "a[href='/app/escritorio/download']"
    )

    if not link_download.is_displayed():
        abrir_menu = menu.find_element(By.XPATH, "./a")
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", abrir_menu
        )
        abrir_menu.click()
        espera.until(
            EC.visibility_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "#menu_escritorio a[href='/app/escritorio/download']",
                )
            )
        )

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});", link_download
    )
    link_download.click()
    espera.until(lambda navegador: "/app/escritorio/download" in navegador.current_url)


def _selecionar_todas_empresas(driver, By) -> int:
    checkbox_todas = driver.find_element(By.CSS_SELECTOR, "#selecionaTudo")
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});", checkbox_todas
    )
    if not checkbox_todas.is_selected():
        try:
            checkbox_todas.click()
        except Exception:
            driver.execute_script("arguments[0].click();", checkbox_todas)

    if not checkbox_todas.is_selected():
        raise ErroSibrax("A opção de selecionar todas as empresas não foi marcada.")

    limite = time.monotonic() + 5
    total = 0
    selecionadas = 0
    while time.monotonic() < limite:
        checkboxes_empresas = driver.find_elements(
            By.CSS_SELECTOR,
            "table input[type='checkbox']:not(#selecionaTudo)",
        )
        total = len(checkboxes_empresas)
        selecionadas = sum(
            1 for checkbox in checkboxes_empresas if checkbox.is_selected()
        )
        if total and selecionadas == total:
            return selecionadas
        time.sleep(0.2)

    raise ErroSibrax(
        f"O selecionar tudo marcou {selecionadas} de {total} empresas."
    )


def baixar_notas_sibrax(
    competencia: str,
    pasta_script: Path,
    *,
    caminho_env: Path | None = None,
) -> ResultadoDownload:
    if not re.fullmatch(r"(0[1-9]|1[0-2])\.\d{4}", competencia):
        raise ErroSibrax("A competência precisa estar no formato MM.AAAA.")

    config = carregar_configuracao_sibrax(caminho_env or pasta_script / ".env")
    webdriver, TimeoutException, WebDriverException, By, EC, WebDriverWait = (
        _importar_selenium()
    )

    downloads_usuario = Path.home() / "Downloads"
    identificador = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    pasta_trabalho = downloads_usuario / "ROBO_SIBRAX_TEMP" / identificador
    pasta_trabalho.mkdir(parents=True, exist_ok=False)
    pasta_download = downloads_usuario

    opcoes = webdriver.ChromeOptions()
    opcoes.add_argument("--start-maximized")
    opcoes.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(pasta_download.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "safebrowsing.enabled": True,
        },
    )
    opcoes.add_experimental_option(
        "excludeSwitches", ["enable-logging", "enable-automation"]
    )

    driver = None
    try:
        print("\nAbrindo o Sibrax em uma janela segura do Chrome...")
        driver = webdriver.Chrome(options=opcoes)
        espera = WebDriverWait(driver, 40)
        driver.get(config.url)

        campo_usuario = espera.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "#username"))
        )
        campo_senha = espera.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "#password"))
        )
        botao_entrar = espera.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#entrar"))
        )
        campo_usuario.clear()
        campo_usuario.send_keys(config.usuario)
        campo_senha.clear()
        campo_senha.send_keys(config.senha)
        botao_entrar.click()

        espera.until(lambda navegador: "/app/entrar" not in navegador.current_url)
        if "/selecionar-empresa" in driver.current_url:
            espera.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table td > a"))
            )
            _selecionar_empresa_inicial(driver, By, config.empresa)
            espera.until(
                lambda navegador: "/selecionar-empresa" not in navegador.current_url
            )

        _abrir_download_xmls(driver, By, WebDriverWait, EC)
        espera.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#periodo"))
        )

        campo_periodo = espera.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "#periodo"))
        )
        campo_periodo.clear()
        campo_periodo.send_keys(competencia.replace(".", "/"))

        selecionadas = _selecionar_todas_empresas(driver, By)
        botao_download = espera.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#downloadPeriodo"))
        )

        existentes = _inventario_downloads(pasta_download)
        print(
            f"{selecionadas} empresas selecionadas. Solicitando a competência "
            f"{competencia}..."
        )
        print(
            "O Sibrax normalmente leva de 1 a 2 minutos para gerar o ZIP. "
            f"O robô aguardará até {config.timeout_segundos // 60} minutos."
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", botao_download
        )
        botao_download.click()
        arquivo_baixado = _aguardar_download(
            pasta_download,
            existentes,
            config.timeout_segundos,
            competencia,
        )
        pasta_mensal = preparar_pasta_mensal(
            arquivo_baixado,
            competencia,
            downloads_usuario,
            pasta_trabalho,
        )
        return ResultadoDownload(
            arquivo_baixado=arquivo_baixado,
            pasta_mensal=pasta_mensal,
            empresas_selecionadas=selecionadas,
        )
    except TimeoutException as erro:
        raise ErroSibrax(
            "O Sibrax demorou demais para responder. Confira a internet e tente novamente."
        ) from erro
    except WebDriverException as erro:
        mensagem = str(erro).splitlines()[0]
        raise ErroSibrax(f"Não foi possível controlar o Chrome: {mensagem}") from erro
    finally:
        if driver is not None:
            driver.quit()
