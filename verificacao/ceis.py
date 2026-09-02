"""
verificacao/ceis.py — Cadastro Nacional de Empresas Inidôneas e Suspensas.

Fonte: API de Dados do Portal da Transparência (ver _portal_transparencia.py
para o cliente HTTP e a documentação dos endpoints/parâmetros confirmados).
"""

from __future__ import annotations

from datetime import datetime

from . import _portal_transparencia as pt

FONTE = "CEIS"
FONTE_URL = "https://portaldatransparencia.gov.br/sancoes/ceis"


async def consultar_ceis(cnpj: str) -> dict:
    agora = datetime.now().isoformat()

    try:
        registros = await pt.buscar("ceis", cnpj)
    except pt.ErroConsulta as e:
        return {
            "fonte": FONTE,
            "status": "erro",
            "mensagem": f"Não foi possível consultar o CEIS ({e.codigo}).",
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
        "mensagem": f"{len(registros)} registro(s) localizado(s) no CEIS.",
        "registros": registros,
        "consultado_em": agora,
        "fonte_url": FONTE_URL,
        "erro_detalhe": None,
    }
