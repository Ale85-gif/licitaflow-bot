# LicitaFlow — acesso e visão geral

Para o Diego, ao entrar no repositório pela primeira vez.
Escrito em 2026-09-04 pelo Claude que trabalha com o Alesson.

---

## 1. Acesso

Repositório: **github.com/Ale85-gif/licitaflow-bot** (privado, você já foi
adicionado como colaborador).

```bash
git clone https://github.com/Ale85-gif/licitaflow-bot.git
```

**O que NÃO vem no clone** (segredo/estado local, nunca commitado):

| Arquivo | O que é | Onde conseguir |
|---|---|---|
| `chaves.json` | Credencial de service account do Google (Sheets API) | Peça pro Alesson — ele tem uma cópia própria, fora do git |
| `.venv` local | Ambiente Python 3.11+ com as libs de `requirements.txt` | Cria você mesmo (`python -m venv`) |

Sem `chaves.json`, os bots não conseguem escrever na planilha "ATAS PNCP"
— tudo mais (leitura do dados.db, o painel HTML) funciona sem ele.

---

## 2. O que é, em uma frase

Automação do **Compras.gov.br** pra UASG 160082 (Prefeitura Militar de
Brasília): coleta dados de pregões (fase, situação de item, fornecedor
homologado), cruza tudo, e caminha na direção de gerar Atas de Registro de
Preços automaticamente. Roda **local**, numa máquina com Chrome real e
sessão gov.br autenticada — não é hospedável, é a mesma restrição que já
motivou o desenho do `sarp-agente/`.

---

## 3. Estrutura — o que cada coisa faz

```
Bot comprasnet .py / Bot comprasnet rapido.py
    → varredura principal: escreve na planilha "ATAS PNCP" (abas por
      pregão, BOT_HEADERS com 14 colunas fixas — Número Ata na 5ª,
      autorizada/Saldo na 9ª/10ª). É a MESMA planilha que o
      sync_service.py do SARP já lê hoje.

comum.py, auth.py, ponte.py, ponte_cliente.py
    → infraestrutura compartilhada (log, conexão Google, sessão gov.br
      via extensão Chrome + ponte FastAPI local na porta 8765)

capturar_fase.py
    → fase real do pregão (div.step-item[aria-selected="true"]),
      grava em dados.db (tabela pregoes_fase)

padronizar_itens.py, cruzar_item_fornecedor.py,
capturar_itens_fornecedor_leve.py, capturar_grupo.py
    → separam fornecedor/CNPJ, capturam situação real por item
      ([data-test="situacao-item"]), cruzam item × fornecedor
      (chave: pregão + item, nunca só o item — um item pode ter mais
      de um fornecedor)

homologacao.py, motor_homologacao.py
    → classifica situação (Homologado/Pendente/Fracassado/Anulado) sem
      nunca inventar valor pra texto desconhecido

fila_ata.py, motor_fila_ata.py
    → fila de itens elegíveis pra Ata, por fornecedor, com "Ata parcial
      disponível" calculado (não gerado)

bot_criar_ata.py
    → o mais avançado e mais delicado: clona a Ata-modelo específica de
      cada pregão (cada pregão tem a sua, não existe modelo universal),
      preenche a tabela de itens via editor rich-text dentro de um
      <iframe>. Autosave por debounce (~15-20s, sem botão "Salvar").
      A estrutura de colunas da tabela MUDA por modelo — não assuma
      índice fixo sem conferir o modelo específico.

api.py + licitaflow/licitaflow.html
    → painel de controle local (porta 8000), lê pregões do dados.db e
      da planilha, roda os bots sob comando explícito do usuário —
      nunca automático

verificacao/
    → consulta CEIS, CNEP, SICAF, CNPJ, PNCP (APIs públicas — não
      depende de sessão logada, é o tipo de coisa que já dá pra rodar
      direto no backend do SARP, sem precisar do agente local)

dados.db (SQLite, gitignored)
    → pregoes_indice, pregoes_itens, participacao_itens, pregoes_fase
```

---

## 4. Regra que vale aqui também

Mesmo espírito do ÁGORA: **nunca inventar dado**. Se não achou de
verdade no site, registra como faltando — nunca chuta um valor
plausível. Isso apareceu MUITAS vezes durante o desenvolvimento e é o
motivo de boa parte das decisões de arquitetura estarem do jeito que
estão (ex.: `homologacao.py` recusa classificar texto que não reconhece,
em vez de assumir o mais parecido).

---

## 5. Estado atual (04/09/2026)

**Funcionando e testado:**
- Captura de fase, situação de item, cruzamento item×fornecedor, fila
  de Ata — tudo validado contra dados reais (pregões 90020/2026,
  13/2026, 44/2026)
- Mecanismo de preenchimento de Ata confirmado funcionando (testado num
  clone descartável, 327/2026, derivado da Ata-modelo do 90020/2026)

**Bloqueado, não avança sozinho:**
- **De onde vem a "Quantidade Máxima" da Ata** — investigação real na
  Ata 283/2026 (pregão 44/2026, MAX-FER TOOLS COMERCIAL LTDA) mostra que
  não bate nem com a coluna "TOTAIS" do TR, nem com "quantidade
  ofertada" do fornecedor no Compras.gov.br. Não achamos, em nenhuma
  tela do site, um campo pronto de "quantidade adjudicada por
  fornecedor". Hipótese mais provável: é decisão administrativa de
  quem monta a Ata, não dado que o sistema entrega pronto. Próximo
  passo é perguntar direto pra quem preenche essa coluna na prática —
  não é algo que dê pra resolver só investigando o portal.
- Marca/Modelo: mecanismo de captura existe (`expandir_item_avulso_e_extrair`,
  validado no pregão 44/2026) mas **quebra em pregões na "Fase
  Recursal"** — funciona só em "Adjudicação/Homologação" em diante.

**Migração combinada, ainda não codada:**
- Bot vai parar de depender só da planilha como intermediário —
  `POST /arps/sync/bot` recebendo linhas direto do bot, com o mesmo
  formato de `_parse_rows`/`BOT_HEADERS`. Duas colunas manuais
  (`percentual_desconto`, `reajuste_em`) ainda sem schema — nomes e
  tipos já levantados com dado real da planilha, faltando implementar.

Histórico completo, sessão por sessão, em `sessoes/` no próprio
repositório — vale ler antes de mexer em qualquer bot específico.
