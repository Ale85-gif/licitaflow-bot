"""
verificacao/historico.py — persistência das consultas por CNPJ e detecção de
mudanças em relação à consulta anterior (regra 14 do módulo).

Guardado em JSON ao lado dos outros dados do LicitaFlow (mesmo padrão de
anexos/index.json e atas_geradas.json), uma linha por consulta feita.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ARQUIVO = Path(__file__).resolve().parent.parent / "licitaflow" / "verificacoes_empresas.json"


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


def _comparar(anterior: dict, atual: dict) -> list[dict]:
    mudancas = []
    for fonte, status_atual in atual["fontes"].items():
        status_anterior = anterior["fontes"].get(fonte)
        if status_anterior and status_anterior != status_atual and status_atual == "registro_encontrado":
            mudancas.append({
                "fonte": fonte,
                "de": status_anterior,
                "para": status_atual,
                "consulta_anterior_em": anterior["em"],
            })
    return mudancas


def registrar(cnpj: str, consolidado: dict, pregao: str | None = None, razao_social: str = "") -> dict:
    indice = _carregar()
    linha = indice.setdefault(cnpj, [])
    anterior = linha[-1] if linha else None

    entrada = {
        "em": datetime.now().isoformat(),
        "status": consolidado["status"],
        "fontes": {f["fonte"]: f["status"] for f in consolidado["fontes"]},
        "pregao": pregao,
        "razaoSocial": razao_social,
    }
    linha.append(entrada)
    _salvar(indice)

    mudancas = _comparar(anterior, entrada) if anterior else []
    return {"entrada": entrada, "mudancas": mudancas}


def historico(cnpj: str) -> list[dict]:
    return _carregar().get(cnpj, [])
