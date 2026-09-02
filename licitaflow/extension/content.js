/**
 * content.js — o único arquivo que toca a página do Compras.gov.br.
 *
 * Três responsabilidades, nesta ordem:
 *   1. dizer se a página está autenticada (sem ler cookie, sem pedir senha);
 *   2. dizer que tipo de página é;
 *   3. quando pedirem, extrair os dados visíveis do pregão.
 *
 * Nada aqui automatiza login, preenche formulário de autenticação ou tenta
 * esconder que é uma extensão. Ele lê o que já está na tela do usuário.
 *
 * Content script em MV3 não aceita `import` declarativo, então este arquivo
 * é propositalmente autocontido — a modularização vive no background e no
 * popup, que rodam como módulos ES.
 */

(() => {
  "use strict";

  const LOG = (...a) => console.debug("[LicitaFlow]", ...a);

  /* ------------------------------------------------------------------ *
   * 1. AUTENTICAÇÃO — por sinais visíveis, somados
   * ------------------------------------------------------------------ *
   * Nenhum seletor único decide. Cada sinal vale pontos e o veredito sai
   * da soma. Se o portal mudar um botão de lugar, a detecção degrada em
   * vez de quebrar.
   */

  const RX_SAIR = /\b(sair|logout|encerrar sess[aã]o|desconectar)\b/i;
  const RX_LOGIN_URL = /(sso|login|acesso\.gov\.br|autenticar|signin)/i;

  function temControleDeSaida() {
    const alvos = document.querySelectorAll("a,button,[role='button'],[role='menuitem']");
    for (const el of alvos) {
      const texto = (el.innerText || el.textContent || "").trim();
      const href = el.getAttribute("href") || "";
      if (texto.length < 40 && RX_SAIR.test(texto)) return true;
      if (RX_SAIR.test(href)) return true;
    }
    return false;
  }

  function temFormularioDeLogin() {
    return !!document.querySelector("input[type='password']");
  }

  function estaEmUrlDeLogin() {
    return RX_LOGIN_URL.test(location.href);
  }

  function temIdentificacaoDeUsuario() {
    // Rótulos que o portal usa perto do nome/perfil do usuário logado.
    const rx = /\b(meu perfil|minhas compras|perfil de acesso|unidade|uasg)\b/i;
    const cab = document.querySelector("header, nav, .header, #header, .barra-usuario");
    return !!cab && rx.test(cab.innerText || "");
  }

  function avaliarSessao() {
    let pontos = 0;
    if (temControleDeSaida()) pontos += 3;
    if (temIdentificacaoDeUsuario()) pontos += 2;
    if (temFormularioDeLogin()) pontos -= 3;
    if (estaEmUrlDeLogin()) pontos -= 3;

    return {
      autenticado: pontos >= 3,
      pontos,
      url: location.href,
      host: location.host,
      em: new Date().toISOString(),
    };
  }

  /* ------------------------------------------------------------------ *
   * 2. QUE PÁGINA É ESTA
   * ------------------------------------------------------------------ */

  const PADROES = [
    { tipo: "pregao", rx: /(pregao|compra|licitacao|processo)[^a-z]/i },
    { tipo: "itens", rx: /(itens|resultado|julgamento|homologa)/i },
    { tipo: "atas", rx: /(ata|registro-de-precos|arp)/i },
  ];

  function classificarPagina() {
    const alvo = location.pathname + location.search;
    for (const p of PADROES) if (p.rx.test(alvo)) return p.tipo;
    // Fallback: o conteúdo pode identificar a tela mesmo com URL genérica.
    const txt = (document.body?.innerText || "").slice(0, 4000);
    if (/n[uú]mero do preg[aã]o|preg[aã]o eletr[oô]nico/i.test(txt)) return "pregao";
    return "desconhecida";
  }

  /* ------------------------------------------------------------------ *
   * 3. EXTRAÇÃO — por rótulo e cabeçalho, nunca por posição
   * ------------------------------------------------------------------ */

  /** Procura "RÓTULO: valor" no texto visível, em qualquer lugar da página. */
  function porRotulo(rotulos, padraoValor) {
    const txt = document.body?.innerText || "";
    for (const r of rotulos) {
      const rx = new RegExp(`${r}\\s*[:\\-–]?\\s*(${padraoValor})`, "i");
      const m = txt.match(rx);
      if (m) return m[1].trim();
    }
    return null;
  }

  /**
   * Lê uma tabela mapeando COLUNAS PELO NOME do cabeçalho.
   * Se o portal inserir ou reordenar colunas, continua funcionando.
   */
  function lerTabelas(nomesEsperados) {
    const saida = [];
    for (const tabela of document.querySelectorAll("table")) {
      const ths = [...tabela.querySelectorAll("thead th, tr:first-child th, tr:first-child td")];
      if (ths.length < 2) continue;

      const cabecalhos = ths.map((th) => (th.innerText || "").trim().toLowerCase());
      const casa = nomesEsperados.some((n) => cabecalhos.some((c) => c.includes(n)));
      if (!casa) continue;

      const linhas = [...tabela.querySelectorAll("tbody tr")].filter((tr) => tr.children.length);
      for (const tr of linhas) {
        const celulas = [...tr.children].map((td) => (td.innerText || "").trim());
        const obj = {};
        cabecalhos.forEach((h, i) => { if (h) obj[h] = celulas[i] ?? ""; });
        saida.push(obj);
      }
      if (saida.length) break;
    }
    return saida;
  }

  const SITUACOES = {
    ok: /homologad|adjudicad/i,
    bad: /dilig[eê]nci|recurso|cancelad|desert|fracassad/i,
  };

  function traduzirSituacao(texto = "") {
    if (SITUACOES.ok.test(texto)) return "ok";
    if (SITUACOES.bad.test(texto)) return "bad";
    return "wait";
  }

  function acharCampo(linha, chaves) {
    for (const k of Object.keys(linha)) {
      if (chaves.some((c) => k.includes(c))) return linha[k];
    }
    return "";
  }

  function extrairPregao() {
    const uasg = porRotulo(["uasg", "unidade de compra", "unidade administrativa"], "\\d{6}");
    const numero = porRotulo(["preg[aã]o(?: eletr[oô]nico)?(?: n[ºo°.]*)?", "n[uú]mero da compra"],
                             "\\d{1,6}\\s*/\\s*\\d{4}");
    const fase = porRotulo(["fase", "situa[cç][aã]o da compra", "etapa"], "[A-Za-zÀ-ú/ ]{4,45}");
    const objeto = porRotulo(["objeto"], "[^\\n]{10,300}");

    const brutos = lerTabelas(["item", "descri", "situa"]);
    const itens = brutos.map((l, idx) => {
      const num = parseInt(acharCampo(l, ["item", "nº", "numero"]).replace(/\D/g, ""), 10);
      const situacao = acharCampo(l, ["situa", "status", "resultado"]);
      return {
        n: Number.isFinite(num) ? num : idx + 1,
        desc: acharCampo(l, ["descri", "material", "servi"]) || "",
        unidade: acharCampo(l, ["unidade", "und", "medida"]) || "",
        fornecedor: acharCampo(l, ["fornecedor", "vencedor", "licitante"]) || "",
        situacaoTexto: situacao,
        status: traduzirSituacao(situacao),
      };
    }).filter((i) => i.desc || i.situacaoTexto);

    // Regra 9 do briefing: não inventar dado. Falta é falta, e é declarada.
    const faltando = [];
    if (!uasg) faltando.push("UASG");
    if (!numero) faltando.push("número do pregão");
    if (!itens.length) faltando.push("tabela de itens");

    return {
      ok: faltando.length === 0,
      faltando,
      uasg,
      numero: numero ? numero.replace(/\s/g, "") : null,
      ano: numero ? numero.split("/")[1]?.trim() : null,
      fase: fase ? fase.trim() : null,
      objeto: objeto ? objeto.trim() : null,
      itens,
      fonte: location.href,
      lidoEm: new Date().toISOString(),
    };
  }

  /* ------------------------------------------------------------------ *
   * 4. EXECUTOR — busca pedida pelo bot, feita dentro da sessão da página
   * ------------------------------------------------------------------ *
   * Aqui a requisição é same-origin de verdade: o navegador aplica os
   * cookies da sua sessão sem que a extensão precise lê-los ou copiá-los.
   */

  async function requisitar({ url, metodo = "GET", corpo = null, cabecalhos = null }) {
    // Só busca dentro dos domínios do portal — a extensão não vira proxy geral.
    const destino = new URL(url, location.origin);
    if (!/(gov\.br|serpro\.gov\.br|comprasnet\.gov\.br)$/i.test(destino.hostname)) {
      return { ok: false, erro: "dominio_nao_permitido", host: destino.hostname };
    }

    try {
      const resp = await fetch(destino.href, {
        method: metodo,
        credentials: "same-origin",
        headers: {
          Accept: "application/json, text/plain, */*",
          ...(corpo ? { "Content-Type": "application/json" } : {}),
          ...(cabecalhos || {}),
        },
        body: corpo ? JSON.stringify(corpo) : undefined,
        redirect: "follow",
      });

      const texto = await resp.text();

      // Redirecionado para o login = a sessão caiu. O bot precisa saber disso
      // explicitamente, em vez de receber o HTML da tela de login como "dado".
      if (RX_LOGIN_URL.test(resp.url) || /<input[^>]+type=["']password/i.test(texto)) {
        return { ok: false, erro: "sessao_expirada", url_final: resp.url };
      }

      let json = null;
      try { json = JSON.parse(texto); } catch { /* resposta não-JSON */ }

      return {
        ok: resp.ok,
        status: resp.status,
        url_final: resp.url,
        json,
        texto: json ? null : texto.slice(0, 2_000_000),
      };
    } catch (e) {
      return { ok: false, erro: "falha_rede", detalhe: String(e) };
    }
  }

  /* ------------------------------------------------------------------ *
   * 5. PONTE COM O POPUP / BACKGROUND
   * ------------------------------------------------------------------ */

  chrome.runtime.onMessage.addListener((msg, _rem, responder) => {
    try {
      if (msg?.tipo === "status") {
        responder({ ...avaliarSessao(), pagina: classificarPagina() });
      } else if (msg?.tipo === "analisar") {
        const sessao = avaliarSessao();
        if (!sessao.autenticado) {
          responder({ ok: false, erro: "sem_sessao" });
        } else {
          responder({ ok: true, sessao, dados: extrairPregao() });
        }
      } else if (msg?.tipo === "requisitar") {
        requisitar(msg).then(responder);
      } else {
        responder({ ok: false, erro: "mensagem_desconhecida" });
      }
    } catch (e) {
      LOG("falha", e);
      responder({ ok: false, erro: "excecao", detalhe: String(e) });
    }
    return true;
  });

  LOG("ativo em", location.host, classificarPagina());
})();
