/**
 * sidepanel.js — a mesma interface do popup, só que fixa: fica aberta na
 * janela do navegador enquanto você navega, em vez de fechar a cada clique
 * fora dela. Por isso, diferente do popup, ela também escuta troca e
 * atualização de aba para se manter em dia sozinha.
 */

import * as api from "./services/api.js";

const $ = (s) => document.querySelector(s);
const URL_PORTAL = "https://www.comprasnet.gov.br/seguro/loginPortal.asp";

const ESTADOS = {
  fora: {
    classe: "", titulo: "Fora do Compras.gov.br",
    texto: "Abra o portal para começar.", analisar: false,
  },
  carregando: {
    classe: "aviso", titulo: "Aguardando a página",
    texto: "A página ainda está carregando.", analisar: false,
  },
  sem_login: {
    classe: "erro", titulo: "🔴 Compras.gov.br não conectado",
    texto: "Abra o Compras.gov.br e faça o login normalmente.", analisar: false,
  },
  sessao_perdida: {
    classe: "erro", titulo: "🔴 Sessão perdida",
    texto: "A sessão caiu — faça login novamente no Compras.gov.br.", analisar: false,
  },
  autenticado: {
    classe: "ok", titulo: "🟢 Sessão detectada",
    texto: "Você pode iniciar a análise do pregão.", analisar: true,
  },
};

let ultimoStatus = null;

// Diferencia "nunca logou nesta navegação" de "estava conectado e a sessão
// caiu" — são avisos diferentes para o usuário, mesmo sinal técnico.
let estavaConectado = false;

/* ---------------------------------------------------------------- */

function pintarEstado(s) {
  let situacao = s.situacao;
  if (situacao === "sem_login" && estavaConectado) situacao = "sessao_perdida";
  if (situacao === "autenticado") estavaConectado = true;
  if (situacao === "fora") estavaConectado = false;

  const e = ESTADOS[situacao] || ESTADOS.fora;
  $("#estado").className = "estado " + e.classe;
  $("#estadoTitulo").textContent = e.titulo;

  let texto = e.texto;
  if (s.situacao === "autenticado" && s.pagina === "desconhecida") {
    $("#estado").className = "estado aviso";
    $("#estadoTitulo").textContent = "Página não identificada";
    texto = "Abra um pregão no Compras.gov.br para iniciar a análise.";
  }
  $("#estadoTexto").textContent = texto;
  $("#btnAnalisar").disabled = !e.analisar;
}

async function verificar() {
  const s = await chrome.runtime.sendMessage({ tipo: "status" });
  ultimoStatus = s;
  pintarEstado(s);

  const n = await api.tamanhoFila();
  const cfg = await api.config();
  $("#rodape").firstChild.textContent = cfg.baseUrl
    ? `Enviando para ${cfg.baseUrl}. `
    : "Nenhum dado enviado. ";
  $("#filaInfo").textContent = n ? `${n} leituras na fila local.` : "";
}

/* ---------------------------------------------------------------- */

function desenharResultado(r) {
  const box = $("#resultado");
  box.hidden = false;

  if (!r.ok) {
    const motivos = {
      sem_sessao: "A sessão não está mais ativa nesta aba.",
      sem_content_script: "Recarregue a página do Compras.gov.br e tente de novo.",
      dados_incompletos: `Não encontrei nesta página: ${(r.faltando || []).join(", ")}.`,
    };
    box.innerHTML = `<div class="trava bloqueada"><b>Não foi possível analisar.</b><br>
      ${motivos[r.erro] || r.erro}</div>`;
    return;
  }

  const { dados, avaliacao: a, diferenca: d } = r;

  $("#contexto").hidden = false;
  $("#contexto").innerHTML = `
    <div><span>UASG</span><b>${dados.uasg}</b></div>
    <div><span>Pregão</span><b>${dados.numero}</b></div>
    <div><span>Objeto</span><b>${dados.objeto || "Informação não disponível"}</b></div>
    <div><span>Fase</span><b>${dados.fase || "Informação não disponível"}</b></div>
    <div><span>Itens</span><b>${dados.itens.length}</b></div>`;

  const chips = dados.itens
    .map((i) => `<span class="chip ${i.status}" title="Item ${i.n}: ${i.situacaoTexto || "—"}">${i.n}</span>`)
    .join("");

  const trava = a.estado === "liberada"
    ? `<div class="trava liberada"><b>ATA LIBERADA</b><br>${a.total}/${a.total} itens homologados.</div>`
    : `<div class="trava bloqueada"><b>ATA BLOQUEADA</b><br>
         Pendentes de homologação: ${a.pendentes.join(", ")}.</div>`;

  const mudancas = d?.mudancas?.length
    ? `<div class="mudou">Desde a última leitura: ${d.mudancas
        .map((m) => `item ${m.item} → ${m.para === "ok" ? "homologado" : m.para}`).join(", ")}.</div>`
    : d?.primeira ? `<div class="mudou">Primeira leitura deste pregão.</div>` : "";

  box.innerHTML = `
    <b>${a.homologados}/${a.total} itens homologados</b>
    <div class="barra"><i style="width:${a.percentual}%"></i></div>
    <div class="chips">${chips}</div>
    ${trava}${mudancas}`;
}

/* ---------------------------------------------------------------- *
 * Ponte com o bot
 * ---------------------------------------------------------------- */

const TEXTO_PONTE = {
  conectada: ["on", "Conectada", "O bot está usando a sua sessão."],
  conectando: ["esperando", "Conectando...", "Procurando o servidor da ponte."],
  desconectada: ["esperando", "Servidor fora do ar", "Rode: uvicorn ponte:app --port 8765"],
  desligada: ["", "Ponte com o bot", "Desligada. O bot não consegue usar sua sessão."],
};

async function pintarPonte() {
  const { ponteEstado = "desligada", ponteAtiva = true, ponteStats } =
    await chrome.storage.local.get(["ponteEstado", "ponteAtiva", "ponteStats"]);

  const estado = ponteAtiva ? ponteEstado : "desligada";
  const [classe, titulo, texto] = TEXTO_PONTE[estado] || TEXTO_PONTE.desligada;

  $("#ponte").className = "ponte " + classe;
  $("#ponteTitulo").textContent = titulo;
  $("#btnPonte").textContent = ponteAtiva ? "Desligar" : "Ligar";

  const extra = ponteStats?.atendidos
    ? ` ${ponteStats.atendidos} pedidos atendidos.`
    : "";
  $("#ponteTexto").textContent = texto + extra;
}

$("#btnPonte").addEventListener("click", async () => {
  const { ponteAtiva = true, ponteUrl = "ws://localhost:8765/ws" } =
    await chrome.storage.local.get(["ponteAtiva", "ponteUrl"]);

  await chrome.runtime.sendMessage({
    tipo: "ponte",
    acao: ponteAtiva ? "desligar" : "ligar",
    url: ponteUrl,
  });
  setTimeout(pintarPonte, 600);
});

/* ---------------------------------------------------------------- */

$("#btnAbrir").addEventListener("click", () => {
  chrome.tabs.create({ url: URL_PORTAL });
});

$("#btnAtualizar").addEventListener("click", verificar);

$("#btnAnalisar").addEventListener("click", async () => {
  const b = $("#btnAnalisar");
  b.disabled = true;
  b.textContent = "Analisando...";
  const r = await chrome.runtime.sendMessage({ tipo: "analisar" });
  desenharResultado(r);
  b.textContent = "Analisar pregão";
  b.disabled = false;
});

$("#btnDashboard").addEventListener("click", async () => {
  const { baseUrl } = await api.config();
  chrome.tabs.create({ url: baseUrl || "http://localhost:8000" });
});

$("#btnConfig").addEventListener("click", async () => {
  const c = $("#config");
  c.hidden = !c.hidden;
  if (!c.hidden) {
    const cfg = await api.config();
    const { ponteUrl = "ws://localhost:8765/ws" } =
      await chrome.storage.local.get("ponteUrl");
    $("#cfgUrl").value = cfg.baseUrl || "";
    $("#cfgFornecedor").value = cfg.fornecedorAcompanhado || "";
    $("#cfgPonte").value = ponteUrl;
  }
});

$("#btnSalvarConfig").addEventListener("click", async () => {
  await api.salvarConfig({
    baseUrl: $("#cfgUrl").value.trim(),
    fornecedorAcompanhado: $("#cfgFornecedor").value.trim(),
  });
  await chrome.storage.local.set({ ponteUrl: $("#cfgPonte").value.trim() });
  await api.drenarFila();
  $("#config").hidden = true;
  verificar();
  pintarPonte();
});

/* ---------------------------------------------------------------- *
 * O painel fica aberto entre trocas de aba — diferente do popup, que
 * nasce de novo a cada clique. Sem isto, ele mostraria o status da aba
 * de quando foi aberto, mesmo depois de você já ter ido para outra.
 * ---------------------------------------------------------------- */
chrome.tabs.onActivated.addListener(() => verificar());
chrome.tabs.onUpdated.addListener((_tabId, info, aba) => {
  if (info.status === "complete" && aba.active) verificar();
});

verificar();
pintarPonte();
setInterval(pintarPonte, 3000);
