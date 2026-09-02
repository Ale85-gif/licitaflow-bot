"""
verificacao/confirmacoes.py — confirmação manual de consultas que não têm
API pública (hoje, só o SICAF).

Regra de segurança que não muda: isto NUNCA vira "sem registro" ou "regular".
Só registra que um humano abriu a fonte oficial e conferiu, quando, e com
qual comprovante — a leitura do que foi encontrado continua sendo do
usuário, fora do sistema. Ver _aplicar_confirmacoes_manuais() em api.py para
como isso entra no resultado consolidado (como "confirmado_manualmente",
nunca como "sem_registro").
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

ARQUIVO = Path(__file__).resolve().parent.parent / "licitaflow" / "confirmacoes_manuais.json"


def _chave(cnpj: str, fonte: str) -> str:
    return f"{cnpj}:{fonte}"


def _carregar() -> dict:
    if not ARQUIVO.exists():
        return {}
    try:
        return json.loads(ARQUIVO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _salvar(indice: dict) -> None:
    ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO.write_text(json.dumps(indice, ensure_ascii=False, indent=2), encoding="utf-8")


def registrar(cnpj: str, fonte: str, arquivo: Optional[str] = None) -> dict:
    indice = _carregar()
    entrada = {"confirmadoEm": datetime.now().isoformat(), "arquivo": arquivo}
    indice[_chave(cnpj, fonte)] = entrada
    _salvar(indice)
    return entrada


def obter(cnpj: str, fonte: str) -> Optional[dict]:
    return _carregar().get(_chave(cnpj, fonte))


def remover(cnpj: str, fonte: str) -> None:
    indice = _carregar()
    indice.pop(_chave(cnpj, fonte), None)
    _salvar(indice)
