"""
capturar_grupo.py - Etapa 2.10: captura os itens-filhos de um GRUPO
(número + descrição, SEM situação própria -- confirmado na Etapa 2.9.1
que só o grupo tem [data-test="situacao-item"], os itens-filhos não).

Caminho usado: clica o chevron de expandir na PRÓPRIA linha do grupo, na
aba "Itens" -- não precisa de fornecedor nenhum (caminho descoberto na
2.9.1, phase-independent, sem a fragilidade de coletar_fornecedores_itens
em Fase Recursal).
"""

from __future__ import annotations

import re

from comum import log


async def _linha_do_grupo(pagina, numero_grupo_texto: str):
    """numero_grupo_texto: ex. 'GRUPO 1' (como aparece na aba Itens).

    Container é .cp-grupo-expansivel -- confirmado ao vivo que NÃO é o
    mesmo .cp-item-expansivel usado pra linha de fornecedor (achar isso
    errado foi o motivo do primeiro teste desta etapa ter dado 0 itens)."""
    return pagina.locator("div.cp-grupo-expansivel:visible", has_text=numero_grupo_texto).first


async def _expandir_grupo_na_aba_itens(pagina, linha_grupo) -> bool:
    chevron = linha_grupo.locator("button").last
    if await chevron.count() == 0:
        return False
    await chevron.scroll_into_view_if_needed()
    await chevron.click()
    await pagina.wait_for_timeout(1500)
    return True


async def capturar_itens_do_grupo(pagina, numero_grupo_texto: str) -> list[dict]:
    """Retorna [{"numero_item":..., "descricao":...}, ...] -- nunca inclui
    situação (item-filho de grupo não tem situação própria, ver Etapa
    2.9.1). Pagina até o fim."""
    aba = pagina.get_by_text("Itens", exact=True).first
    if await aba.count() > 0:
        await aba.click()
        await pagina.wait_for_timeout(1500)

    linha_grupo = await _linha_do_grupo(pagina, numero_grupo_texto)
    if await linha_grupo.count() == 0:
        log(f"    ⚠ Não achei a linha do {numero_grupo_texto} na aba Itens.")
        return []

    if not await _expandir_grupo_na_aba_itens(pagina, linha_grupo):
        log(f"    ⚠ Não consegui expandir {numero_grupo_texto}.")
        return []

    # Achado ao vivo: os itens do grupo, ao expandir, NÃO ficam aninhados
    # dentro do container .cp-grupo-expansivel -- viram cards soltos na
    # PÁGINA, logo depois do card do grupo. Por isso a leitura (e o
    # paginador) operam no nível da página, coletando só os cards que
    # aparecem DEPOIS do card do grupo-alvo e ANTES do próximo "GRUPO ".
    itens: list[dict] = []
    pagina_num = 1
    while True:
        cartoes = pagina.locator("app-identificacao-e-fase-item")
        n = await cartoes.count()
        coletando = False
        novos_nesta_pagina = 0
        for i in range(n):
            numero = await cartoes.nth(i).evaluate(
                "el => { const s = el.querySelector('.dots span'); return s ? s.innerText.trim() : null; }"
            )
            if not numero:
                continue
            if numero == numero_grupo_texto:
                coletando = True
                continue
            if numero.upper().startswith("GRUPO"):
                if coletando:
                    break  # chegou no próximo grupo -- para de coletar
                continue
            if coletando and re.fullmatch(r"\d+", numero):
                descricao = await cartoes.nth(i).evaluate(
                    "el => { const s = el.querySelector('.dots span.text-uppercase'); return s ? s.innerText.trim() : null; }"
                )
                itens.append({"numero_item": numero, "descricao": descricao})
                novos_nesta_pagina += 1

        log(f"    {numero_grupo_texto}: página {pagina_num} — {novos_nesta_pagina} card(s), total acumulado {len(itens)}")

        proximo = pagina.locator("p-paginator .p-paginator-next:visible").first
        if await proximo.count() == 0:
            break
        desabilitado = await proximo.get_attribute("disabled")
        aria_dis = await proximo.get_attribute("aria-disabled")
        if desabilitado is not None or aria_dis == "true":
            break
        await proximo.scroll_into_view_if_needed()
        await proximo.click()
        await pagina.wait_for_timeout(1200)
        pagina_num += 1
        if pagina_num > 30:
            log(f"    AVISO: {numero_grupo_texto} tem mais de 30 páginas, parando por segurança.")
            break

    # NÃO tenta fechar o grupo de volta aqui -- se ficar aberto, o
    # paginador dele continua visível e compete com o do PRÓXIMO grupo
    # que for processado na mesma aba (bug real, já confirmado: GRUPO 2
    # parava em 10 de 73 porque o ".first" paginador da página ainda era
    # o do GRUPO 1, já esgotado). Quem chama esta função deve navegar de
    # novo (aba/URL fresca) antes de processar o próximo grupo -- ver
    # motor_homologacao.py.

    return itens
