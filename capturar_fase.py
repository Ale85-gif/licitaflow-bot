"""
capturar_fase.py - Etapa 2.5: captura a FASE REAL de um pregão na Área de
Trabalho do lado Governo do Compras.gov.br (cnetmobile), sem inventar nada.

Fonte confirmada (ver diagnóstico anterior):
    tela .../comprasnet-web/seguro/governo/selecao-fornecedores
    elemento: div.step-item[aria-selected="true"]
    valor lido: atributo aria-label (fallback: texto de .step-label)

Regra fundamental: NUNCA deduzir a fase a partir de status de item, situação
de ata, fornecedor, homologação ou qualquer outra informação indireta. Se o
elemento não for encontrado, grava encontrado=0 e fase=NULL -- o chamador
NÃO deve reaproveitar uma fase antiga como se fosse a atual.

Não mexe em fornecedores, itens, Termo de Referência, homologação, SICAF,
sanções nem geração de ata. Só grava a fase na tabela `pregoes_fase`
(própria, nova -- não toca nas tabelas que "Bot comprasnet .py"/"rapido.py"
já mantêm).

Uso:
    python capturar_fase.py                # varre todos os pregões de pregoes_indice
    python capturar_fase.py 90020/2026     # só esse pregão
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from comum import DB_PATH, log

URL_AREA_TRABALHO = "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-area-trabalho-web/seguro/governo/area-trabalho"


def _garantir_tabela() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pregoes_fase (
                pregao         TEXT PRIMARY KEY,
                fase           TEXT,
                encontrado     INTEGER NOT NULL,
                motivo         TEXT,
                atualizado_em  TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _salvar_fase(pregao: str, fase: str | None, encontrado: bool, motivo: str | None) -> None:
    """Sempre sobrescreve a linha inteira (nunca faz merge parcial) -- uma
    leitura que falhou não pode deixar uma fase antiga "sobrevivendo" junto
    com um encontrado=0 nem meio caminho andado."""
    agora = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO pregoes_fase (pregao, fase, encontrado, motivo, atualizado_em)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pregao) DO UPDATE SET
                fase=excluded.fase,
                encontrado=excluded.encontrado,
                motivo=excluded.motivo,
                atualizado_em=excluded.atualizado_em
            """,
            (pregao, fase, 1 if encontrado else 0, motivo, agora),
        )
        conn.commit()
    finally:
        conn.close()


async def _clicar_linha_do_pregao(page, context, numero_ano: str):
    """Clica na linha da Área de Trabalho que contém `numero_ano` (ex:
    "90020/2026") e retorna a aba que abrir (a maioria das ações abre aba
    nova). Levanta RuntimeError com motivo claro se não achar a linha."""
    linha = page.locator("div.linha-item-trabalho", has_text=numero_ano).first
    if await linha.count() == 0:
        raise RuntimeError(f"Pregão {numero_ano} não aparece na Área de Trabalho.")

    await linha.wait_for(state="visible", timeout=15000)
    texto_linha = await linha.inner_text()
    log(f"  Linha encontrada: {texto_linha!r}")

    links = linha.get_by_role("link")
    if await links.count() == 0:
        raise RuntimeError(f"Linha do pregão {numero_ano} não tem link de ação clicável.")

    paginas_antes = set(context.pages)
    await links.last.click()

    for _ in range(20):
        await page.wait_for_timeout(500)
        novas = [pg for pg in context.pages if pg not in paginas_antes]
        if novas:
            return novas[-1]

    # Nenhuma aba nova: algumas ações navegam a própria aba.
    return page


async def capturar_fase_pregao(page_area_trabalho, context, numero_ano: str) -> dict:
    """Retorna {"pregao", "fase", "encontrado", "motivo"}. NUNCA inventa —
    encontrado=False + fase=None quando não dá pra confirmar."""
    try:
        pagina = await _clicar_linha_do_pregao(page_area_trabalho, context, numero_ano)
        await pagina.wait_for_load_state("domcontentloaded", timeout=15000)
        await pagina.wait_for_timeout(2000)

        step_ativo = pagina.locator('div.step-item[aria-selected="true"]').first
        if await step_ativo.count() == 0:
            return {
                "pregao": numero_ano, "fase": None, "encontrado": False,
                "motivo": "Elemento div.step-item[aria-selected='true'] não encontrado na página.",
            }

        fase = await step_ativo.get_attribute("aria-label")
        if not fase:
            # Fallback: texto de .step-label dentro do step ativo.
            rotulo = step_ativo.locator(".step-label").first
            fase = (await rotulo.inner_text()).strip() if await rotulo.count() > 0 else None

        if not fase:
            return {
                "pregao": numero_ano, "fase": None, "encontrado": False,
                "motivo": "Elemento do passo ativo encontrado, mas sem texto de fase legível.",
            }

        return {"pregao": numero_ano, "fase": fase.strip(), "encontrado": True, "motivo": None}

    except Exception as e:
        return {
            "pregao": numero_ano, "fase": None, "encontrado": False,
            "motivo": f"Erro ao capturar: {e}",
        }


async def _ir_para_area_trabalho(page):
    await page.goto(URL_AREA_TRABALHO)
    await page.wait_for_timeout(5000)
    if "acesso-nao-autorizado" in page.url:
        log("Sessão caiu — clicando 'Efetuar Login' para renovar...")
        await page.get_by_text("Efetuar Login", exact=True).first.click()
        await page.wait_for_timeout(4000)


def _pregoes_de_pregoes_indice() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT DISTINCT \"Pregão\" AS pregao FROM pregoes_indice")
        return [r["pregao"] for r in cur.fetchall() if r["pregao"]]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


async def main():
    _garantir_tabela()

    alvo = sys.argv[1] if len(sys.argv) > 1 else None
    pregoes = [alvo] if alvo else _pregoes_de_pregoes_indice()

    if not pregoes:
        log("Nenhum pregão para verificar (nem argumento, nem pregoes_indice populada).")
        return

    log(f"Verificando fase de {len(pregoes)} pregão(ões)...")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        if not browser.contexts:
            raise RuntimeError("Nenhum contexto encontrado no Chrome. Abra o Chrome e faça login primeiro.")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        await page.bring_to_front()

        for numero_ano in pregoes:
            log(f"Pregão {numero_ano}:")
            await _ir_para_area_trabalho(page)
            resultado = await capturar_fase_pregao(page, context, numero_ano)

            if resultado["encontrado"]:
                log(f"  Fase: {resultado['fase']}")
            else:
                log(f"  Fase não encontrada na leitura atual — {resultado['motivo']}")

            _salvar_fase(
                resultado["pregao"], resultado["fase"],
                resultado["encontrado"], resultado["motivo"],
            )

    log("Concluído.")


if __name__ == "__main__":
    asyncio.run(main())
