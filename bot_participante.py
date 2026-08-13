import asyncio
import sys
import traceback
from datetime import date
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from gspread.utils import rowcol_to_a1
from playwright.async_api import async_playwright

from comum import (
    abrir_chrome,
    clear_com_retry,
    conectar_google,
    get_or_create_worksheet,
    log,
    normalizar_espacos,
    parse_data_br,
    salvar_sqlite,
    update_com_retry,
)

# =========================================================
# BOT PARTICIPAÇÃO PMB - Atas de Registro de Preços
# =========================================================
# - Fase 1: varre https://contratos.sistema.gov.br/arp (lista ja
#   vem restrita as atas relevantes para a UASG logada, tanto como
#   Gerenciadora quanto como Participante) e filtra só as atas
#   onde a PMB é Participante (Gerenciadora já é coberta pelo
#   Bot comprasnet .py em abas próprias)
# - Fase 2: abre o detalhe de cada ata filtrada e extrai a tabela
#   "Item da ata" (fornecedor, quantidade, valor unitario, valor
#   total etc.) -> mesmo nivel de detalhe das abas de pregao do
#   Bot comprasnet
# - Uma linha por item na aba PARTICIPAÇÃO (nao cria aba por ata)
# - Nao deleta nada na planilha: apenas limpa e reescreve a aba
#   PARTICIPAÇÃO por completo a cada execucao
# - Abre o proprio Chrome (perfil de automacao) no inicio, para nao
#   depender de uma instancia do Chrome aberta por outro processo
# - O detalhe de cada ata (tabela "Item da ata") e buscado via HTTP
#   direto em paralelo (reaproveitando os cookies da sessao logada),
#   em vez de navegar pagina por pagina no navegador - mesma tecnica
#   usada no Bot comprasnet rapido.py
# - Salva progresso parcial periodicamente (checkpoint), para nao
#   perder tudo se a execucao for interrompida no meio
# =========================================================

URL_HOME = "https://contratos.sistema.gov.br"
URL_ARP = "https://contratos.sistema.gov.br/arp"

HTTP_CONCORRENCIA = 6  # requisições simultâneas de detalhe de ata

ABA_PARTICIPACAO = "PARTICIPAÇÃO"

ITEM_HEADERS = [
    "Papel PMB",
    "Situação Ata",
    "Ata",
    "Unidade Gerenciadora da Ata",
    "Vigência Início",
    "Vigência Fim",
    "Compra",
    "CNPJ Fornecedor",
    "Fornecedor (Classificação)",
    "Número Item",
    "Descrição Item",
    "Quantidade Registrada",
    "Valor Unitário",
    "Valor Total",
    "Qtd. Limite Adesão",
    "Qtd. Limite Adesão Informada na Compra",
    "Aceita Adesão",
    "Link detalhe",
    "Link PNCP",
    "Observação",
]

LIMITE_SEGURANCA_PAGINAS = 80
CHECKPOINT_A_CADA = 40


def vigencia_ativa(vig_fim_str: str) -> bool:
    dt = parse_data_br(vig_fim_str)
    return dt is not None and dt >= date.today()


# =========================================================
# PORTAL - LISTA DE ATAS (ARP)
# =========================================================

async def coletar_pagina_atual(page) -> list[dict]:
    linhas = await page.query_selector_all("#crudTable tbody tr")
    registros = []

    for linha in linhas:
        try:
            cels = await linha.query_selector_all("td")

            if len(cels) < 7:
                continue

            textos = [normalizar_espacos(await c.inner_text()) for c in cels]

            link_detalhe = ""
            anchors = await cels[-1].query_selector_all("a")

            for a in anchors:
                href = await a.get_attribute("href")

                if href and "/show" in href:
                    link_detalhe = urljoin(URL_HOME, href)
                    break

            registros.append({
                "situacao": textos[0],
                "ata": textos[1],
                "unidade_gerenciadora": textos[2],
                "papel": textos[3],
                "vig_inicio": textos[4],
                "vig_fim": textos[5],
                "compra": textos[6],
                "link": link_detalhe,
            })

        except Exception as e:
            log(f"Erro lendo linha da lista de atas: {e}")

    return registros


async def obter_total_paginas(page) -> int:
    try:
        info = await page.evaluate(
            """() => {
                const $ = window.jQuery;
                const dt = $('#crudTable').DataTable();
                const i = dt.page.info();
                return {pages: i.pages, recordsDisplay: i.recordsDisplay};
            }"""
        )
        log(f"Total de atas relevantes para a PMB (gerenciadora + participante): {info.get('recordsDisplay')}")
        return int(info.get("pages") or 1)
    except Exception as e:
        log(f"Falha ao ler total de páginas via DataTable API: {e}")
        return 1


async def avancar_pagina(page, tentativas=3) -> bool:
    for tentativa in range(1, tentativas + 1):
        try:
            seta = page.locator("#crudTable_paginate > a:has(i.fa-angle-right)")

            if await seta.count() == 0:
                return False

            async with page.expect_response(lambda r: "arp/search" in r.url, timeout=15000):
                await seta.first.click()

            await page.wait_for_timeout(500)
            return True

        except Exception as e:
            log(f"Tentativa {tentativa}/{tentativas} de avançar página falhou: {e}")
            await page.wait_for_timeout(1500)

    return False


async def coletar_todas_atas_pmb(page) -> list[dict]:
    log(f"Indo para lista de Atas de Registro de Preços: {URL_ARP}")
    await page.goto(URL_ARP, wait_until="domcontentloaded")
    await page.wait_for_selector("#crudTable tbody tr", timeout=25000)
    await page.wait_for_timeout(1200)

    total_paginas = await obter_total_paginas(page)
    total_paginas = min(total_paginas, LIMITE_SEGURANCA_PAGINAS) if total_paginas else 1

    todos = []
    vistos = set()

    for pagina in range(1, total_paginas + 1):
        registros = await coletar_pagina_atual(page)
        novos = 0

        for r in registros:
            chave = r["link"] or f"{r['ata']}|{r['compra']}"

            if chave not in vistos:
                vistos.add(chave)
                todos.append(r)
                novos += 1

        log(f"Lista de atas - página {pagina}/{total_paginas}: {len(registros)} linha(s), {novos} nova(s). Total acumulado: {len(todos)}")

        if pagina < total_paginas:
            avancou = await avancar_pagina(page)

            if not avancou:
                log(f"Não foi possível avançar da página {pagina}. Parando aqui (dados coletados até agora serão usados).")
                break

    return todos


# =========================================================
# PORTAL - DETALHE DA ATA VIA HTTP (rápido, em paralelo)
# =========================================================

def extrair_link_pncp_html(soup: BeautifulSoup) -> str:
    """Le a URL do botao 'PNCP' na secao Acoes do detalhe da ata."""
    try:
        link = soup.select_one("a[title='PNCP']")

        if link:
            href = link.get("href")
            return urljoin(URL_HOME, href) if href else ""

    except Exception:
        pass

    return ""


async def extrair_itens_da_ata_via_http(client: httpx.AsyncClient, sem: asyncio.Semaphore, url_detalhe: str) -> tuple[list[dict], str, str]:
    """Retorna (lista_de_itens, observacao, link_pncp). observacao vazia = sucesso."""
    async with sem:
        try:
            resp = await client.get(url_detalhe, timeout=30.0)
        except Exception as e:
            return [], f"Falha ao abrir detalhe: {e}", ""

    if "/login" in str(resp.url):
        return [], "Sessão expirada (redirecionado para login)", ""

    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}", ""

    soup = BeautifulSoup(resp.text, "html.parser")
    link_pncp = extrair_link_pncp_html(soup)

    try:
        tabela_itens = None

        for linha in soup.select("table > tbody > tr"):
            cels = linha.find_all("td", recursive=False)

            if len(cels) < 2:
                continue

            rotulo = normalizar_espacos(cels[0].get_text()).rstrip(":")

            if rotulo == "Item da ata":
                tabela_itens = cels[1].find("table")
                break

        if tabela_itens is None:
            return [], "Sem tabela 'Item da ata' no detalhe", link_pncp

        linhas_item = tabela_itens.select("tbody tr")
        itens = []

        for linha in linhas_item:
            cels = linha.find_all("td")
            valores = [normalizar_espacos(c.get_text()) for c in cels]

            if not any(valores):
                continue

            def campo(idx):
                return valores[idx] if idx < len(valores) else ""

            itens.append({
                "cnpj": campo(0),
                "fornecedor": campo(1),
                "numero": campo(2),
                "descricao": campo(3),
                "quantidade": campo(4),
                "valor_unitario": campo(5),
                "valor_total": campo(6),
                "qtd_limite_adesao": campo(7),
                "qtd_limite_adesao_informada": campo(8),
                "aceita_adesao": campo(9),
            })

        if not itens:
            return [], "Tabela 'Item da ata' presente mas sem linhas", link_pncp

        return itens, "", link_pncp

    except Exception as e:
        return [], f"Falha ao extrair itens: {e}", link_pncp


async def _processar_ata_via_http(client: httpx.AsyncClient, sem: asyncio.Semaphore, ata: dict) -> tuple[list[dict], str, str]:
    if not ata["link"]:
        return [], "Sem link de detalhe", ""

    return await extrair_itens_da_ata_via_http(client, sem, ata["link"])


async def coletar_atas_via_http(page, atas: list[dict]) -> list:
    """Busca o detalhe (itens + link PNCP) de cada ata via HTTP direto, em
    paralelo (limitado por HTTP_CONCORRENCIA), reaproveitando os cookies da
    sessão logada no Chrome. Retorna uma lista de tuplas
    (itens, observacao, link_pncp) na mesma ordem de `atas`."""
    cookies_pw = await page.context.cookies()
    cookies = {c["name"]: c["value"] for c in cookies_pw}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9",
    }

    sem = asyncio.Semaphore(HTTP_CONCORRENCIA)

    async with httpx.AsyncClient(cookies=cookies, headers=headers, follow_redirects=True) as client:
        tarefas = [_processar_ata_via_http(client, sem, ata) for ata in atas]
        return await asyncio.gather(*tarefas)


# =========================================================
# PLANILHA - ABA PARTICIPAÇÃO
# =========================================================

def montar_linhas(registros_com_itens: list[dict]) -> list[list]:
    linhas = [ITEM_HEADERS]

    for r in registros_com_itens:
        base = [
            r["papel"], r["situacao"], r["ata"], r["unidade_gerenciadora"],
            r["vig_inicio"], r["vig_fim"], r["compra"],
        ]

        if r["itens"]:
            for item in r["itens"]:
                linhas.append(base + [
                    item["cnpj"], item["fornecedor"], item["numero"], item["descricao"],
                    item["quantidade"], item["valor_unitario"], item["valor_total"],
                    item["qtd_limite_adesao"], item["qtd_limite_adesao_informada"], item["aceita_adesao"],
                    r["link"], r.get("link_pncp", ""), "",
                ])
        else:
            linhas.append(base + ["", "", "", "", "", "", "", "", "", "", r["link"], r.get("link_pncp", ""), r["observacao"]])

    return linhas


def gravar_aba(planilha, nome_aba: str, linhas: list[list]) -> None:
    ws = get_or_create_worksheet(
        planilha, nome_aba,
        rows=max(2000, len(linhas) + 50), cols=len(ITEM_HEADERS) + 2
    )
    clear_com_retry(ws)

    fim = rowcol_to_a1(len(linhas), len(ITEM_HEADERS))
    update_com_retry(ws, f"A1:{fim}", linhas)

    salvar_sqlite("participacao_itens", linhas[0], linhas[1:])


def salvar_participacao(planilha, registros_com_itens: list[dict]) -> None:
    linhas = montar_linhas(registros_com_itens)
    gravar_aba(planilha, ABA_PARTICIPACAO, linhas)
    log(f"Aba '{ABA_PARTICIPACAO}' atualizada: {len(registros_com_itens)} ata(s), {len(linhas) - 1} linha(s) de item.")


# =========================================================
# ORQUESTRAÇÃO PRINCIPAL
# =========================================================

async def main():
    try:
        abrir_chrome()
        planilha = conectar_google()

        async with async_playwright() as p:
            log("Conectando ao Chrome via CDP...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")

            if not browser.contexts:
                raise RuntimeError(
                    "Nenhum contexto encontrado no Chrome. "
                    "Abra o Chrome com --remote-debugging-port=9222 --user-data-dir=C:\\chrome-real antes de rodar."
                )

            context = browser.contexts[0]
            page = await context.new_page()  # aba nova e dedicada, nao reaproveita abas ja abertas

            try:
                atas_todas = await coletar_todas_atas_pmb(page)
                log(f"Total de atas na lista: {len(atas_todas)}")

                gerenciadora = sum(1 for r in atas_todas if "gerenciadora" in r["papel"].lower())
                participante = sum(1 for r in atas_todas if "participante" in r["papel"].lower())
                log(f"Como Gerenciadora: {gerenciadora} | Como Participante: {participante}")

                atas_participante = [r for r in atas_todas if "participante" in r["papel"].lower()]
                log(f"Filtrando para só Participante (Gerenciadora já é coberta pelo Bot comprasnet): {len(atas_participante)} atas.")

                atas = [
                    r for r in atas_participante
                    if r["situacao"].strip().lower() == "ativa" and vigencia_ativa(r["vig_fim"])
                ]
                log(f"Filtrando para só situação Ativa e vigência em dia (Vig. Fim >= hoje): {len(atas)} de {len(atas_participante)} atas.")

                log(f"Buscando detalhe de {len(atas)} ata(s) via HTTP (concorrência={HTTP_CONCORRENCIA})...")
                resultados = await coletar_atas_via_http(page, atas)

                registros_com_itens = []

                for i, (ata, (itens, observacao, link_pncp)) in enumerate(zip(atas, resultados), start=1):
                    registros_com_itens.append({**ata, "itens": itens, "observacao": observacao, "link_pncp": link_pncp})

                    if i % 10 == 0 or i == len(atas):
                        log(f"Detalhe {i}/{len(atas)} processado: {ata['ata']} ({len(itens)} item(ns)) {('- ' + observacao) if observacao else ''}")

                    if i % CHECKPOINT_A_CADA == 0:
                        log(f"Checkpoint: salvando progresso parcial ({i}/{len(atas)} atas processadas)...")
                        salvar_participacao(planilha, registros_com_itens)

            finally:
                await page.close()

            log("Salvando resultado final na planilha...")
            salvar_participacao(planilha, registros_com_itens)

        log("Processo finalizado com sucesso.")

    except Exception as e:
        log(f"ERRO FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
