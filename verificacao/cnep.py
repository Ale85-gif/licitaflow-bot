"""
verificacao/cnep.py — Cadastro Nacional de Empresas Punidas.

Fonte: API de Dados do Portal da Transparência (mesma base do CEIS — ver
_portal_transparencia.py).
"""

from __future__ import annotations

from datetime import datetime

from . import _portal_transparencia as pt

FONTE = "CNEP"
FONTE_URL = "https://portaldatransparencia.gov.br/sancoes/cnep"


async def consultar_cnep(cnpj: str) -> dict:
    agora = datetime.now().isoformat()

    try:
        registros = await pt.buscar("cnep", cnpj)
    except pt.ErroConsulta as e:
        return {
            "fonte": FONTE,
            "status": "erro",
            "mensagem": f"Não foi possível consultar o CNEP ({e.codigo}).",
            "registros": [],
            "consultado_em": agora,
            "fonte_url": FONTE_URL,
            "erro_detalhe": e.detalhe,
        }

    if not registros:
        return {
            "fonte": FONTE,
            "status": "sem_registro",
            "mensagem": "Nenhum registro localizado na fonte consultada na data e horário informados.",
            "registros": [],
            "consultado_em": agora,
            "fonte_url": FONTE_URL,
            "erro_detalhe": None,
        }

    return {
        "fonte": FONTE,
        "status": "registro_encontrado",
        "mensagem": f"{len(registros)} registro(s) localizado(s) no CNEP.",
        "registros": registros,
        "consultado_em": agora,
        "fonte_url": FONTE_URL,
        "erro_detalhe": None,
    }
