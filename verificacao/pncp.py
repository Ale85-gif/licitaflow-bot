"""
verificacao/pncp.py — cruzamento de fornecedor com contratos do PNCP.

Confirmado em 30/08/2026 (manual oficial + documentação pública):
  base:   https://pncp.gov.br/api/consulta   (consulta pública, sem autenticação)
  GET /v1/contratos?cnpjOrgao=<CNPJ do órgão>&dataInicial=AAAAMMDD&dataFinal=AAAAMMDD&pagina=N

IMPORTANTE — limite real da API pública: ela filtra contratos por ÓRGÃO
comprador + período, não por CNPJ do FORNECEDOR vencedor. Não existe um
parâmetro documentado de "cnpj do fornecedor" neste endpoint. Por isso este
módulo busca os contratos do órgão no período informado e filtra pelo
fornecedor no lado do cliente — é o único jeito honesto de fazer isso sem
inventar um parâmetro que a API não tem.

O nome exato do campo do CNPJ do fornecedor dentro de cada contrato
(possivelmente "niFornecedor") não pôde ser confirmado num payload real —
por isso o filtro tenta as variantes mais prováveis. Vale conferir contra
uma resposta real na primeira execução.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import httpx

BASE_URL = "https://pncp.gov.br/api/consulta"
FONTE = "PNCP"
FONTE_URL = "https://pncp.gov.br/app/"

_CAMPOS_CNPJ_FORNECEDOR = ("niFornecedor", "cnpjFornecedor", "nifornecedor")


class ErroConsulta(Exception):
    def __init__(self, codigo: str, detalhe: str):
        self.codigo = codigo
        self.detalhe = detalhe
        super().__init__(f"{codigo}: {detalhe}")


async def _buscar_contratos_do_orgao(cnpj_orgao: str, data_inicial: str, data_final: str) -> list[dict]:
    params = {"cnpjOrgao": cnpj_orgao, "dataInicial": data_inicial, "dataFinal": data_final, "pagina": 1}

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20.0) as client:
        try:
            resp = await client.get("/v1/contratos", params=params)
        except httpx.TimeoutException as e:
            raise ErroConsulta("timeout", str(e)) from e
        except httpx.ConnectError as e:
            raise ErroConsulta("connection_error", str(e)) from e

    if resp.status_code == 429:
        raise ErroConsulta("429", "Limite de requisições do PNCP excedido.")
    if resp.status_code >= 500:
        raise ErroConsulta(str(resp.status_code), "Erro no servidor do PNCP.")
    if resp.status_code != 200:
        raise ErroConsulta(str(resp.status_code), resp.text[:300])

    try:
        dados = resp.json()
    except ValueError as e:
        raise ErroConsulta("json_invalido", "Resposta não é um JSON válido.") from e

    if isinstance(dados, dict):
        return dados.get("data") or dados.get("content") or []
    if isinstance(dados, list):
        return dados
    raise ErroConsulta("formato_inesperado", "Resposta em formato não reconhecido.")


def _cnpj_do_contrato(contrato: dict) -> str:
    for campo in _CAMPOS_CNPJ_FORNECEDOR:
        if contrato.get(campo):
            return str(contrato[campo])
    return ""


async def consultar_pncp(
    cnpj: str,
    cnpj_orgao: Optional[str] = None,
    data_inicial: Optional[str] = None,
    data_final: Optional[str] = None,
) -> dict:
    agora = datetime.now().isoformat()

    if not (cnpj_orgao and data_inicial and data_final):
        return {
            "fonte": FONTE,
            "status": "nao_consultado",
            "mensagem": "Informe órgão (CNPJ da UASG) e período para cruzar dados no PNCP.",
            "registros": [],
            "consultado_em": agora,
            "fonte_url": FONTE_URL,
            "erro_detalhe": None,
        }

    try:
        contratos = await _buscar_contratos_do_orgao(cnpj_orgao, data_inicial, data_final)
    except ErroConsulta as e:
        return {
            "fonte": FONTE,
            "status": "erro",
            "mensagem": f"Não foi possível consultar o PNCP ({e.codigo}).",
            "registros": [],
            "consultado_em": agora,
            "fonte_url": FONTE_URL,
            "erro_detalhe": e.detalhe,
        }

    cnpj_limpo = "".join(c for c in cnpj if c.isdigit())
    encontrados = [c for c in contratos if cnpj_limpo and cnpj_limpo in _cnpj_do_contrato(c)]

    if not encontrados:
        return {
            "fonte": FONTE,
            "status": "sem_registro",
            "mensagem": "Nenhum contrato deste fornecedor localizado no PNCP para o órgão/período consultado.",
            "registros": [],
            "consultado_em": agora,
            "fonte_url": FONTE_URL,
            "erro_detalhe": None,
        }

    return {
        "fonte": FONTE,
        "status": "consulta_realizada",
        "mensagem": f"{len(encontrados)} contrato(s) localizado(s) no PNCP para este fornecedor.",
        "registros": encontrados,
        "consultado_em": agora,
        "fonte_url": FONTE_URL,
        "erro_detalhe": None,
    }
