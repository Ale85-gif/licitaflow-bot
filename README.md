# Bots Comprasnet / Contratos.gov (PMB)

Automação que varre o portal [contratos.sistema.gov.br](https://contratos.sistema.gov.br)
e atualiza uma planilha Google Sheets com os pregões e atas de registro de preços
relevantes para a PMB.

## O que cada arquivo faz

- **`comum.py`** — funções e constantes compartilhadas (log, conexão com Google
  Sheets, retry contra erro 429 de cota, abrir/fechar o Chrome de automação,
  export para SQLite).
- **`Bot comprasnet rapido.py`** — **versão em uso** pelo orquestrador. Varre os
  pregões em que a PMB é **unidade gerenciadora**: a listagem de pregões/itens
  usa Chrome (Playwright), mas o detalhe de cada item é buscado via requisição
  HTTP direta (reaproveitando os cookies da sessão logada) em paralelo — ~13x
  mais rápido que abrir uma aba por item. Grava em abas individuais na
  planilha, além de `INDICE_PREGOES` e `BD_CONSOLIDADO`.
- **`Bot comprasnet .py`** — versão original (100% Playwright, um item por
  aba). Mantida como referência/fallback caso o portal mude algo que quebre a
  versão HTTP mas não a versão com navegador completo. Não é chamada pelo
  `executar_bots.py`.
- **`bot_participante.py`** — varre as atas de registro de preços em que a PMB é
  **participante** (não gerenciadora) e grava tudo na aba `PARTICIPAÇÃO`. Também
  abre seu próprio Chrome de automação ao iniciar.
- **`bot_criar_ata.py`** — **em construção**. Vai criar (não só ler) uma Ata de
  Registro de Preços no Contratos.gov.br. Por enquanto só investiga a tela de
  criação (`/arp/create`), sem preencher/enviar nada.
- **`executar_bots.py`** — orquestrador: roda o bot gerenciador (versão rápida),
  **encerra o Chrome de automação** e só então roda o bot de participação com
  um Chrome novo — evita reaproveitar uma instância que ficou muitas horas
  aberta e parou de responder ao CDP.

## Pré-requisitos

1. Python 3.11+ instalado.
2. Google Chrome instalado em `C:\Program Files\Google\Chrome\Application\chrome.exe`
   (caminho fixo em `comum.py`; ajuste a constante `CHROME` se o seu
   Chrome estiver em outro lugar).
3. Um arquivo `chaves.json` na raiz do projeto, com as credenciais de uma
   Service Account do Google com acesso de edição à planilha (Google Sheets API
   + Google Drive API habilitadas no projeto do Google Cloud). **Esse arquivo
   nunca deve ser commitado** — já está no `.gitignore`.
4. A planilha do Google (`PLANILHA_ID` em `comum.py`) compartilhada com o
   e-mail da Service Account.

## Instalação

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
```

## Como rodar

Rodar tudo (gerenciador + participação), na ordem certa:

```powershell
.venv\Scripts\python executar_bots.py
```

Ou rodar cada bot separadamente:

```powershell
.venv\Scripts\python "Bot comprasnet rapido.py"
.venv\Scripts\python bot_participante.py
```

> Os dois bots abrem o Chrome sozinhos (com
> `--remote-debugging-port=9222 --user-data-dir=C:\chrome-real`) e esperam 5s
> antes de continuar — pode ser necessário fazer login manualmente no portal
> gov.br na primeira execução, já que o perfil do Chrome (`C:\chrome-real`)
> fica fora da pasta do projeto e persiste a sessão entre execuções. Se o
> Chrome já estiver aberto com esse mesmo perfil, essa etapa não faz nada
> (o Chrome só permite uma instância por perfil).

## Banco de dados local (`dados.db`)

Além da planilha do Google, os dois bots também gravam os mesmos dados num
banco SQLite local (`dados.db`, na raiz do projeto) — um canal rápido para
outros programas seus consumirem sem passar pela API/cota do Google Sheets.
A cada execução as tabelas são recriadas do zero (mesmo comportamento das
abas correspondentes no Sheets: sempre refletem o estado da última rodada).

| Tabela              | Origem                                    | Conteúdo                                      |
|----------------------|-------------------------------------------|------------------------------------------------|
| `pregoes_itens`      | `Bot comprasnet rapido.py` (aba `BD_CONSOLIDADO`) | Um item por linha, de todos os pregões ativos |
| `pregoes_indice`     | `Bot comprasnet rapido.py` (aba `INDICE_PREGOES`) | Um resumo por pregão (status, vigência, saldo) |
| `participacao_itens` | `bot_participante.py` (aba `PARTICIPAÇÃO`)  | Um item por linha, das atas em que a PMB participa |

Todas as colunas são gravadas como texto (`TEXT`), do mesmo jeito que aparecem
na planilha. Exemplo de consulta:

```powershell
.venv\Scripts\python -c "import sqlite3; conn = sqlite3.connect('dados.db'); print(conn.execute('SELECT * FROM pregoes_indice LIMIT 5').fetchall())"
```

## Segurança

- `chaves.json`, `*.key`, `.venv/` e perfis de navegador locais estão no
  `.gitignore` e não devem ser commitados.
- A porta de depuração remota do Chrome (9222) só aceita conexões locais
  (`127.0.0.1`) por padrão.
