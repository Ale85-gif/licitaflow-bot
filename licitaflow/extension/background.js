/**
 * background.js — service worker (MV3, módulo ES).
 *
 * Não toca na página. Ele coordena: fala com o content script, guarda a
 * leitura, calcula o que mudou desde a última vez e pinta o badge do ícone.
 */

import * as api from "./services/api.js";
import * as ponte from "./services/ponte.js";
import { avaliar, diferenca, ESTADOS } from "./modules/homologacao.js";

// Clicar no ícone da barra de ferramentas abre o painel lateral (fixo, na
// mesma janela do navegador) em vez de um popup que fecha sozinho.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch((e) => console.error(e));

const DOMINIOS = ["gov.br/compras", "cnetmobile.estaleiro.serpro.gov.br", "contratos.comprasnet.gov.br", "comprasnet.gov.br"];

const ehComprasnet = (url = "") => DOMINIOS.some((d) => url.includes(d));
const chaveDe = (d) => `${d.uasg || "?"}:${d.numero || "?"}`;

/* ---------------------------------------------------------------- */

async function perguntarAoConteudo(tabId, mensagem) {
  try {
    return await chrome.tabs.sendMessage(tabId, mensagem);
  } catch {
    // Content script ainda não carregou nesta aba (ou a página não é do portal).
    return null;
  }
}

async function statusDaAba(tabId) {
  const aba = await chrome.tabs.get(tabId).catch(() => null);
  if (!aba || !ehComprasnet(aba.url || "")) {
    return { situacao: "fora", url: aba?.url || "" };
  }

  const r = await perguntarAoConteudo(tabId, { tipo: "status" });
  if (!r) return { situacao: "carregando", url: aba.url };
  if (!r.autenticado) return { situacao: "sem_login", url: aba.url, pagina: r.pagina };
  return { situacao: "autenticado", url: aba.url, pagina: r.pagina };
}

async function pintarBadge(tabId, situacao) {
  const cores = { autenticado: "#4C6A3B", sem_login: "#95301F", carregando: "#8A5A00" };
  const textos = { autenticado: "on", sem_login: "off", carregando: "..." };
  await chrome.action.setBadgeText({ tabId, text: textos[situacao] || "" });
  if (cores[situacao]) {
    await chrome.action.setBadgeBackgroundColor({ tabId, color: cores[situacao] });
  }
}

/* ---------------------------------------------------------------- *
 * Análise
 * ---------------------------------------------------------------- */

async function analisar(tabId) {
  const r = await perguntarAoConteudo(tabId, { tipo: "analisar" });
  if (!r) return { ok: false, erro: "sem_content_script" };
  if (!r.ok) return r;

  const dados = r.dados;
  if (!dados.ok) {
    return { ok: false, erro: "dados_incompletos", faltando: dados.faltando, dados };
  }

  const chave = chaveDe(dados);
  const cfg = await api.config();
  const av = avaliar(dados, cfg.fornecedorAcompanhado || null);

  // Memória: compara com a leitura anterior antes de gravar a nova.
  const anterior = await chrome.storage.local
    .get(`snap:${chave}`).then((s) => s[`snap:${chave}`] || null);
  const dif = diferenca(anterior, dados);

  await chrome.storage.local.set({ [`snap:${chave}`]: dados });
  await api.registrar(chave, av);

  // Envio ao backend só acontece se você configurou a URL.
  await api.enviar(api.ROTAS.homologacoes, {
    uasg: dados.uasg, pregao: dados.numero, avaliacao: av,
    itens: dados.itens, lidoEm: dados.lidoEm,
  });

  return { ok: true, chave, dados, avaliacao: av, diferenca: dif,
           liberada: av.estado === ESTADOS.LIBERADA };
}

/* ---------------------------------------------------------------- *
 * Eventos
 * ---------------------------------------------------------------- */

chrome.runtime.onMessage.addListener((msg, _rem, responder) => {
  (async () => {
    const [aba] = await chrome.tabs.query({ active: true, currentWindow: true });

    if (msg.tipo === "status") {
      const s = aba ? await statusDaAba(aba.id) : { situacao: "fora" };
      if (aba) await pintarBadge(aba.id, s.situacao);
      responder({ ...s, tabId: aba?.id });
    } else if (msg.tipo === "analisar") {
      responder(aba ? await analisar(aba.id) : { ok: false, erro: "sem_aba" });
    } else if (msg.tipo === "historico") {
      responder(await api.historico(msg.chave));
    } else if (msg.tipo === "ponte") {
      // liga/desliga a ponte com o bot
      if (msg.acao === "ligar") {
        await chrome.storage.local.set({ ponteAtiva: true, ponteUrl: msg.url });
        await ponte.conectar();
      } else if (msg.acao === "desligar") {
        await chrome.storage.local.set({ ponteAtiva: false });
        ponte.desconectar();
      }
      responder({ estado: ponte.estado(), stats: ponte.estatisticas() });
    } else {
      responder({ ok: false, erro: "desconhecido" });
    }
  })();
  return true;
});

chrome.tabs.onUpdated.addListener(async (tabId, info, aba) => {
  if (info.status !== "complete" || !ehComprasnet(aba.url || "")) return;
  const s = await statusDaAba(tabId);
  await pintarBadge(tabId, s.situacao);
});

chrome.runtime.onInstalled.addListener(async () => {
  console.debug("[LicitaFlow] instalado. Backend não configurado — modo local.");

  // Grava o padrão explicitamente, para não depender de defaults espalhados
  // em cada arquivo. Só entra aqui na primeira instalação neste perfil —
  // se você já desligou a ponte antes, "ponteAtiva" já existe e não é tocado.
  const atual = await chrome.storage.local.get("ponteAtiva");
  if (atual.ponteAtiva === undefined) {
    await chrome.storage.local.set({ ponteAtiva: true, ponteUrl: "ws://localhost:8765/ws" });
  }

  chrome.alarms.create("ponte", { periodInMinutes: 0.5 });
  ponte.conectar();
});

/* ---------------------------------------------------------------- *
 * Manter a ponte de pé
 * ---------------------------------------------------------------- *
 * Service worker MV3 é encerrado quando fica ocioso. Mensagem de
 * WebSocket já renova esse prazo, mas em período sem tráfego o alarme
 * de 30s acorda o worker e reconecta se necessário. É por isso que a
 * ponte sobrevive a uma tarde inteira sem você tocar em nada.
 */
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "ponte") ponte.conectar();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("ponte", { periodInMinutes: 0.5 });
  ponte.conectar();
});

ponte.conectar();
