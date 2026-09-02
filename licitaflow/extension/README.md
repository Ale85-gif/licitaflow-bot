# LicitaFlow — extensão (v0.1)

Assistente de acompanhamento de homologação no Compras.gov.br.
Roda dentro da sua própria sessão, no seu navegador, depois que **você**
faz login no gov.br.

## O que ela faz nesta versão

- Detecta se a aba está numa área autenticada do portal, por sinais visíveis.
- Bloqueia a análise enquanto não houver sessão.
- Lê UASG, número do pregão, fase e a tabela de itens da página aberta.
- Aplica a regra da ata: pendente é pendente, um item trava tudo.
- Guarda cada leitura localmente e mostra o que mudou desde a anterior.
- Deixa pronta a saída para um backend, sem enviar nada até você configurar.

## O que ela não faz, por decisão de projeto

Não automatiza login, não pede CPF ou senha, não lê cookies (a permissão
`cookies` nem está no manifest), não tenta CAPTCHA nem anti-bot, e não
esconde que é uma extensão.

## Instalar

**Chrome:** `chrome://extensions` → ative o Modo do desenvolvedor →
"Carregar sem compactação" → escolha esta pasta.

**Edge:** `edge://extensions` → Modo de desenvolvedor → "Carregar
descompactada". O mesmo pacote serve nos dois.

## Uso

1. Clique no ícone → "Abrir Compras.gov.br".
2. Faça login normalmente, com o segundo fator.
3. Abra o pregão que você acompanha.
4. Volte ao ícone: o status fica verde e "Analisar pregão" libera.

O badge do ícone mostra `on` / `off` conforme a sessão, sem precisar abrir
o popup.

## Configuração

A engrenagem abre dois campos, ambos opcionais:

- **URL da API** — enquanto vazia, nada sai do navegador. As leituras ficam
  em `chrome.storage.local` e a fila de envio se acumula até você
  configurar. Aponte para `http://localhost:8000` para usar com o backend
  local do painel.
- **Fornecedor acompanhado** — filtra os itens antes de aplicar a regra da
  ata, que é como o cálculo funciona na prática: a trava é por fornecedor,
  não pelo pregão inteiro.

## Ajustes que dependem do portal

Estes três pontos foram escritos de forma tolerante, mas valem uma
conferência com o portal aberto:

| Onde | O quê |
|---|---|
| `content.js` → `RX_SAIR`, `temIdentificacaoDeUsuario` | sinais de sessão ativa |
| `content.js` → `PADROES` | quais URLs contam como tela de pregão |
| `content.js` → `SITUACOES` | como o portal escreve "Homologado", "Em diligência" etc. |

A leitura de tabela mapeia **coluna pelo nome do cabeçalho**, não pela
posição — se o portal inserir ou reordenar colunas, continua funcionando.
Quando um dado não aparece, a extensão diz o que faltou em vez de completar
com suposição.

## Estrutura

```
manifest.json        MV3, permissões mínimas
content.js           único arquivo que toca a página (autocontido)
background.js        service worker: roteia, guarda, compara, pinta o badge
popup.html/css/js    interface
modules/
  homologacao.js     regra da ata e diferença entre leituras
services/
  api.js             config, histórico local, fila e envio
```

Content script em MV3 não aceita `import` declarativo — por isso o
`content.js` é autocontido, e a modularização vive no background e no popup,
que rodam como módulos ES.

## Limites conhecidos

- Só lê a página que está aberta; não varre pregões em segundo plano.
- Uma tela com itens carregados por rolagem infinita pode entregar leitura
  parcial. Role até o fim antes de analisar.
- A geração da ata ainda não existe: a v0.1 informa liberado/bloqueado.

---

## Ponte com o bot — logar uma vez, o bot roda o dia todo

O bot deixa de ter sessão própria. Ele pede uma URL, a extensão busca essa
URL dentro da aba que você já deixou logada, e devolve o JSON.

```
seu bot ──HTTP──▶ ponte.py ──WebSocket──▶ extensão
                                              │ executa na aba logada
seu bot ◀──JSON── ponte.py ◀──WebSocket──  Compras.gov.br
```

**Ligar:**

1. `uvicorn ponte:app --port 8765`
2. Faça login no Compras.gov.br como sempre e deixe a aba aberta.
3. No popup, clique em "Ligar" na Ponte. Fica verde.

**No seu bot**, troque a chamada:

```python
from ponte_cliente import get, SessaoExpirada

dados = get("https://.../api/itens", params={"uasg": "160082"})
```

Pode apagar tudo que existia só para manter sessão: `sessao.py`,
`worker.py`, a pasta `perfil_chrome`, o keepalive. A sessão é a do
navegador que você já usa.

**Por que a busca roda no content script e não no service worker:** só
dentro da página a requisição é same-origin de verdade. Saindo do service
worker, a origem é a extensão e cookie `SameSite=Strict` não vai junto —
é a causa clássica de "funciona no navegador e falha no bot".

Se você for deslogado, a ponte devolve HTTP 401 com `sessao_expirada`, e o
`ponte_cliente` levanta `SessaoExpirada`. O bot trata isso como caso
previsto em vez de tentar fazer parse da tela de login.
