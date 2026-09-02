"""
capturar_itens_fornecedor_leve.py - Etapa 2.9 (revisão): versão leve de
coleta Fornecedor -> Itens, que só expande a linha do fornecedor (nunca
entra em cada item via "+"). Não pega Quantidade ofertada/Marca/Modelo
(bot_criar_ata.py continua sendo a fonte disso quando precisar) -- só
número do item, descrição e SITUAÇÃO REAL ([data-test="situacao-item"],
Etapa 2.8), que é tudo que o cruzamento Item×Fornecedor precisa.

Por que essa versão existe: coletar_fornecedores_itens() (bot_criar_ata.py)
entra em cada item pra pegar Quantidade/Marca/Modelo, e esse passo navega
pra uma tela que só existe corretamente na etapa "Adjudicação/Homologação"
-- na "Fase Recursal" o mesmo botão leva pra uma tela de "Recursos e
contrarrazões" diferente, e o fornecedor nunca é encontrado lá (confirmado
ao vivo no pregão 90020/2026, fornecedor 02.475.798/0002-34). A tabela
colapsada (sem entrar no item) já mostra número+descrição+situação, então
essa versão nunca precisa daquele passo -- funciona em qualquer fase.
"""

from __future__ import annotations

import re

from bot_criar_ata import _expandir_fornecedor, _limpar
from comum import log


async def _extrair_itens_da_tabela(tabela) -> tuple[list[dict], list[str]]:
    """Lê todas as <tr> da tabela JÁ VISÍVEL (sem clicar em nada dentro
    delas). Retorna (itens, avisos) -- avisos lista linhas que não
    bateram no padrão esperado (ex: linha de GRUPO), pra nunca sumir
    dado em silêncio."""
    linhas = tabela.locator("tbody tr")
    n = await linhas.count()
    itens = []
    avisos = []

    for j in range(n):
        tr = linhas.nth(j)
        info = await tr.evaluate(
            """(el) => {
                const numEl = el.querySelector('.dots span');
                const descEl = el.querySelector('.dots span.text-uppercase');
                const sitEl = el.querySelector('[data-test="situacao-item"]');
                return {
                    numero: numEl ? numEl.innerText.trim() : null,
                    descricao: descEl ? descEl.innerText.trim() : null,
                    situacao: sitEl ? sitEl.innerText.trim() : null,
                };
            }"""
        )
        numero = info.get("numero") or ""
        if re.fullmatch(r"\d+", numero):
            itens.append({
                "numero_item": numero,
                "descricao": _limpar(info.get("descricao") or ""),
                "situacao": info.get("situacao"),
            })
        else:
            texto = await tr.inner_text()
            avisos.append(f"linha não reconhecida (possível GRUPO): {texto[:80]!r}")

    return itens, avisos


async def _proximo_botao_da_tabela(tabela):
    """Paginador (p-paginator) que vem logo depois DESSA tabela
    especificamente -- cada tabela (melhor classificado / não é melhor
    classificado) tem o seu próprio, então não dá pra pegar só o
    ".first" da linha inteira (isso pegaria sempre o da primeira)."""
    paginador = tabela.locator("xpath=following-sibling::p-paginator[1]").first
    if await paginador.count() == 0 or not await paginador.is_visible():
        return None
    proximo = paginador.locator(".p-paginator-next").first
    if await proximo.count() == 0:
        return None
    if (await proximo.get_attribute("disabled")) is not None:
        return None
    if (await proximo.get_attribute("aria-disabled")) == "true":
        return None
    return proximo


async def capturar_itens_do_fornecedor(pagina, linha_fornecedor) -> dict:
    """`linha_fornecedor` já deve estar expandida (chama _expandir_fornecedor
    se ainda não estiver). Percorre TODAS as tabelas da linha (melhor
    classificado + não é melhor classificado) com paginação própria de
    cada uma. Retorna {"itens": [...], "avisos": [...]}."""
    expandiu = await _expandir_fornecedor(pagina, linha_fornecedor)
    if not expandiu:
        return {"itens": [], "avisos": ["não consegui expandir a linha do fornecedor"]}

    todos_itens = []
    todos_avisos = []

    tabelas = linha_fornecedor.locator("table")
    total_tabelas = await tabelas.count()

    for t in range(total_tabelas):
        tabela = tabelas.nth(t)
        pagina_num = 1
        while True:
            itens, avisos = await _extrair_itens_da_tabela(tabela)
            todos_itens.extend(itens)
            todos_avisos.extend(avisos)

            proximo = await _proximo_botao_da_tabela(tabela)
            if proximo is None:
                break
            await proximo.scroll_into_view_if_needed()
            await proximo.click()
            await pagina.wait_for_timeout(1200)
            pagina_num += 1
            if pagina_num > 30:
                todos_avisos.append(f"tabela {t}: mais de 30 páginas, parei por segurança")
                break

    return {"itens": todos_itens, "avisos": todos_avisos}


async def capturar_todos_fornecedores_leve(pagina, arquivo_progresso: str | None = None) -> list[dict]:
    """Equivalente leve de coletar_fornecedores_itens(), mas sem entrar em
    cada item -- só situação+descrição+número, sem quantidade/marca/modelo.
    Funciona em qualquer fase do pregão."""
    aba_fornecedores = pagina.get_by_text("Fornecedores", exact=True).first
    if await aba_fornecedores.count() > 0:
        await aba_fornecedores.click()
        await pagina.wait_for_timeout(2000)

    linhas = pagina.locator("div.cp-item-expansivel:visible")
    total = await linhas.count()
    log(f"Total de fornecedores na listagem: {total}")

    resultado = []
    import json
    from pathlib import Path

    for i in range(total):
        linha = linhas.nth(i)
        texto_fechado = await linha.inner_text()

        m_habilitados = re.search(r"Itens habilitados\s*\n?\s*(\d+) de (\d+)", texto_fechado)
        if not m_habilitados or m_habilitados.group(1) == "0":
            continue

        m_cnpj = re.search(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", texto_fechado)
        cnpj = m_cnpj.group(0) if m_cnpj else ""
        nome_match = re.search(r"\n([A-ZÀ-Ÿ0-9][^\n]*)\nItens habilitados", texto_fechado)
        nome = _limpar(nome_match.group(1)) if nome_match else ""

        log(f"  Fornecedor {i+1}/{total}: {cnpj} ({nome}) — {m_habilitados.group(0)}")

        try:
            captura = await capturar_itens_do_fornecedor(pagina, linha)
        except Exception as e:
            log(f"    ⚠ Falha no fornecedor {cnpj}: {e} — pulando, seguindo pro próximo.")
            continue

        for aviso in captura["avisos"]:
            log(f"    ⚠ {aviso}")

        resultado.append({"cnpj": cnpj, "fornecedor": nome, "itens": captura["itens"]})

        if arquivo_progresso:
            Path(arquivo_progresso).write_text(
                json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    return resultado
