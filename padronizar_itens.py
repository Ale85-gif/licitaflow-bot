"""
padronizar_itens.py - Etapa 2.7: organiza e padroniza os dados de
item/fornecedor/CNPJ/situação já existentes no dados.db, sem criar tabela
nova e sem inventar nada.

Fontes analisadas (ver relatório da Etapa 2.7):
  - pregoes_itens: 1605 itens. Fornecedor vem como "CNPJ - Nome" (mesmo
    formato de BD_CONSOLIDADO na planilha, é o espelho dela). Sem coluna
    de situação do item.
  - participacao_itens: 428 itens. Já tem "CNPJ Fornecedor" separado do
    nome. Tem "Situação Ata" (da ATA, não do item -- não confundir).

Nenhuma das duas tabelas tem uma coluna real de "situação do item"
(Homologado/Pendente/etc). Por isso `situacao` sai sempre None aqui --
não é bug, é o estado real dos dados hoje. Ver Etapa 2.8 pra decidir como
capturar isso de verdade (repetindo o padrão da Etapa 2.5: da página real
do Compras.gov.br, nunca deduzido).

Não mexe em nenhuma tabela existente, não cria tabela nova, não altera
api.py nem os bots. Só lê e devolve estruturas padronizadas em memória.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from comum import DB_PATH

# Mesma regra de api.py::_separar_documento_nome -- reaproveitada aqui como
# função própria pra não precisar alterar api.py nesta etapa (que é só de
# organização de dados, não de interface).
RX_ID_PREFIXO = re.compile(r"^[\d./-]{6,20}\s*-?\s*")


def separar_fornecedor_cnpj(bruto: str) -> tuple[str, str]:
    """Recebe 'CNPJ - Razão Social' (formato de pregoes_itens/BD_CONSOLIDADO)
    e devolve (fornecedor, cnpj) -- nome limpo, CNPJ só dígitos ("" se não
    achar). Repete a limpeza até estabilizar (alguns cadastros do portal
    repetem o número dentro do próprio nome)."""
    bruto = (bruto or "").strip()
    m = RX_ID_PREFIXO.match(bruto)
    cnpj = re.sub(r"\D", "", m.group(0)) if m else ""

    nome = bruto
    anterior = None
    while nome != anterior:
        anterior = nome
        nome = RX_ID_PREFIXO.sub("", nome).strip()

    return nome, cnpj


@dataclass
class ItemPadronizado:
    pregao: str
    item: str
    descricao: str
    fornecedor: str
    cnpj: str
    situacao: str | None
    fase_pregao: str | None = None  # preenchido só se pedido explicitamente (nunca == situacao)
    fonte: str = ""


def _conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def itens_de_pregoes_itens() -> list[ItemPadronizado]:
    """pregoes_itens: Fornecedor vem embutido ('CNPJ - Nome') -- separa aqui."""
    conn = _conectar()
    try:
        cur = conn.execute(
            'SELECT "Pregão Origem", "Número Item", "Descrição", "Fornecedor" FROM pregoes_itens'
        )
        out = []
        for r in cur.fetchall():
            fornecedor, cnpj = separar_fornecedor_cnpj(r["Fornecedor"])
            out.append(ItemPadronizado(
                pregao=r["Pregão Origem"],
                item=r["Número Item"],
                descricao=r["Descrição"],
                fornecedor=fornecedor,
                cnpj=cnpj,
                situacao=None,  # não existe essa informação nesta tabela
                fonte="pregoes_itens",
            ))
        return out
    finally:
        conn.close()


def itens_de_participacao() -> list[ItemPadronizado]:
    """participacao_itens: CNPJ já vem separado -- só reorganiza pro mesmo formato."""
    conn = _conectar()
    try:
        cur = conn.execute(
            'SELECT "Compra", "Número Item", "Descrição Item", '
            '"Fornecedor (Classificação)", "CNPJ Fornecedor" FROM participacao_itens'
        )
        out = []
        for r in cur.fetchall():
            out.append(ItemPadronizado(
                pregao=r["Compra"],
                item=r["Número Item"],
                descricao=r["Descrição Item"],
                fornecedor=r["Fornecedor (Classificação)"],
                cnpj=re.sub(r"\D", "", r["CNPJ Fornecedor"] or ""),
                situacao=None,  # "Situação Ata" existe, mas é da ATA -- não é situação do item
                fonte="participacao_itens",
            ))
        return out
    finally:
        conn.close()


def todos_itens_padronizados() -> list[ItemPadronizado]:
    return itens_de_pregoes_itens() + itens_de_participacao()


def agrupar_por_fornecedor(itens: list[ItemPadronizado]) -> dict[str, dict]:
    """{cnpj_ou_'sem-cnpj': {"nome":..., "itens": [ItemPadronizado, ...]}}"""
    grupos: dict[str, dict] = {}
    for it in itens:
        chave = it.cnpj or "sem-cnpj"
        if chave not in grupos:
            grupos[chave] = {"nome": it.fornecedor, "itens": []}
        grupos[chave]["itens"].append(it)
    return grupos


def _relatorio() -> None:
    itens = todos_itens_padronizados()
    fornecedores = agrupar_por_fornecedor(itens)

    sem_cnpj = [i for i in itens if not i.cnpj]
    sem_situacao = [i for i in itens if i.situacao is None]

    print("=" * 60)
    print("RELATÓRIO — ETAPA 2.7 (padronização de fornecedor/CNPJ/situação)")
    print("=" * 60)
    print(f"Total de itens analisados: {len(itens)}")
    print(f"  - de pregoes_itens: {len(itens_de_pregoes_itens())}")
    print(f"  - de participacao_itens: {len(itens_de_participacao())}")
    print(f"Fornecedores distintos (por CNPJ): {len(fornecedores)}")
    print(f"Itens SEM CNPJ identificado: {len(sem_cnpj)}")
    print(f"Itens SEM situação (== todos, hoje): {len(sem_situacao)} / {len(itens)}")

    print("\n--- TESTE 1: item com fornecedor ---")
    ex = next((i for i in itens if i.fornecedor), None)
    print(ex)

    print("\n--- TESTE 2: item com fornecedor + CNPJ ---")
    ex = next((i for i in itens if i.fornecedor and i.cnpj), None)
    print(ex)

    print("\n--- TESTE 3: item cujo CNPJ estava embutido no campo fornecedor (pregoes_itens) ---")
    ex = next((i for i in itens_de_pregoes_itens() if i.cnpj), None)
    print(ex)

    print("\n--- TESTE 4: vários itens do mesmo fornecedor ---")
    maior = max(fornecedores.items(), key=lambda kv: len(kv[1]["itens"]))
    print(f"CNPJ {maior[0]} ({maior[1]['nome']}): {len(maior[1]['itens'])} item(ns)")
    for it in maior[1]["itens"][:5]:
        print(f"    item {it.item} — {it.descricao[:60]}")

    print("\n--- TESTE 5: itens de fornecedores diferentes (amostra) ---")
    for cnpj, dados in list(fornecedores.items())[:3]:
        print(f"  {cnpj} ({dados['nome']}): {len(dados['itens'])} item(ns)")

    print("\n--- TESTE 6: situação do item ---")
    print(f"  Nenhum item tem situação real capturada hoje (confirmado: {len(sem_situacao)}/{len(itens)}).")
    print("  Não inventei um valor default — fica None mesmo.")

    print("\n--- TESTE 7: fase do pregão separada da situação do item ---")
    conn = _conectar()
    try:
        cur = conn.execute('SELECT pregao, fase FROM pregoes_fase LIMIT 5')
        fases = {r["pregao"]: r["fase"] for r in cur.fetchall()}
    except sqlite3.OperationalError:
        fases = {}
    finally:
        conn.close()
    for pregao, fase in fases.items():
        print(f"  Pregão {pregao}: fase='{fase}' (tabela pregoes_fase) — "
              f"situação de item NÃO foi copiada disso, continua None.")


if __name__ == "__main__":
    _relatorio()
