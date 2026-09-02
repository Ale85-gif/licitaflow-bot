"""
verificacao/consolidacao.py — junta os resultados das fontes num status geral.

Regra de prioridade (a mais severa vence): um registro encontrado em
qualquer fonte sempre domina, mesmo que as outras estejam limpas. Uma fonte
"não consultada" (o SICAF, sempre) deixa o resultado geral como "consulta
parcial" em vez de "sem registro" — nunca se transforma indisponibilidade em
resultado positivo (regra 24 do módulo).
"""

from __future__ import annotations

_ORDEM_SEVERIDADE = [
    "registro_encontrado",
    "erro",
    "atencao",
    "nao_consultado",
    "sem_registro",
    "confirmado_manualmente",
    "consulta_realizada",
]

_STATUS_GERAL = {
    "registro_encontrado": ("🔴", "REGISTRO ENCONTRADO"),
    "erro": ("🟠", "CONSULTA INCOMPLETA"),
    "atencao": ("🟡", "ATENÇÃO"),
    "nao_consultado": ("🟡", "VERIFICAÇÃO INCOMPLETA"),
    "sem_registro": ("🟢", "SEM REGISTRO"),
    "confirmado_manualmente": ("🔵", "CONSULTA REALIZADA"),
    "consulta_realizada": ("🔵", "CONSULTA REALIZADA"),
}


def consolidar(resultados: list[dict]) -> dict:
    status_presentes = {r["status"] for r in resultados}

    emoji, rotulo = "⚪", "NÃO CONSULTADO"
    for status in _ORDEM_SEVERIDADE:
        if status in status_presentes:
            emoji, rotulo = _STATUS_GERAL[status]
            break

    return {
        "status": rotulo,
        "emoji": emoji,
        "fontes": resultados,
    }
