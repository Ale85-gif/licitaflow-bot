"""
cruzar_item_fornecedor.py - Etapa 2.9: cruza os dados da aba "Itens"
(número + descrição + situação real, [data-test="situacao-item"], Etapa
2.8) com os da aba "Fornecedores" (fornecedor + CNPJ + itens dele).

Fornecedores: usa capturar_todos_fornecedores_leve() (capturar_itens_
fornecedor_leve.py), não coletar_fornecedores_itens() de bot_criar_ata.py
-- essa última entra em cada item pra pegar Quantidade/Marca/Modelo (que
não precisamos aqui) navegando por uma tela que só existe corretamente na
etapa "Adjudicação/Homologação". Em "Fase Recursal" trava (confirmado ao
vivo no pregão 90020/2026, fornecedor 02.475.798/0002-34: a mesma ação
leva a uma tela de "Recursos e contrarrazões" diferente). A versão leve só
expande a linha do fornecedor (sem entrar em item nenhum) e funciona em
qualquer fase.

Chave do cruzamento: (pregão, número do item) -- nunca só o número do
item sozinho, porque "Item 1" de pregões diferentes não tem relação.

Não cria tabela nova. Não salva nada em dados.db. Só roda em memória e
imprime o relatório de validação. Nenhuma regra de homologação é decidida
aqui -- "Homologado" e "Homologado (anulado)" continuam textos
diferentes, sem julgamento nenhum sobre qual "vale".
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

from playwright.async_api import async_playwright

from capturar_itens_fornecedor_leve import capturar_todos_fornecedores_leve
from comum import log


async def _clicar_linha(page, context, texto_alvo: str):
    linha = page.locator("div.linha-item-trabalho", has_text=texto_alvo).first
    await linha.wait_for(state="visible", timeout=15000)
    paginas_antes = set(context.pages)
    await linha.get_by_role("link").last.click()
    for _ in range(20):
        await page.wait_for_timeout(500)
        novas = [pg for pg in context.pages if pg not in paginas_antes]
        if novas:
            return novas[-1]
    return page


URL_AREA_TRABALHO = "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-area-trabalho-web/seguro/governo/area-trabalho"


async def _ir_para_pregao(page, context, numero_ano: str):
    await page.goto(URL_AREA_TRABALHO)
    await page.wait_for_timeout(5000)
    if "acesso-nao-autorizado" in page.url:
        await page.get_by_text("Efetuar Login", exact=True).first.click()
        await page.wait_for_timeout(4000)
    pagina = await _clicar_linha(page, context, numero_ano)
    await pagina.wait_for_load_state("domcontentloaded", timeout=15000)
    await pagina.wait_for_timeout(2500)
    return pagina


async def capturar_itens_situacao(pagina) -> dict[str, dict]:
    """Aba 'Itens': número + descrição + situação real, paginando até o fim.
    Retorna {numero_item: {"descricao":..., "situacao":..., "tipo":...}}.
    Preserva o texto exato do site -- "Homologado (anulado)" NÃO vira
    "Homologado". tipo="grupo" quando o número começa com "GRUPO "."""
    aba = pagina.get_by_text("Itens", exact=True).first
    if await aba.count() > 0:
        await aba.click()
        await pagina.wait_for_timeout(1500)

    resultado: dict[str, dict] = {}
    pagina_num = 1
    while True:
        cartoes = pagina.locator("app-identificacao-e-fase-item")
        n = await cartoes.count()
        for i in range(n):
            cartao = cartoes.nth(i)
            info = await cartao.evaluate("""
            (el) => {
                const numEl = el.querySelector('.dots span');
                const descEl = el.querySelector('.dots span.text-uppercase, .dots span.pl-1');
                const sitEl = el.querySelector('[data-test="situacao-item"]');
                return {
                    numero: numEl ? numEl.innerText.trim() : null,
                    descricao: descEl ? descEl.innerText.trim() : null,
                    situacao: sitEl ? sitEl.innerText.trim() : null,
                };
            }
            """)
            if info["numero"]:
                eh_grupo = bool(re.match(r"^GRUPO\s+\d+", info["numero"], re.IGNORECASE))
                resultado[info["numero"]] = {
                    "descricao": info["descricao"],
                    "situacao": info["situacao"],
                    "tipo": "grupo" if eh_grupo else "item",
                }

        log(f"    Itens: página {pagina_num} — {n} card(s), total acumulado {len(resultado)}")

        proximo = pagina.locator("p-paginator .p-paginator-next").first
        if await proximo.count() == 0:
            break
        desabilitado = await proximo.get_attribute("disabled")
        aria_dis = await proximo.get_attribute("aria-disabled")
        if desabilitado is not None or aria_dis == "true":
            break
        await proximo.scroll_into_view_if_needed()
        await proximo.click()
        await pagina.wait_for_timeout(1500)
        pagina_num += 1
        if pagina_num > 30:  # trava de segurança, nunca deveria chegar aqui
            log("    AVISO: mais de 30 páginas, parando por segurança.")
            break

    return resultado


def cruzar(pregao: str, uasg: str, fase: str | None,
           itens_situacao: dict[str, dict], fornecedores: list[dict]) -> dict:
    """Cruza pela chave (pregao, numero_item). Não inventa nada: item sem
    fornecedor fica com fornecedor=None; fornecedor sem situação (não
    deveria acontecer, mas se acontecer) fica situacao=None.

    Um item PODE ter mais de um fornecedor (normal em Registro de Preços --
    mais de uma empresa registrada pro mesmo item) -- por isso o resultado
    é uma linha por (item, fornecedor), não um dict 1:1 que descartaria o
    segundo fornecedor silenciosamente."""
    def norm(n) -> str:
        s = str(n or "").strip()
        return s.zfill(5) if s.isdigit() else s

    item_para_fornecedores: dict[str, list[dict]] = {}
    for forn in fornecedores:
        for item in forn.get("itens", []):
            numero = norm(item.get("numero_item"))
            if not numero:
                continue
            item_para_fornecedores.setdefault(numero, []).append({
                "fornecedor": forn.get("fornecedor"),
                "cnpj": forn.get("cnpj"),
            })

    # itens_situacao (aba Itens) também precisa passar pelo norm() -- vem
    # sem zero-padding ("1", não "00001"), senão nenhuma chave bate com o
    # lado dos fornecedores (bug real, já aconteceu: 0 cruzados por causa
    # só disso, não porque os dados estivessem errados).
    itens_situacao_norm = {norm(k): v for k, v in itens_situacao.items()}

    registros = []
    inconsistencias = []
    itens_com_mais_de_um_fornecedor = 0

    todos_numeros = set(itens_situacao_norm.keys()) | set(item_para_fornecedores.keys())
    for numero in sorted(todos_numeros):
        sit_info = itens_situacao_norm.get(numero)
        forns_info = item_para_fornecedores.get(numero) or [None]

        if len(forns_info) > 1:
            itens_com_mais_de_um_fornecedor += 1

        for forn_info in forns_info:
            registro = {
                "pregao": pregao,
                "uasg": uasg,
                "item": numero,
                "descricao": sit_info["descricao"] if sit_info else None,
                "fornecedor": forn_info["fornecedor"] if forn_info else None,
                "cnpj": forn_info["cnpj"] if forn_info else None,
                "situacao": sit_info["situacao"] if sit_info else None,
                "fase": fase,
            }
            registros.append(registro)

            problemas = []
            if not registro["fornecedor"]:
                problemas.append("fornecedor não encontrado")
            if not registro["cnpj"]:
                problemas.append("CNPJ não encontrado")
            if not registro["situacao"]:
                problemas.append("situação não encontrada")
            if not registro["descricao"]:
                problemas.append("descrição não encontrada (item só apareceu na aba Fornecedores)")
            if problemas:
                inconsistencias.append({"item": numero, "problemas": problemas})

    return {
        "pregao": pregao,
        "total_itens_aba_itens": len(itens_situacao),
        "total_itens_com_fornecedor": len(item_para_fornecedores),
        "itens_com_mais_de_um_fornecedor": itens_com_mais_de_um_fornecedor,
        "registros": registros,
        "inconsistencias": inconsistencias,
    }


async def processar_pregao(page, context, numero_ano: str, uasg: str = "160082") -> dict:
    log(f"\n=== Pregão {numero_ano} ===")
    pagina = await _ir_para_pregao(page, context, numero_ano)
    log(f"  URL: {pagina.url}")

    log("  Capturando aba Itens (situação real, com paginação)...")
    itens_situacao = await capturar_itens_situacao(pagina)
    log(f"  Total de itens capturados na aba Itens: {len(itens_situacao)}")

    # fase (se já tiver sido capturada antes -- Etapa 2.5)
    import sqlite3
    from comum import DB_PATH
    fase = None
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("SELECT fase FROM pregoes_fase WHERE pregao=?", (numero_ano,))
        row = cur.fetchone()
        fase = row[0] if row else None
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    log("  Capturando aba Fornecedores (versão leve, funciona em qualquer fase)...")
    fornecedores = await capturar_todos_fornecedores_leve(pagina)
    log(f"  Total de fornecedores com itens habilitados: {len(fornecedores)}")

    return cruzar(numero_ano, uasg, fase, itens_situacao, fornecedores)


async def main():
    pregoes_alvo = sys.argv[1:] or ["90020/2026", "13/2026"]

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = context.pages[0]
        await page.bring_to_front()

        resultados = []
        for numero_ano in pregoes_alvo:
            try:
                r = await processar_pregao(page, context, numero_ano)
                resultados.append(r)
            except Exception as e:
                log(f"  ERRO processando {numero_ano}: {e}")
                resultados.append({"pregao": numero_ano, "erro": str(e)})

        with open("_cruzamento_resultado.json", "w", encoding="utf-8") as f:
            json.dump(resultados, f, ensure_ascii=False, indent=2)

        print("\n" + "=" * 60)
        print("RELATÓRIO FINAL — ETAPA 2.9")
        print("=" * 60)
        for r in resultados:
            if "erro" in r:
                print(f"\nPregão {r['pregao']}: ERRO — {r['erro']}")
                continue
            regs = r["registros"]
            com_forn = sum(1 for x in regs if x["fornecedor"])
            sem_forn = len(regs) - com_forn
            com_cnpj = sum(1 for x in regs if x["cnpj"])
            sem_cnpj = len(regs) - com_cnpj
            com_sit = sum(1 for x in regs if x["situacao"])
            sem_sit = len(regs) - com_sit
            cruzados_ok = sum(1 for x in regs if x["fornecedor"] and x["cnpj"] and x["situacao"] and x["descricao"])

            print(f"\nPregão {r['pregao']}:")
            print(f"  Total de itens (aba Itens): {r['total_itens_aba_itens']}")
            print(f"  Total de itens com fornecedor (aba Fornecedores): {r['total_itens_com_fornecedor']}")
            print(f"  Itens com mais de um fornecedor: {r['itens_com_mais_de_um_fornecedor']}")
            print(f"  Registros no cruzamento (1 por item+fornecedor): {len(regs)}")
            print(f"  Com fornecedor: {com_forn} | Sem fornecedor: {sem_forn}")
            print(f"  Com CNPJ: {com_cnpj} | Sem CNPJ: {sem_cnpj}")
            print(f"  Com situação: {com_sit} | Sem situação: {sem_sit}")
            print(f"  Corretamente cruzados (tudo presente): {cruzados_ok}")
            print(f"  Inconsistências: {len(r['inconsistencias'])}")
            for inc in r["inconsistencias"][:10]:
                print(f"    [!] Item {inc['item']}: {', '.join(inc['problemas'])}")
            if len(r["inconsistencias"]) > 10:
                print(f"    ... e mais {len(r['inconsistencias'])-10}")

        print("\nResultado completo salvo em _cruzamento_resultado.json")


if __name__ == "__main__":
    asyncio.run(main())
