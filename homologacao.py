"""
homologacao.py - Etapa 2.10: motor de homologação.

Camada de INTERPRETAÇÃO separada do texto original (nunca perde o texto
original, nunca inventa/assume uma classificação pra texto desconhecido).

NÃO gera Ata. NÃO decide sozinho o que fazer com casos especiais como
"Homologado (anulado)" -- só sinaliza que é especial e não conta como
elegível, deixando a regra de negócio pra ser definida depois (pedido
explícito do usuário).

Trata ITEM NORMAL e GRUPO como estruturas diferentes:
  - item normal: fornecedor/CNPJ/situação são do PRÓPRIO item.
  - grupo: fornecedor/CNPJ/situação são do GRUPO INTEIRO. Os itens-filhos
    (número+descrição) só existem pra mostrar a composição -- nunca
    recebem situação própria artificialmente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Classificacao(str, Enum):
    HOMOLOGADO = "HOMOLOGADO"
    PENDENTE = "PENDENTE"
    FRACASSADO = "FRACASSADO"
    ANULADO = "ANULADO"           # especial -- NUNCA conta como homologado
    SEM_SITUACAO = "SEM_SITUACAO"  # situação real ainda não foi capturada
    DESCONHECIDA = "DESCONHECIDA"  # texto que o site mostrou mas o SARP não conhece ainda


# Mapa fechado dos textos REAIS já confirmados no Compras.gov.br (Etapa
# 2.8/2.9/2.9.1, ao vivo, pregões 90020/2026 e 13/2026). Texto que não
# estiver aqui vira DESCONHECIDA -- nunca é tratado como se fosse um dos
# conhecidos "porque parece parecido".
_MAPA_CLASSIFICACAO: dict[str, Classificacao] = {
    "Homologado": Classificacao.HOMOLOGADO,
    "Homologado (anulado)": Classificacao.ANULADO,
    "Aguardando julgamento": Classificacao.PENDENTE,
    "Julgado e habilitado (aguardando adjudicação)": Classificacao.PENDENTE,
    "Fracassado (aguardando homologação)": Classificacao.FRACASSADO,
}


def classificar_situacao(texto_original: str | None) -> Classificacao:
    """Nunca perde texto_original (quem usar isso guarda os dois campos
    lado a lado). Texto não mapeado -> DESCONHECIDA, nunca um chute."""
    if not texto_original:
        return Classificacao.SEM_SITUACAO
    return _MAPA_CLASSIFICACAO.get(texto_original.strip(), Classificacao.DESCONHECIDA)


# Só isso conta como "pode entrar numa Ata". ANULADO fica de fora de
# propósito -- é a decisão que o usuário pediu pra deixar em aberto até
# entender o significado operacional, então por enquanto NÃO é elegível.
CLASSIFICACOES_ELEGIVEIS = {Classificacao.HOMOLOGADO}


@dataclass
class ItemAvaliado:
    numero: str
    descricao: str | None
    situacao_texto: str | None
    situacao_classe: Classificacao
    elegivel: bool


@dataclass
class FornecedorAvaliado:
    tipo: str  # "item_normal" ou "grupo"
    identificador: str  # numero do item OU "GRUPO N"
    fornecedor: str | None
    cnpj: str | None
    itens: list[ItemAvaliado] = field(default_factory=list)
    homologados: int = 0
    pendentes: int = 0
    total: int = 0
    apto: bool = False
    motivo: str = ""
    # Situação da PRÓPRIA unidade (o item, quando tipo=item_normal; o
    # grupo, quando tipo=grupo) -- None em item_normal_multi, onde cada
    # item da lista tem a sua própria (não existe uma situação única do
    # "fornecedor"). Usado pela Etapa 2.11 (fila de Ata) pra não ter que
    # recalcular a classificação de novo.
    situacao_texto: str | None = None
    situacao_classe: "Classificacao | None" = None


def avaliar_item_normal(numero: str, descricao: str | None, fornecedor: str | None,
                         cnpj: str | None, situacao_texto: str | None) -> FornecedorAvaliado:
    """Um item normal = um 'fornecedor' de 1 item só, pra caber no mesmo
    formato de avaliação usado pra fornecedores com vários itens."""
    classe = classificar_situacao(situacao_texto)
    elegivel = classe in CLASSIFICACOES_ELEGIVEIS
    item = ItemAvaliado(numero, descricao, situacao_texto, classe, elegivel)

    if not fornecedor:
        motivo = "sem fornecedor identificado"
        apto = False
    elif elegivel:
        motivo = "item homologado"
        apto = True
    else:
        motivo = f"situação '{situacao_texto}' não é elegível (classificada como {classe.value})"
        apto = False

    return FornecedorAvaliado(
        tipo="item_normal", identificador=numero, fornecedor=fornecedor, cnpj=cnpj,
        itens=[item], homologados=1 if elegivel else 0, pendentes=0 if elegivel else 1,
        total=1, apto=apto, motivo=motivo,
        situacao_texto=situacao_texto, situacao_classe=classe,
    )


def avaliar_fornecedor_multiitem(fornecedor: str, cnpj: str, itens: list[dict]) -> FornecedorAvaliado:
    """`itens`: lista de {"numero", "descricao", "situacao"} -- todos os
    itens desse fornecedor num MESMO pregão (chave pregão+item já deve
    ter sido resolvida por quem chama)."""
    avaliados = []
    homologados = 0
    for it in itens:
        classe = classificar_situacao(it.get("situacao"))
        elegivel = classe in CLASSIFICACOES_ELEGIVEIS
        if elegivel:
            homologados += 1
        avaliados.append(ItemAvaliado(
            numero=it.get("numero") or it.get("numero_item"),
            descricao=it.get("descricao"),
            situacao_texto=it.get("situacao"),
            situacao_classe=classe,
            elegivel=elegivel,
        ))

    total = len(avaliados)
    pendentes = total - homologados
    apto = total > 0 and pendentes == 0
    motivo = (
        "nenhum item associado" if total == 0 else
        "todos os itens homologados" if apto else
        f"{pendentes} de {total} item(ns) ainda não elegível(is)"
    )

    return FornecedorAvaliado(
        tipo="item_normal_multi", identificador=f"{fornecedor} ({cnpj})",
        fornecedor=fornecedor, cnpj=cnpj, itens=avaliados,
        homologados=homologados, pendentes=pendentes, total=total,
        apto=apto, motivo=motivo,
    )


def avaliar_grupo(numero_grupo: str, situacao_texto: str | None,
                   fornecedor: str | None, cnpj: str | None,
                   itens_filhos: list[dict]) -> FornecedorAvaliado:
    """Grupo: UMA situação (do grupo inteiro), UM fornecedor (ou nenhum).
    Os itens-filhos NUNCA recebem situação própria aqui -- só entram na
    lista pra mostrar a composição do grupo."""
    classe = classificar_situacao(situacao_texto)
    elegivel = classe in CLASSIFICACOES_ELEGIVEIS

    itens_avaliados = [
        ItemAvaliado(
            numero=f"{numero_grupo}.{it.get('numero_item') or it.get('numero')}",
            descricao=it.get("descricao"),
            situacao_texto=None,  # de propósito -- item-filho não tem situação própria
            situacao_classe=Classificacao.SEM_SITUACAO,
            elegivel=False,  # irrelevante pra grupo -- quem decide é o grupo, não o item-filho
        )
        for it in itens_filhos
    ]

    if not fornecedor:
        motivo = "grupo ainda sem fornecedor (aguardando resultado do julgamento/habilitação)"
        apto = False
    elif elegivel:
        motivo = "grupo homologado"
        apto = True
    else:
        motivo = f"situação do grupo '{situacao_texto}' não é elegível (classificada como {classe.value})"
        apto = False

    return FornecedorAvaliado(
        tipo="grupo", identificador=numero_grupo, fornecedor=fornecedor, cnpj=cnpj,
        itens=itens_avaliados,
        homologados=len(itens_avaliados) if apto else 0,
        pendentes=0 if apto else len(itens_avaliados),
        total=len(itens_avaliados),
        apto=apto, motivo=motivo,
        situacao_texto=situacao_texto, situacao_classe=classe,
    )
