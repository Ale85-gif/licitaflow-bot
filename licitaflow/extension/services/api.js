/**
 * services/api.js — camada de saída para o backend do LicitaFlow.
 *
 * Enquanto não houver backend, tudo funciona local: cada leitura vai para
 * chrome.storage.local e a fila de envio se acumula. Quando você configurar
 * a URL da API, a fila é drenada. Nenhuma URL inventada, nenhum envio às
 * escondidas — sem configuração, nada sai do navegador.
 */

const CHAVE_CONFIG = "licitaflow:config";
const CHAVE_FILA = "licitaflow:fila";
const CHAVE_HIST = "licitaflow:historico";

export const ROTAS = {
  pregoes: "/api/pregoes",
  itens: "/api/itens",
  fornecedores: "/api/fornecedores",
  homologacoes: "/api/homologacoes",
  status: (id) => `/api/pregoes/${id}/status`,
};

export async function config() {
  const r = await chrome.storage.local.get(CHAVE_CONFIG);
  return { baseUrl: "", token: "", uasgPadrao: "", ...(r[CHAVE_CONFIG] || {}) };
}

export async function salvarConfig(novo) {
  const atual = await config();
  await chrome.storage.local.set({ [CHAVE_CONFIG]: { ...atual, ...novo } });
}

export async function configurada() {
  return !!(await config()).baseUrl;
}

/* ---------------------------------------------------------------- *
 * Histórico local — a memória do briefing, funcionando sem backend
 * ---------------------------------------------------------------- */

export async function registrar(chave, snapshot) {
  const r = await chrome.storage.local.get(CHAVE_HIST);
  const hist = r[CHAVE_HIST] || {};
  const linha = hist[chave] || [];

  linha.push({
    em: new Date().toISOString(),
    homologados: snapshot.homologados,
    total: snapshot.total,
    pendentes: snapshot.pendentes,
  });

  hist[chave] = linha.slice(-60);          // dois meses de leituras diárias
  await chrome.storage.local.set({ [CHAVE_HIST]: hist });
  return hist[chave];
}

export async function historico(chave) {
  const r = await chrome.storage.local.get(CHAVE_HIST);
  return (r[CHAVE_HIST] || {})[chave] || [];
}

export async function ultimaLeitura(chave) {
  const linha = await historico(chave);
  return linha[linha.length - 1] || null;
}

/* ---------------------------------------------------------------- *
 * Envio
 * ---------------------------------------------------------------- */

async function enfileirar(rota, corpo) {
  const r = await chrome.storage.local.get(CHAVE_FILA);
  const fila = r[CHAVE_FILA] || [];
  fila.push({ rota, corpo, em: Date.now() });
  await chrome.storage.local.set({ [CHAVE_FILA]: fila.slice(-200) });
}

export async function enviar(rota, corpo) {
  const { baseUrl, token } = await config();
  if (!baseUrl) {
    await enfileirar(rota, corpo);
    return { ok: false, motivo: "sem_backend", enfileirado: true };
  }

  try {
    const resp = await fetch(baseUrl.replace(/\/$/, "") + rota, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(corpo),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return { ok: true, dados: await resp.json().catch(() => ({})) };
  } catch (e) {
    await enfileirar(rota, corpo);
    return { ok: false, motivo: String(e), enfileirado: true };
  }
}

export async function drenarFila() {
  if (!(await configurada())) return { enviados: 0 };
  const r = await chrome.storage.local.get(CHAVE_FILA);
  const fila = r[CHAVE_FILA] || [];
  const restante = [];
  let enviados = 0;

  for (const p of fila) {
    const res = await enviar(p.rota, p.corpo);
    res.ok ? enviados++ : restante.push(p);
  }
  await chrome.storage.local.set({ [CHAVE_FILA]: restante });
  return { enviados, pendentes: restante.length };
}

export async function tamanhoFila() {
  const r = await chrome.storage.local.get(CHAVE_FILA);
  return (r[CHAVE_FILA] || []).length;
}
