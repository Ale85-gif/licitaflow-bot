"""
preparar_atas_processo.py - ETAPA 2 (Checkpoint 5): entrada do painel
para preparar as Atas de um processo_id já confirmado (Etapa 1).

Só GLUE — não duplica nenhuma lógica. Reaproveita:
  - processos_repo.validar_processo (âncora, Checkpoint 2)
  - bot_criar_ata.obter_modelo_ata / coletar_fornecedores_itens_do_processo /
    criar_atas_todos_fornecedores / navegar_para_artefatos_digitais /
    extrair_itens_tr (Checkpoints 1, 3, 4 — intocados)
  - capturar_fase._ir_para_area_trabalho / _clicar_linha_do_pregao
    (já em produção, usadas hoje por /api/fase/atualizar) — resolve a
    navegação da Área de Trabalho até a tela do pregão específico, sem
    reinventar isso aqui.

Uso:
    python preparar_atas_processo.py <processo_id>

Grava progresso real (eventos) em logs/preparar_atas_status.json e o
histórico final em licitaflow/preparacoes_atas.json. Nunca clica em
"Concluir"/"Publicar" — cada Ata fica em RASCUNHO ou REVISÃO NECESSÁRIA
(ver bot_criar_ata.py, Checkpoint 4).
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from comum import abrir_chrome, log
import processos_repo
from bot_criar_ata import (
    obter_modelo_ata,
    coletar_fornecedores_itens_do_processo,
    criar_atas_todos_fornecedores,
    navegar_para_artefatos_digitais,
    extrair_itens_tr,
)
from capturar_fase import _ir_para_area_trabalho, _clicar_linha_do_pregao

RAIZ = Path(__file__).resolve().parent
ANEXOS_DIR = RAIZ / "licitaflow" / "anexos"
ANEXOS_INDEX = ANEXOS_DIR / "index.json"
STATUS_ARQUIVO = RAIZ / "logs" / "preparar_atas_status.json"
PREPARACOES_ARQUIVO = RAIZ / "licitaflow" / "preparacoes_atas.json"


def _slug_pregao(numero: str) -> str:
    import re
    return re.sub(r"[^\w.-]", "_", numero)


def _carregar_anexos() -> dict:
    if not ANEXOS_INDEX.exists():
        return {}
    try:
        return json.loads(ANEXOS_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _emitir(processo_id: str, evento: str, **dados) -> None:
    """Acrescenta um evento real ao arquivo de status (lido pelo painel
    via GET /api/processos/{id}/atas/preparar/status). Nunca escreve
    percentuais/etapas fictícias — só o que de fato aconteceu."""
    STATUS_ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    atual = {"processoId": processo_id, "eventos": [], "concluido": False, "resultado": None}
    if STATUS_ARQUIVO.exists():
        try:
            atual = json.loads(STATUS_ARQUIVO.read_text(encoding="utf-8"))
        except Exception:
            pass
    atual["processoId"] = processo_id
    atual.setdefault("eventos", []).append({
        "em": datetime.now().strftime("%H:%M:%S"), "evento": evento, **dados,
    })
    STATUS_ARQUIVO.write_text(json.dumps(atual, ensure_ascii=False, indent=2), encoding="utf-8")


def _finalizar(processo_id: str, resultado: dict) -> None:
    atual = {"processoId": processo_id, "eventos": [], "concluido": False, "resultado": None}
    if STATUS_ARQUIVO.exists():
        try:
            atual = json.loads(STATUS_ARQUIVO.read_text(encoding="utf-8"))
        except Exception:
            pass
    atual["concluido"] = True
    atual["resultado"] = resultado
    STATUS_ARQUIVO.write_text(json.dumps(atual, ensure_ascii=False, indent=2), encoding="utf-8")

    # Histórico persistido por processo_id (nunca sobrescreve execuções
    # anteriores do mesmo processo — acrescenta).
    historico = {}
    if PREPARACOES_ARQUIVO.exists():
        try:
            historico = json.loads(PREPARACOES_ARQUIVO.read_text(encoding="utf-8"))
        except Exception:
            historico = {}
    historico.setdefault(processo_id, []).append({**resultado, "preparadoEm": datetime.now().isoformat()})
    PREPARACOES_ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    PREPARACOES_ARQUIVO.write_text(json.dumps(historico, ensure_ascii=False, indent=2), encoding="utf-8")


async def preparar(processo_id: str) -> dict:
    # 1. Processo (âncora obrigatória — Checkpoint 2/5).
    processo = processos_repo.validar_processo(processo_id)
    _emitir(processo_id, "processo_validado", pregao=processo["pregaoCompleto"])

    # 2. Modelo (Checkpoint 3 — hoje sempre 279).
    modelo = obter_modelo_ata(processo_id)
    _emitir(processo_id, "modelo_identificado", modelo=f"{modelo['numero']}/{modelo['ano']}")

    # 3. TR real, já exigido como anexo pela Etapa 1 antes de confirmar.
    anexo = _carregar_anexos().get(processo["pregaoCompleto"])
    if not anexo:
        raise RuntimeError(
            f"Processo {processo_id} confirmado, mas o TR anexado para o pregão "
            f"{processo['pregaoCompleto']!r} não foi encontrado (anexo removido depois da confirmação?)."
        )
    caminho_tr = ANEXOS_DIR / _slug_pregao(processo["pregaoCompleto"]) / anexo["arquivo"]
    if not caminho_tr.exists():
        raise RuntimeError(f"Arquivo do TR não encontrado em disco: {caminho_tr}")
    itens_tr = extrair_itens_tr(str(caminho_tr))

    # 4. Chrome + navegação até a tela do pregão (reaproveita capturar_fase.py).
    abrir_chrome()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        if not browser.contexts:
            raise RuntimeError("Nenhum contexto encontrado no Chrome. Conecte antes de preparar Atas.")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        await page.bring_to_front()

        await _ir_para_area_trabalho(page)
        pagina_fornecedores = await _clicar_linha_do_pregao(page, context, processo["pregaoCompleto"])

        fornecedores = await coletar_fornecedores_itens_do_processo(pagina_fornecedores, processo_id)
        total_itens = sum(len(f.get("itens", [])) for f in fornecedores)
        _emitir(processo_id, "fornecedores_identificados", quantidade=len(fornecedores))
        _emitir(processo_id, "itens_analisados", quantidade=total_itens)

        pagina_artefatos = await navegar_para_artefatos_digitais(page)
        _emitir(processo_id, "preparando_atas")

        arquivo_relatorio = str(RAIZ / "logs" / f"preparar_atas_{processo_id}.csv")
        relatorio = await criar_atas_todos_fornecedores(
            pagina_artefatos, processo_id, fornecedores, itens_tr, arquivo_relatorio=arquivo_relatorio,
        )

    atas = [
        {
            "fornecedor": r["fornecedor"], "cnpj": r["cnpj"],
            "itens": len(next((f["itens"] for f in fornecedores if f["cnpj"] == r["cnpj"]), [])),
            "estado": r["status"], "identificador": r["ata"],
            "divergencias": r.get("divergencias", []),
        }
        for r in relatorio
    ]
    pendencias = [a for a in atas if a["estado"] != "RASCUNHO"]

    return {
        "processoId": processo_id,
        "pregao": processo["pregaoCompleto"],
        "estado": "concluido",
        "atas": atas,
        "pendencias": pendencias,
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python preparar_atas_processo.py <processo_id>")
        sys.exit(1)
    processo_id = sys.argv[1]

    try:
        resultado = asyncio.run(preparar(processo_id))
        _finalizar(processo_id, resultado)
        log(f"Preparação concluída para processo {processo_id}: {len(resultado['atas'])} ata(s).")
    except Exception as e:
        traceback.print_exc()
        _finalizar(processo_id, {
            "processoId": processo_id, "pregao": None, "estado": "erro",
            "atas": [], "pendencias": [], "motivo": str(e),
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
