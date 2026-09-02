/**
 * modules/homologacao.js — a regra de negócio do produto, isolada da UI.
 *
 * Existe uma regra só, e é ela que justifica a extensão inteira:
 * a ata não sai enquanto UM item do fornecedor acompanhado estiver pendente.
 */

export const ESTADOS = {
  BLOQUEADA: "bloqueada",
  LIBERADA: "liberada",
  SEM_DADOS: "sem_dados",
};

/** Situação da ata para um pregão já extraído. */
export function avaliar(dados, fornecedorAcompanhado = null) {
  if (!dados?.itens?.length) {
    return { estado: ESTADOS.SEM_DADOS, total: 0, homologados: 0, pendentes: [] };
  }

  const itens = fornecedorAcompanhado
    ? dados.itens.filter((i) => (i.fornecedor || "").toLowerCase()
        .includes(fornecedorAcompanhado.toLowerCase()))
    : dados.itens;

  const homologados = itens.filter((i) => i.status === "ok");
  const pendentes = itens.filter((i) => i.status !== "ok");

  return {
    estado: pendentes.length ? ESTADOS.BLOQUEADA : ESTADOS.LIBERADA,
    total: itens.length,
    homologados: homologados.length,
    pendentes: pendentes.map((i) => i.n).sort((a, b) => a - b),
    percentual: Math.round((homologados.length / itens.length) * 100),
    fornecedor: fornecedorAcompanhado,
  };
}

/**
 * Compara a leitura de agora com a anterior e devolve só o que mudou.
 * É esta função que alimenta a memória/histórico do briefing.
 */
export function diferenca(anterior, atual) {
  if (!anterior?.itens?.length) return { primeira: true, mudancas: [] };

  const antes = new Map(anterior.itens.map((i) => [i.n, i.status]));
  const mudancas = [];

  for (const i of atual.itens) {
    const de = antes.get(i.n);
    if (de && de !== i.status) {
      mudancas.push({ item: i.n, de, para: i.status, desc: i.desc });
    }
  }
  return { primeira: false, mudancas };
}

export function resumo(av) {
  if (av.estado === ESTADOS.SEM_DADOS) return "Sem itens legíveis nesta página.";
  if (av.estado === ESTADOS.LIBERADA) return `${av.total}/${av.total} itens homologados.`;
  return `${av.homologados}/${av.total} homologados — pendentes: ${av.pendentes.join(", ")}.`;
}
