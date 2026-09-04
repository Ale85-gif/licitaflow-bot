"""Leitura/validação do arquivo licitaflow/processos.json (Etapa 1 -
Identificação do Processo: Pregão + TR + Processo Administrativo + UASG).

Módulo compartilhado entre api.py (painel) e bot_criar_ata.py (geração
de Ata) para não duplicar a mesma lógica de validação em dois lugares
nem acoplar o bot ao app FastAPI. Só depende de json/re/pathlib -
importável tanto de dentro de um processo Playwright quanto de um
processo uvicorn sem custo extra.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

RAIZ = Path(__file__).resolve().parent
PROCESSOS_ARQUIVO = RAIZ / "licitaflow" / "processos.json"
UASG_FIXA = "160082"


class ProcessoNaoConfirmado(Exception):
    """processo_id inexistente, não confirmado, ou que não corresponde
    ao pregão/UASG/TR esperado. Nunca deve ser contornada - é sempre um
    sinal para parar a geração da Ata e revisar manualmente."""


def carregar_processos() -> dict:
    if not PROCESSOS_ARQUIVO.exists():
        return {}
    try:
        return json.loads(PROCESSOS_ARQUIVO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def salvar_processos(indice: dict) -> None:
    PROCESSOS_ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    PROCESSOS_ARQUIVO.write_text(json.dumps(indice, ensure_ascii=False, indent=2), encoding="utf-8")


def montar_processo_id(uasg: str, num_pregao: str, ano_pregao: str, num_tr: str, ano_tr: str) -> str:
    bruto = f"{uasg}-{num_pregao}-{ano_pregao}-{num_tr}-{ano_tr}"
    return re.sub(r"[^\w.-]", "_", bruto)


def partir_composto(valor: str) -> tuple[str, str]:
    """'90020/2026' -> ('90020', '2026'). Sem barra, devolve ('valor', '')."""
    valor = str(valor or "").strip()
    if "/" in valor:
        numero, ano = valor.split("/", 1)
        return numero.strip(), ano.strip()
    return valor, ""


def processo_confirmado_para(numero_pregao: str) -> Optional[dict]:
    """Processo confirmado mais recente para esse pregão, ou None."""
    candidatos = [
        {**p, "processoId": pid}
        for pid, p in carregar_processos().items()
        if p.get("pregaoCompleto") == numero_pregao
    ]
    if not candidatos:
        return None
    candidatos.sort(key=lambda p: p.get("confirmadoEm", ""))
    return candidatos[-1]


def validar_processo(processo_id: str, numero_pregao: Optional[str] = None) -> dict:
    """Confirma que `processo_id` existe em processos.json (== está
    confirmado - só existe registro lá depois de POST /api/processos/confirmar,
    não há estado "rascunho" nesse arquivo), que a UASG bate com a
    configurada e que há um TR registrado. Se `numero_pregao` for
    informado, confirma também que é exatamente o pregão desse processo
    (nunca aceita o processo de um pregão sendo usado para outro).

    Levanta ProcessoNaoConfirmado em qualquer caso contrário. Retorna o
    registro do processo (dict, com "processoId" incluso)."""
    processos = carregar_processos()
    processo = processos.get(processo_id)
    if not processo:
        raise ProcessoNaoConfirmado(f"processo_nao_confirmado: {processo_id!r} não encontrado.")

    if processo.get("uasg") != UASG_FIXA:
        raise ProcessoNaoConfirmado(
            f"processo_nao_confirmado: UASG do processo {processo_id!r} "
            f"({processo.get('uasg')!r}) diferente da configurada ({UASG_FIXA!r})."
        )

    if not processo.get("tr"):
        raise ProcessoNaoConfirmado(f"processo_nao_confirmado: processo {processo_id!r} sem TR registrado.")

    if numero_pregao is not None and processo.get("pregaoCompleto") != numero_pregao:
        raise ProcessoNaoConfirmado(
            f"processo_nao_confirmado: processo {processo_id!r} pertence ao pregão "
            f"{processo.get('pregaoCompleto')!r}, não a {numero_pregao!r}."
        )

    return {**processo, "processoId": processo_id}
