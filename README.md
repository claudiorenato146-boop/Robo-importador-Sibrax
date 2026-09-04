# Robô de Importação NFe

Este robô lê as pastas mensais exportadas pelo sistema, compacta separadamente
os XMLs de `Entradas`, `Saidas` e `Canceladas` e salva os ZIPs na competência
correta das pastas já existentes dos clientes.

Ele não processa CTe, não apaga arquivos da origem, não cria pastas de clientes
e nunca sobrescreve silenciosamente um ZIP existente.

## Novo modo completo: Sibrax + preparação para o Domínio

O arquivo `EXECUTAR_ROBO_COMPLETO.bat` agora realiza o processo inteiro:

1. abre uma nova janela do Chrome;
2. entra no Sibrax com o usuário e a senha do `.env`;
3. escolhe a empresa inicial pelo nome completo ou CNPJ;
4. abre diretamente a tela `Download XMLs`;
5. informa a competência;
6. marca todas as empresas disponíveis no lote;
7. aguarda o Sibrax terminar o download;
8. extrai o ZIP com segurança para `Downloads\NOTAS - MM.AAAA`;
9. executa a conferência e a preparação dos ZIPs para o Domínio.

O robô não registra nem mostra a senha no terminal ou nos relatórios.

### Configuração inicial em cada computador

Copie os três modelos e preencha com os seus dados — nenhum dos originais
é versionado:

```bash
copy config.exemplo.json config.json
copy .env.example .env
copy clientes_nfe.exemplo.csv clientes_nfe.csv
```

`config.json` define as pastas de origem e destino; o `.env`, o acesso ao
Sibrax; o CSV, a carteira de clientes.


1. Execute uma vez o arquivo `INSTALAR_DEPENDENCIAS.bat`.
2. Abra o arquivo `.env` no Bloco de Notas.
3. Preencha:

```text
SIBRAX_URL=https://emissor.sibrax.com.br/app/entrar
SIBRAX_USUARIO=seu_usuario
SIBRAX_SENHA=sua_senha
SIBRAX_EMPRESA=nome completo ou CNPJ da empresa inicial
SIBRAX_TIMEOUT_SEGUNDOS=900
```

4. Salve o `.env`.
5. Execute `EXECUTAR_ROBO_COMPLETO.bat`.

Se a pasta do robô estiver no servidor e todos usarem a mesma configuração, o
`.env` pode permanecer ao lado do robô. O arquivo `.env.example` é apenas um
modelo sem credenciais.

O ChromeDriver não precisa ser instalado manualmente: o Selenium cuida dessa
compatibilidade. Na primeira execução, o computador precisa ter internet para
obter o componente compatível quando necessário.

### Proteções no download

- a opção `Enviar por e-mail` não é marcada;
- somente as caixas das empresas da tabela são selecionadas;
- o robô espera o arquivo terminar de baixar antes de continuar;
- o ZIP é reconhecido pelo padrão `EMPRESA_[MM-AAAA]_[SIBRAX].zip`;
- o robô aguarda até o limite do `.env`, mesmo que a geração leve alguns minutos;
- outros arquivos baixados ao mesmo tempo são ignorados;
- caminhos perigosos dentro do ZIP são bloqueados;
- se a pasta mensal já existir, arquivos idênticos são aceitos;
- arquivos diferentes com o mesmo nome nunca são sobrescritos;
- a pasta temporária local fica em `Downloads\ROBO_SIBRAX_TEMP`.

O atalho `EXECUTAR_ROBO_NFE.bat` continua disponível para o modo antigo, quando
o lote já estiver baixado em `Downloads`.

## Estrutura esperada na origem

Exemplo para julho de 2026:

```text
C:\Users\seu_usuario\Downloads\NOTAS - 07.2026
└── 99900002000102-MODELO_SERVICOS_ADMINISTRATIVOS_LTDA
    ├── resumo.txt                 <- ignorado
    ├── CTe                        <- ignorado
    └── NFe
        ├── Entradas
        │   ├── nota1.xml
        │   └── nota2.xml
        ├── Saidas
        │   └── nota3.xml
        └── Canceladas
            └── nota4.xml
```

O nome mensal pode ser `NOTAS - 07.2026` ou `NOTA - 07.2026`.

## Estrutura gerada no destino

O destino padrão é:

```text
X:\Fiscal\Importação NFe
```

As pastas dos clientes já precisam existir no formato `codigo-apelido`. O robô
cria apenas a competência `MMAAAA` quando houver pelo menos um ZIP válido para
salvar.

```text
Importação NFe
└── 124-MODELO SERVICOS
    └── 072026
        ├── NFe_Entradas_99900002000102.zip
        ├── NFe_Saidas_99900002000102.zip
        └── NFe_Canceladas_99900002000102.zip
```

Dentro de cada ZIP ficam somente os XMLs, diretamente na raiz. Nenhuma
subpasta é adicionada ao ZIP.

## Como executar

1. Gere o lote no sistema.
2. Confirme que a pasta mensal está em `Downloads`.
3. Confirme que a unidade de rede do destino está conectada.
4. Clique duas vezes em `EXECUTAR_ROBO_NFE.bat`.
5. Informe a competência no formato `MM.AAAA`.
6. Leia a conferência apresentada pelo robô.
7. Digite `PROCESSAR` somente se origem, destino e quantidades estiverem
   corretos.

O robô primeiro monta o plano sem escrever no destino. Somente depois da
confirmação ele cria as competências e os ZIPs.

## Comportamentos importantes

- Empresa sem pasta na origem: `SEM_MOVIMENTO`, sem erro.
- Pasta `NFe` ausente: `SEM_NFE`, sem erro.
- Categoria ausente ou vazia: não gera ZIP e não é erro.
- Empresa da origem fora do cadastro de clientes ativos: ignorada e registrada.
- Cliente ativo sem pasta no destino: ignorado e registrado, sem criação.
- XML vazio, malformado ou dentro de subpasta inesperada: a categoria não é
  compactada e vira erro no relatório.
- ZIP existente com exatamente os mesmos XMLs: não é recriado.
- ZIP existente com conteúdo diferente: não é sobrescrito; vira conflito.
- Duas pastas de destino com o mesmo código: nenhuma é escolhida.
- Duas pastas de origem com o mesmo CNPJ: nenhuma é escolhida.

## Relatórios

Cada execução gera:

- um relatório CSV para conferência no Excel;
- um manifesto JSON detalhado;
- um log de texto.

Eles ficam na pasta `relatorios`, ao lado do robô. O relatório diferencia
sucesso, ausência normal de movimento, cliente ignorado, conflito e erro.

## Modo de simulação

Para conferir sem criar competências ou ZIPs:

```powershell
python robo_importacao_nfe.py --competencia 07.2026 --simular
```

Também é possível informar caminhos alternativos para testes:

```powershell
python robo_importacao_nfe.py `
  --competencia 07.2026 `
  --origem "C:\Teste\NOTAS - 07.2026" `
  --destino "C:\Teste\Importacao NFe" `
  --simular
```

## Cadastro dos clientes

O cadastro fica em `clientes_nfe.csv`, que **não é versionado**. Copie
`clientes_nfe.exemplo.csv`, renomeie e preencha com as suas empresas.

O vínculo é sempre feito pelo CNPJ completo de 14 dígitos e pelo código exato
da pasta de destino.
O nome da empresa nunca é usado para adivinhar correspondências.

Qualquer alteração nesse arquivo é validada antes do processamento. CNPJ
inválido, código repetido ou CNPJ repetido bloqueia a execução.

## Requisitos

- Windows;
- Python 3.10 ou superior;
- Google Chrome;
- Selenium 4.46 ou superior para o modo completo;
- acesso à pasta mensal em `Downloads`;
- acesso de leitura e escrita ao destino;
- internet para acessar o Sibrax e para a instalação inicial do Selenium.

---

## Testes

```bash
pip install pytest
pytest -q
```

17 testes, sem abrir o Chrome: cobrem a leitura do `.env` (inclusive senha com
espaço e com `=`), a validação do cadastro, a montagem do ZIP só com XML na
raiz, a idempotência da reexecução e a recusa de sobrescrever um ZIP diferente.

## Segurança

- O `.env` **não é versionado** — é ele que guarda o usuário e a senha do
  Sibrax. Copie o `.env.example` e preencha na máquina.
- A senha nunca aparece no terminal, no log nem nos relatórios.
- `clientes_nfe.csv` e a pasta `relatorios/` estão no `.gitignore`: os
  relatórios registram caminhos de rede e nomes de usuário do Windows.
- Confira com `git status` antes de cada commit.

## Licença

MIT — veja [LICENSE](LICENSE). Use, copie e adapte à vontade.

## Aviso

Este repositório traz **só o código**. Nenhum cadastro de cliente, nenhuma
credencial e nenhum certificado estão aqui, e os caminhos de rede nos exemplos
são genéricos. Os arquivos `*.exemplo.*` existem para o projeto rodar sem
depender de dado real — copie, renomeie e preencha com os seus.
