"""
ponte.py - ponte HTTP <-> WebSocket entre o bot Python e a extensão LicitaFlow.

O bot deixa de ter sessão própria: ele pede uma URL para a ponte, a ponte
repassa o pedido para a extensão via WebSocket, a extensão busca essa URL
dentro da aba do Compras.gov.br que você já deixou logada, e devolve a
resposta para o bot.

Rodar:
    uvicorn ponte:app --port 8765

Protocolo com a extensão (licitaflow/extension/services/ponte.js):
    extensão -> ponte    {"tipo": "ola", "versao": "0.1.0"}
    ponte    -> extensão {"tipo": "fetch", "id", "url", "metodo", "corpo", "cabecalhos"}
    extensão -> ponte    {"id", "ok": true, "status", "url_final", "json", "texto"}
                       ou {"id", "ok": false, "erro": "sessao_expirada", ...}
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI(title="LicitaFlow - ponte")

TIMEOUT_PADRAO = 20.0

_contador = itertools.count(1)
_extensao: Optional[WebSocket] = None
_pendentes: dict[str, "asyncio.Future[dict]"] = {}
_ultima_conexao: Optional[float] = None


@app.websocket("/ws")
async def ws_extensao(ws: WebSocket) -> None:
    global _extensao, _ultima_conexao
    await ws.accept()
    _extensao = ws
    _ultima_conexao = time.time()

    try:
        while True:
            msg = await ws.receive_json()
            tipo = msg.get("tipo")

            if tipo == "ola":
                _ultima_conexao = time.time()
                continue

            if tipo == "pong":
                continue

            # Resposta a um pedido de "fetch" feito antes: tem "id", sem "tipo".
            pedido_id = msg.get("id")
            if pedido_id and pedido_id in _pendentes:
                fut = _pendentes.pop(pedido_id)
                if not fut.done():
                    fut.set_result(msg)

    except WebSocketDisconnect:
        pass
    finally:
        if _extensao is ws:
            _extensao = None


INTERVALO_PING = 15.0


async def _keepalive() -> None:
    """Detecta conexão morta ativamente, em vez de esperar o próximo fetch
    falhar para perceber que a extensão sumiu (ex.: aba fechada, extensão
    recarregada sem um handshake de fechamento limpo)."""
    global _extensao
    while True:
        await asyncio.sleep(INTERVALO_PING)
        if _extensao is None:
            continue
        try:
            await _extensao.send_json({"tipo": "ping"})
        except Exception:
            _extensao = None


@app.on_event("startup")
async def _iniciar_keepalive() -> None:
    asyncio.create_task(_keepalive())


async def _pedir_fetch(url: str, metodo: str, corpo: Any, cabecalhos: Optional[dict],
                        timeout: float) -> dict:
    if _extensao is None:
        return {"ok": False, "erro": "ponte_desconectada",
                "detalhe": "A extensão não está conectada à ponte."}

    pedido_id = str(next(_contador))
    fut: "asyncio.Future[dict]" = asyncio.get_event_loop().create_future()
    _pendentes[pedido_id] = fut

    try:
        await _extensao.send_json({
            "tipo": "fetch", "id": pedido_id, "url": url, "metodo": metodo,
            "corpo": corpo, "cabecalhos": cabecalhos,
        })
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pendentes.pop(pedido_id, None)
        return {"ok": False, "erro": "tempo_esgotado",
                "detalhe": f"Extensão não respondeu em {timeout}s."}
    except Exception as e:
        _pendentes.pop(pedido_id, None)
        return {"ok": False, "erro": "falha_envio", "detalhe": str(e)}


def _responder(resultado: dict) -> JSONResponse:
    if resultado.get("erro") == "sessao_expirada":
        return JSONResponse(resultado, status_code=401)
    if not resultado.get("ok", False):
        return JSONResponse(resultado, status_code=502)
    return JSONResponse(resultado, status_code=200)


@app.get("/fetch")
async def fetch_get(url: str, timeout: float = TIMEOUT_PADRAO):
    return _responder(await _pedir_fetch(url, "GET", None, None, timeout))


@app.post("/fetch")
async def fetch_post(corpo: dict):
    url = corpo.get("url")
    if not url:
        return JSONResponse({"erro": "url_obrigatoria"}, status_code=400)

    metodo = corpo.get("metodo", "GET")
    dados = corpo.get("corpo")
    cabecalhos = corpo.get("cabecalhos")
    timeout = float(corpo.get("timeout", TIMEOUT_PADRAO))

    return _responder(await _pedir_fetch(url, metodo, dados, cabecalhos, timeout))


@app.get("/health")
async def health():
    return {
        "extensao_conectada": _extensao is not None,
        "ultima_conexao": _ultima_conexao,
        "pedidos_pendentes": len(_pendentes),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ponte:app", host="127.0.0.1", port=8765, reload=False)
