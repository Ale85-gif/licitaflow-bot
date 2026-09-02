"""
ponte_cliente.py - cliente Python para falar com o Compras.gov.br através
da sessão já aberta no navegador, via ponte.py + extensão LicitaFlow.

O bot não guarda mais sessão própria (nada de storage_state, perfil_chrome
ou keepalive). Ele pede uma URL para a ponte; a ponte pede para a extensão
buscar essa URL dentro da aba que você já deixou logada; a resposta volta
pelo mesmo caminho.

Uso no bot:
    from ponte_cliente import get, SessaoExpirada, PonteIndisponivel

    dados = get("https://www.gov.br/compras/.../api/itens", params={"uasg": "160082"})
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

import requests

PONTE_URL = "http://localhost:8765"
TIMEOUT_HTTP = 25.0


class SessaoExpirada(Exception):
    """A sessão do navegador caiu; é preciso logar de novo na aba do portal."""


class PonteIndisponivel(Exception):
    """A ponte não está rodando, ou a extensão não está conectada a ela."""


def _chamar(url: str, metodo: str = "GET", corpo=None, cabecalhos: Optional[dict] = None,
            timeout: float = TIMEOUT_HTTP) -> dict:
    try:
        resp = requests.post(
            f"{PONTE_URL}/fetch",
            json={"url": url, "metodo": metodo, "corpo": corpo,
                  "cabecalhos": cabecalhos, "timeout": timeout},
            timeout=timeout + 5,
        )
    except requests.ConnectionError as e:
        raise PonteIndisponivel(
            f"Não consegui falar com a ponte em {PONTE_URL}. "
            "Ela está rodando (uvicorn ponte:app --port 8765)?"
        ) from e

    if resp.status_code == 401:
        raise SessaoExpirada(
            "A sessão do Compras.gov.br expirou. Faça login de novo na aba do portal."
        )

    dados = resp.json()

    if resp.status_code == 502:
        erro = dados.get("erro", "erro_desconhecido")
        if erro == "ponte_desconectada":
            raise PonteIndisponivel(
                "A extensão LicitaFlow não está conectada à ponte. "
                "Abra o popup da extensão e clique em 'Ligar'."
            )
        raise RuntimeError(f"Falha na ponte: {erro} — {dados.get('detalhe', '')}")

    return dados


def get(url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
        timeout: float = TIMEOUT_HTTP):
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode(params)}"
    resultado = _chamar(url, "GET", None, headers, timeout)
    return resultado.get("json") if resultado.get("json") is not None else resultado.get("texto")


def post(url: str, corpo: Optional[dict] = None, headers: Optional[dict] = None,
         timeout: float = TIMEOUT_HTTP):
    resultado = _chamar(url, "POST", corpo, headers, timeout)
    return resultado.get("json") if resultado.get("json") is not None else resultado.get("texto")


def online() -> bool:
    """True só quando a ponte está de pé E a extensão está conectada a ela."""
    try:
        resp = requests.get(f"{PONTE_URL}/health", timeout=3)
        return resp.ok and resp.json().get("extensao_conectada", False)
    except requests.RequestException:
        return False
