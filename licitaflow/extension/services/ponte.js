/**
 * services/ponte.js — o elo entre o seu bot Python e a sessão do navegador.
 *
 * A ideia: o bot deixa de ter sessão própria. Ele pede uma URL, a extensão
 * busca essa URL DENTRO da aba já logada e devolve a resposta. Não existe
 * segunda sessão para manter viva, expirar ou salvar em disco — existe a
 * sua, a que você abriu de manhã.
 *
 * A busca roda no content script, não aqui no service worker, e isso é
 * proposital: só dentro da página a requisição é mesmo same-origin, e o
 * navegador aplica os cookies exatamente como aplicaria se você tivesse
 * clicado. Requisição saindo do service worker tem a extensão como origem
 * e cookie SameSite=Strict fica de fora — é a origem de "funciona no
 * navegador e falha no bot" que você já deve ter visto.
 */

const PADROES_ABA = [
  "https://www.gov.br/compras/*",
  "https://cnetmobile.estaleiro.serpro.gov.br/*",
  "https://contratos.comprasnet.gov.br/*",
  "https://www.comprasnet.gov.br/*",
];

let socket = null;
let tentativa = 0;
let stats = { atendidos: 0, erros: 0, ultimo: null };

const log = (...a) => console.debug("[ponte]", ...a);

/* ---------------------------------------------------------------- *
 * Aba de serviço
 * ---------------------------------------------------------------- */

async function abaDoPortal(origemAlvo) {
  const abas = await chrome.tabs.query({ url: PADROES_ABA });

  // Prioridade 1: uma aba que já está na MESMA ORIGEM da URL pedida — é a
  // única em que o fetch "same-origin" do content script funciona de fato.
  if (origemAlvo) {
    const mesmaOrigem = abas.filter((a) => {
      try { return new URL(a.url).origin === origemAlvo; } catch { return false; }
    });
    const completa = mesmaOrigem.find((a) => a.status === "complete");
    if (completa) return completa;
    if (mesmaOrigem.length) return mesmaOrigem[0];
  }

  // Sem aba na origem certa: cai para qualquer aba do portal (pode falhar
  // por CORS se a origem for diferente da pedida).
  return abas.find((a) => a.status === "complete") || abas[0] || null;
}

async function garantirAba(origemAlvo) {
  let aba = await abaDoPortal(origemAlvo);
  if (aba) return aba;

  const { abrirAbaServico } = await chrome.storage.local
    .get("abrirAbaServico").then((r) => ({ abrirAbaServico: r.abrirAbaServico ?? true }));
  if (!abrirAbaServico) return null;

  aba = await chrome.tabs.create({
    url: origemAlvo ? `${origemAlvo}/` : "https://www.gov.br/compras/pt-br",
    pinned: true,
    active: false,
  });
  // Espera o content script subir.
  await new Promise((r) => setTimeout(r, 3000));
  return aba;
}

/* ---------------------------------------------------------------- *
 * Execução do pedido
 * ---------------------------------------------------------------- */

async function executar(pedido) {
  let origemAlvo = null;
  try { origemAlvo = new URL(pedido.url).origin; } catch { /* URL inválida, segue sem filtro */ }

  const aba = await garantirAba(origemAlvo);
  if (!aba) {
    return { ok: false, erro: "sem_aba",
             detalhe: "Nenhuma aba do Compras.gov.br aberta." };
  }

  try {
    const r = await chrome.tabs.sendMessage(aba.id, {
      tipo: "requisitar",
      url: pedido.url,
      metodo: pedido.metodo || "GET",
      corpo: pedido.corpo || null,
      cabecalhos: pedido.cabecalhos || null,
    });
    return r || { ok: false, erro: "sem_resposta" };
  } catch (e) {
    return { ok: false, erro: "content_script_ausente", detalhe: String(e) };
  }
}

/* ---------------------------------------------------------------- *
 * WebSocket
 * ---------------------------------------------------------------- */

export async function conectar() {
  // Padrão é ligada: um perfil novo do Chrome não deve exigir que você
  // abra o popup e clique em "Ligar" antes de o bot conseguir usar a sessão.
  // Se você desligar pelo popup, o valor explícito "false" fica salvo e
  // este default deixa de valer — a escolha do usuário sempre vence.
  const { ponteUrl = "ws://localhost:8765/ws", ponteAtiva = true } =
    await chrome.storage.local.get(["ponteUrl", "ponteAtiva"]);

  if (!ponteAtiva) return;
  if (socket && (socket.readyState === WebSocket.OPEN ||
                 socket.readyState === WebSocket.CONNECTING)) return;

  log("conectando em", ponteUrl);
  socket = new WebSocket(ponteUrl);

  socket.onopen = () => {
    tentativa = 0;
    log("conectada");
    socket.send(JSON.stringify({ tipo: "ola", versao: chrome.runtime.getManifest().version }));
    chrome.storage.local.set({ ponteEstado: "conectada" });
  };

  socket.onmessage = async (ev) => {
    let pedido;
    try {
      pedido = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (pedido.tipo === "ping") {
      socket.send(JSON.stringify({ tipo: "pong" }));
      return;
    }
    if (pedido.tipo !== "fetch") return;

    const res = await executar(pedido);
    res.ok ? stats.atendidos++ : stats.erros++;
    stats.ultimo = { url: pedido.url, ok: res.ok, em: new Date().toISOString() };
    chrome.storage.local.set({ ponteStats: stats });

    socket.send(JSON.stringify({ id: pedido.id, ...res }));
  };

  socket.onclose = () => {
    chrome.storage.local.set({ ponteEstado: "desconectada" });
    const espera = Math.min(30000, 1000 * 2 ** tentativa++);
    log("caiu; nova tentativa em", espera, "ms");
    setTimeout(conectar, espera);
  };

  socket.onerror = () => socket && socket.close();
}

export function desconectar() {
  tentativa = 0;
  if (socket) { socket.onclose = null; socket.close(); socket = null; }
  chrome.storage.local.set({ ponteEstado: "desligada" });
}

export function estado() {
  if (!socket) return "desligada";
  return socket.readyState === WebSocket.OPEN ? "conectada" : "conectando";
}

export function estatisticas() {
  return stats;
}
