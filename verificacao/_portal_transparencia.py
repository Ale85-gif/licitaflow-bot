"""
verificacao/_portal_transparencia.py — cliente HTTP compartilhado por CEIS e
CNEP (ambos vivem na mesma API de Dados do Portal da Transparência).

Endpoints, parâmetros e cabeçalho de autenticação confirmados na
documentação oficial (swagger) em 30/08/2026:
  base:   https://api.portaldatransparencia.gov.br
  GET /api-de-dados/ceis?codigoSancionado=<CNPJ>&pagina=<n>
  GET /api-de-dados/cnep?codigoSancionado=<CNPJ>&pagina=<n>
  header: chave-api-dados: <chave>

A chave é obtida fazendo login gov.br em
https://api.portaldatransparencia.gov.br/api-de-dados e cadastrando um
e-mail — nunca deve ficar hardcoded no código (ver PORTAL_TRANSPARENCIA_API_KEY
no .env).

Os nomes exatos dos campos de resposta (CeisDTO/CnepDTO) não vêm descritos
no schema público do swagger — só a lista de parâmetros de entrada foi
confirmada. Por isso os módulos ceis.py/cnep.py devolvem os registros brutos
como vieram da API, sem tentar renomear campos que não foram confirmados.
"""

from __future__ import annotations

import os

import httpx

BASE_URL = "https://api.portaldatransparencia.gov.br"
MAX_PAGINAS = 50  # trava de segurança contra loop infinito de paginação


class ErroConsulta(Exception):
    def __init__(self, codigo: str, detalhe: str):
        self.codigo = codigo
        self.detalhe = detalhe
        super().__init__(f"{codigo}: {detalhe}")


def _chave_api() -> str:
    chave = os.getenv("PORTAL_TRANSPARENCIA_API_KEY")
    if not chave:
        raise ErroConsulta(
            "sem_chave",
            "PORTAL_TRANSPARENCIA_API_KEY não configurada (veja .env.example).",
        )
    return chave


async def buscar(recurso: str, cnpj: str) -> list[dict]:
    """GET /api-de-dados/{recurso}?codigoSancionado=...&pagina=N, paginando
    até a API devolver uma página vazia."""
    headers = {"chave-api-dados": _chave_api(), "Accept": "application/json"}
    resultados: list[dict] = []
    pagina = 1

    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=20.0) as client:
        while pagina <= MAX_PAGINAS:
            try:
                resp = await client.get(
                    f"/api-de-dados/{recurso}",
                    params={"codigoSancionado": cnpj, "pagina": pagina},
                )
            except httpx.TimeoutException as e:
                raise ErroConsulta("timeout", str(e)) from e
            except httpx.ConnectError as e:
                raise ErroConsulta("connection_error", str(e)) from e

            if resp.status_code == 429:
                raise ErroConsulta("429", "Limite de requisições da API excedido.")
            if resp.status_code in (401, 403):
                raise ErroConsulta(str(resp.status_code), "Chave de API inválida ou sem permissão.")
            if resp.status_code == 404:
                raise ErroConsulta("404", "Recurso não encontrado — endpoint pode ter mudado.")
            if resp.status_code >= 500:
                raise ErroConsulta(str(resp.status_code), "Erro no servidor do Portal da Transparência.")
            if resp.status_code != 200:
                raise ErroConsulta(str(resp.status_code), resp.text[:300])

            try:
                pagina_dados = resp.json()
            except ValueError as e:
                raise ErroConsulta("json_invalido", "Resposta não é um JSON válido.") from e

            if not isinstance(pagina_dados, list):
                raise ErroConsulta("formato_inesperado", "Resposta não é uma lista de registros.")

            if not pagina_dados:
                break

            resultados.extend(pagina_dados)
            pagina += 1

    return resultados
