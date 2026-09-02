"""
fila_ata.py - Etapa 2.11: transforma o resultado do motor de homologação
(homologacao.py, Etapa 2.10) numa FILA DE ATA — a lista de itens/grupos
elegíveis, pendentes, não elegíveis e sem fornecedor.

NÃO gera Ata. NÃO cria regra de homologação nova. NÃO recalcula
classificação -- só reorganiza o que o motor já decidiu, camada pura, sem
I/O nenhum (sem tocar Chrome, dados.db, api.py).

Grupo é tratado como UMA unidade na fila (nunca um item-filho por si só).
Item normal com múltiplos fornecedores gera uma entrada por (item,
fornecedor) -- nenhuma relação é sobrescrita.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from homologacao import Classificacao, FornecedorAvaliado


class Disponibilidade:
    ELEGIVEL = "ELEGIVEL"
    PENDENTE = "PENDENTE"
    NAO_ELEGIVEL = "NAO_ELEGIVEL"
    SEM_FORNECEDOR = "SEM_FORNECEDOR"


def _disponibilidade(fornecedor: str | None, elegivel: bool, classe: Classificacao) -> str:
    """Só reflete o que o motor já decidiu -- não inventa critério novo."""
    if not fornecedor:
        return Disponibilidade.SEM_FORNECEDOR
    if elegivel:
        return Disponibilidade.ELEGIVEL
    if classe in (Classificacao.PENDENTE, Classificacao.SEM_SITUACAO):
        return Disponibilidade.PENDENTE
    return Disponibilidade.NAO_ELEGIVEL  # ANULADO, FRACASSADO, DESCONHECIDA


@dataclass
class ItemFila:
    pregao: str
    uasg: str
    tipo: str  # "item_normal" ou "grupo"
    identificador: str  # numero do item OU "GRUPO N"
    descricao: str | None
    fornecedor: str | None
    cnpj: str | None
    situacao_original: str | None
    classificacao: str  # Classificacao.value
    elegivel: bool
    disponibilidade: str  # Disponibilidade.*
    data_captura: str
    itens_filhos: list[dict] = field(default_factory=list)  # só preenchido pra tipo="grupo"


def construir_fila(pregao: str, uasg: str, avaliacoes: list[FornecedorAvaliado]) -> list[ItemFila]:
    """Uma entrada por (item, fornecedor) pra item normal -- preserva
    múltiplos fornecedores por item. Uma entrada por GRUPO (nunca por
    item-filho)."""
    agora = datetime.now(timezone.utc).isoformat()
    fila: list[ItemFila] = []

    for a in avaliacoes:
        if a.tipo == "grupo":
            fila.append(ItemFila(
                pregao=pregao, uasg=uasg, tipo="grupo", identificador=a.identificador,
                descricao=f"{len(a.itens)} item(ns) na composição",
                fornecedor=a.fornecedor, cnpj=a.cnpj,
                situacao_original=a.situacao_texto,
                classificacao=(a.situacao_classe or Classificacao.SEM_SITUACAO).value,
                elegivel=a.apto,
                disponibilidade=_disponibilidade(a.fornecedor, a.apto, a.situacao_classe or Classificacao.SEM_SITUACAO),
                data_captura=agora,
                itens_filhos=[{"numero": i.numero, "descricao": i.descricao} for i in a.itens],
            ))
        else:
            # item_normal (1 item) e item_normal_multi (N itens) caem aqui
            # igual -- cada ItemAvaliado dentro de a.itens vira UMA entrada.
            for item in a.itens:
                fila.append(ItemFila(
                    pregao=pregao, uasg=uasg, tipo="item_normal", identificador=item.numero,
                    descricao=item.descricao, fornecedor=a.fornecedor, cnpj=a.cnpj,
                    situacao_original=item.situacao_texto,
                    classificacao=item.situacao_classe.value,
                    elegivel=item.elegivel,
                    disponibilidade=_disponibilidade(a.fornecedor, item.elegivel, item.situacao_classe),
                    data_captura=agora,
                ))

    return fila


def resumo_fila(fila: list[ItemFila]) -> dict:
    por_disp = {Disponibilidade.ELEGIVEL: 0, Disponibilidade.PENDENTE: 0,
                Disponibilidade.NAO_ELEGIVEL: 0, Disponibilidade.SEM_FORNECEDOR: 0}
    for f in fila:
        por_disp[f.disponibilidade] = por_disp.get(f.disponibilidade, 0) + 1

    por_fornecedor: dict[str, dict] = {}
    for f in fila:
        if not f.fornecedor:
            continue
        chave = f"{f.fornecedor} ({f.cnpj})"
        d = por_fornecedor.setdefault(chave, {
            "fornecedor": f.fornecedor, "cnpj": f.cnpj,
            "total": 0, "elegiveis": 0, "pendentes": 0, "nao_elegiveis": 0,
        })
        d["total"] += 1
        if f.disponibilidade == Disponibilidade.ELEGIVEL:
            d["elegiveis"] += 1
        elif f.disponibilidade == Disponibilidade.PENDENTE:
            d["pendentes"] += 1
        elif f.disponibilidade == Disponibilidade.NAO_ELEGIVEL:
            d["nao_elegiveis"] += 1

    grupos = [f for f in fila if f.tipo == "grupo"]

    return {
        "total": len(fila),
        "por_disponibilidade": por_disp,
        "por_fornecedor": por_fornecedor,
        "total_grupos": len(grupos),
    }


def ata_parcial_disponivel(fila: list[ItemFila], fornecedor: str, cnpj: str) -> dict:
    """Não gera Ata -- só calcula, pra um fornecedor específico, quais
    itens JÁ estão elegíveis agora (uma futura 'Ata parcial' usaria
    exatamente essa lista, sem esperar os pendentes)."""
    itens_forn = [f for f in fila if f.fornecedor == fornecedor and f.cnpj == cnpj]
    elegiveis = [f for f in itens_forn if f.disponibilidade == Disponibilidade.ELEGIVEL]
    pendentes = [f for f in itens_forn if f.disponibilidade == Disponibilidade.PENDENTE]
    nao_elegiveis = [f for f in itens_forn if f.disponibilidade == Disponibilidade.NAO_ELEGIVEL]

    return {
        "fornecedor": fornecedor, "cnpj": cnpj,
        "total": len(itens_forn),
        "elegiveis_para_ata_parcial": [f.identificador for f in elegiveis],
        "pendentes": [f.identificador for f in pendentes],
        "nao_elegiveis": [f.identificador for f in nao_elegiveis],
        "ata_completa_disponivel": len(pendentes) == 0 and len(nao_elegiveis) == 0 and len(elegiveis) > 0,
        "ata_parcial_disponivel": len(elegiveis) > 0,
    }
