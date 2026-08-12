import asyncio
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, date
from urllib.parse import urljoin

from gspread.utils import rowcol_to_a1
from playwright.async_api import async_playwright

from comum import (
    CHAVE_JSON,
    PLANILHA_ID,
    clear_com_retry,
    conectar_google,
    get_or_create_worksheet,
    log,
    normalizar_espacos,
    parse_data_br,
    update_com_retry,
)


# =========================================================
# BOT COMPRASNET / CONTRATOS GOV - V37 PROFISSIONAL
# =========================================================
# - Sem dashboard / Sem input / sem interrupção
# - Retry automático contra quota 429
# - Atualiza somente A:N nas abas dos pregões individuais
# - Preserva fórmulas e colunas manuais de O em diante
# - Varre TODOS os pregões encontrados no portal (2026, 2025 e 2024)
# - Atualiza saldo PMB e vigência mesmo se a aba já existir
# - Salva somente itens com vigência ativa
# - Gera aba 'BD_CONSOLIDADO' limpa para Banco de Dados
# - Limpeza profunda: Deleta abas inteiras OU apenas as linhas de itens
#   vencidos da planilha na data atual da execução.
# - Correção de Mapa (V37): Evita deleção de itens ativos por divergência de formatação "01" vs "1"
# - [MODIFICADO] Formatação visual via bot removida. O bot apenas insere os dados.
# =========================================================


# =========================================================
# CONFIG
# =========================================================

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PERFIL = r"C:\chrome-real"

URL_HOME = "https://contratos.sistema.gov.br"
URL_LISTA_BASE = "https://contratos.sistema.gov.br/compras?unidade_origem_id=9822"

ANOS_BUSCA = [2026, 2025, 2024]

ABAS_FIXAS = {
    "TESTE_BOT",
    "INDICE_PREGOES",
    "ERROS_BOT",
    "CONTROLE_BOT",
    "BD_CONSOLIDADO",
}

MODALIDADE_ALVO = "05 - Pregão"
UNIDADE_PMB = "160082 - PMB"

SOMENTE_VIGENCIA_ATIVA = True

PAUSA_SHEETS = 2.0
PAUSA_NAVEGACAO = 2.2
PAUSA_ITEM = 1.2

BOT_HEADERS = [
    "Número Item",
    "Tipo Item",
    "Descrição",
    "Descrição detalhada",
    "Número Ata",
    "Fornecedor",
    "Valor Unitário",
    "Total",
    "autorizada ",
    "Saldo",
    "Vig Início",
    "Vig Fim",
    "Faixa Vigência",
    "Link detalhe item",
]

BOT_COLS = len(BOT_HEADERS)
COL_FAIXA = "M"


# =========================================================
# LOG / UTIL
# =========================================================

def nome_limpo(txt: str) -> str:
    txt = (txt or "").strip()
    txt = txt.replace("/", "_")
    txt = re.sub(r"[^A-Za-z0-9_ -]", "", txt)
    txt = txt.replace(" ", "_")
    return txt[:90] if txt else "sem_nome"


def normalizar_nome_aba_pregao(numero_ano: str) -> str:
    return nome_limpo(numero_ano)


def montar_url_lista_por_ano(ano: int) -> str:
    return f"{URL_LISTA_BASE}&ano={ano}"


def extrair_numero(texto) -> float:
    s = str(texto or "").strip()

    if not s:
        return 0.0

    s = s.replace("R$", "").replace(" ", "")
    s = s.replace(".", "").replace(",", ".")

    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)

    if not m:
        return 0.0

    try:
        return float(m.group(0))
    except Exception:
        return 0.0


def faixa_vigencia(valor_data):
    dt = parse_data_br(valor_data)

    if not dt:
        return "SEM DATA"

    dias = (dt - date.today()).days

    if dias < 0:
        return "VENCIDO"
    if dias <= 30:
        return "ATÉ 1 MÊS"
    if dias <= 90:
        return "ATÉ 3 MESES"
    if dias <= 150:
        return "ATÉ 5 MESES"

    return "ACIMA DE 5 MESES"


def eh_item_vigente_estrito(item: dict) -> bool:
    fim = parse_data_br(item.get("Vig Fim", ""))
    return fim is not None and fim >= date.today()


def resumo_vigencia(itens: list[dict]) -> dict:
    datas = []
    vigentes = 0

    for item in itens:
        fim = parse_data_br(item.get("Vig Fim", ""))

        if fim:
            datas.append(fim)

            if fim >= date.today():
                vigentes += 1

    return {
        "itens_vigentes": vigentes,
        "menor_vig_fim": min(datas).strftime("%d/%m/%Y") if datas else "",
        "maior_vig_fim": max(datas).strftime("%d/%m/%Y") if datas else "",
    }


def item_valido(item: dict) -> tuple[bool, str]:
    numero = normalizar_espacos(item.get("Número Item", ""))
    vig_fim = normalizar_espacos(item.get("Vig Fim", ""))
    fornecedor = normalizar_espacos(item.get("Fornecedor", ""))
    ata = normalizar_espacos(item.get("Número Ata", ""))

    if not numero:
        return False, "Número Item vazio"

    if not vig_fim:
        return False, "Vig Fim vazia"

    if not parse_data_br(vig_fim):
        return False, f"Vig Fim inválida: {vig_fim}"

    if not fornecedor and not ata:
        return False, "Fornecedor e Número Ata vazios"

    return True, ""


def deduplicar_itens_por_link(itens: list[dict]) -> list[dict]:
    unicos = {}

    for item in itens:
        chave = normalizar_espacos(item.get("Link detalhe item", "")) or (
            f"{normalizar_espacos(item.get('Número Item', ''))}|"
            f"{normalizar_espacos(item.get('Número Ata', ''))}|"
            f"{normalizar_espacos(item.get('Fornecedor', ''))}"
        )

        if chave not in unicos:
            unicos[chave] = item

    def chave_ordem(x):
        try:
            return int(re.sub(r"\D", "", str(x.get("Número Item", ""))) or "999999")
        except Exception:
            return 999999

    return sorted(unicos.values(), key=chave_ordem)


def percentual_consumido(qtd_aut: float, qtd_saldo: float) -> float:
    if qtd_aut <= 0:
        return 0.0

    consumido = max(0.0, qtd_aut - qtd_saldo)

    return round((consumido / qtd_aut) * 100, 2)


def classificar_status_pregao(itens: list[dict]) -> str:
    if not itens:
        return "SEM DADOS"

    for item in itens:
        faixa = normalizar_espacos(item.get("Faixa Vigência", ""))
        saldo = extrair_numero(item.get("Saldo", "0"))

        if saldo == 0 or faixa in {"VENCIDO", "ATÉ 1 MÊS"}:
            return "CRÍTICO"

    for item in itens:
        faixa = normalizar_espacos(item.get("Faixa Vigência", ""))
        saldo = extrair_numero(item.get("Saldo", "0"))

        if (0 < saldo <= 10) or faixa in {"ATÉ 3 MESES", "ATÉ 5 MESES"}:
            return "ATENÇÃO"

    return "OK"


# =========================================================
# GOOGLE SHEETS COM RETRY
# =========================================================
# update_com_retry / clear_com_retry -> comum.py (usa PAUSA_SHEETS via pausa=)


# =========================================================
# CHROME / GOOGLE
# =========================================================

def abrir_chrome() -> None:
    if not os.path.exists(CHROME):
        raise FileNotFoundError(f"Chrome não encontrado em: {CHROME}")

    log("Abrindo Chrome com depuração...")

    subprocess.Popen([
        CHROME,
        "--remote-debugging-port=9222",
        f"--user-data-dir={PERFIL}"
    ])

    time.sleep(5)


def testar_google(planilha) -> None:
    ws = get_or_create_worksheet(planilha, "TESTE_BOT", rows=20, cols=5)

    clear_com_retry(ws)

    update_com_retry(
        ws,
        "A1",
        [["STATUS", "OK"], ["DATA", datetime.now().strftime("%d/%m/%Y %H:%M:%S")]]
    )

    log("Teste Google OK.")


# =========================================================
# CONTROLE / ERROS / ABAS
# =========================================================

def registrar_erro(planilha, contexto: str, pregao: str, item: str, mensagem: str) -> None:
    try:
        ws = get_or_create_worksheet(planilha, "ERROS_BOT", rows=5000, cols=6)
        valores = ws.get_all_values()

        if not valores:
            update_com_retry(
                ws,
                "A1",
                [["Data/Hora", "Contexto", "Pregão", "Item", "Mensagem"]]
            )

        ws.append_row(
            [datetime.now().strftime("%d/%m/%Y %H:%M:%S"), contexto, pregao, item, mensagem],
            value_input_option="USER_ENTERED"
        )
        time.sleep(0.5)

    except Exception as e:
        log(f"Falha ao registrar erro em ERROS_BOT: {e}")


def carregar_controle_bot(planilha) -> dict:
    controle = {}

    try:
        ws = get_or_create_worksheet(planilha, "CONTROLE_BOT", rows=5000, cols=5)
        valores = ws.get_all_values()

        if not valores:
            update_com_retry(
                ws,
                "A1",
                [["Pregão", "Status", "Última verificação"]]
            )
            return controle

        for linha in valores[1:]:
            if len(linha) >= 2:
                pregao = normalizar_espacos(linha[0])
                status = normalizar_espacos(linha[1])
                ultima = normalizar_espacos(linha[2]) if len(linha) > 2 else ""

                if pregao:
                    controle[pregao] = {
                        "status": status,
                        "ultima_verificacao": ultima
                    }

    except Exception as e:
        log(f"Falha ao carregar CONTROLE_BOT: {e}")

    return controle


def salvar_status_controle_bot(planilha, pregao: str, status: str) -> None:
    try:
        ws = get_or_create_worksheet(planilha, "CONTROLE_BOT", rows=5000, cols=5)
        valores = ws.get_all_values()

        if not valores:
            update_com_retry(
                ws,
                "A1",
                [["Pregão", "Status", "Última verificação"]]
            )
            valores = ws.get_all_values()

        linha_existente = None

        for idx, linha in enumerate(valores[1:], start=2):
            if len(linha) >= 1 and normalizar_espacos(linha[0]) == pregao:
                linha_existente = idx
                break

        agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        if linha_existente:
            update_com_retry(
                ws,
                f"A{linha_existente}:C{linha_existente}",
                [[pregao, status, agora]]
            )
        else:
            ws.append_row(
                [pregao, status, agora],
                value_input_option="USER_ENTERED"
            )
            time.sleep(0.6)

    except Exception as e:
        log(f"Falha ao salvar CONTROLE_BOT para {pregao}: {e}")


def listar_abas_existentes(planilha) -> dict:
    existentes = {}

    for ws in planilha.worksheets():
        nome = ws.title.strip()

        if nome in ABAS_FIXAS:
            continue

        if re.match(r"^\d+_\d{4}$", nome):
            existentes[nome] = ws

    return existentes


# =========================================================
# EXTRAÇÃO GENÉRICA
# =========================================================

async def extrair_por_rotulos(page, rotulos: list[str]) -> dict:
    dados = {}
    rows = await page.query_selector_all("table tr")

    for row in rows:
        try:
            cells = await row.query_selector_all("th, td")

            if len(cells) < 2:
                continue

            chave = normalizar_espacos(await cells[0].inner_text()).replace(":", "")
            valor = normalizar_espacos(await cells[1].inner_text())

            if chave in rotulos and chave not in dados:
                dados[chave] = valor

        except Exception:
            continue

    for r in rotulos:
        if r not in dados:
            dados[r] = ""

    return dados


# =========================================================
# PORTAL - LISTA DE PREGÕES
# =========================================================

async def extrair_registro_linha_pregao(linha, i: int, ano_alvo=None):
    cels = await linha.query_selector_all("td")

    if len(cels) < 4:
        return None

    textos = [normalizar_espacos(await c.inner_text()) for c in cels]
    texto_linha = " | ".join(textos)

    numero_ano_lista = ""

    for t in textos:
        m = re.search(r"\b\d{4,6}/\d{4}\b", t)

        if m:
            numero_ano_lista = m.group(0)
            break

    if not numero_ano_lista:
        return None

    if ano_alvo and f"/{ano_alvo}" not in numero_ano_lista:
        return None

    modalidade_ok = False
    modalidade = ""

    for t in textos:
        t_up = t.upper()

        if "05" in t_up and ("PREGÃO" in t_up or "PREGAO" in t_up):
            modalidade_ok = True
            modalidade = t
            break

    if not modalidade_ok:
        return None

    anchors = await linha.query_selector_all("a")
    hrefs = []

    for a in anchors:
        href = await a.get_attribute("href")

        if href:
            hrefs.append(urljoin(URL_HOME, href))

    if not hrefs:
        return None

    detalhe_url = hrefs[0] if len(hrefs) >= 1 else ""
    itens_url = hrefs[1] if len(hrefs) >= 2 else ""

    return {
        "linha": i,
        "texto_linha": texto_linha,
        "detalhe_url": detalhe_url,
        "itens_url": itens_url,
        "numero_ano_lista": numero_ano_lista,
        "modalidade": modalidade if modalidade else MODALIDADE_ALVO,
        "ano_busca": ano_alvo or "",
    }


async def avancar_pagina_compras(page, tentativas=3) -> bool:
    for tentativa in range(1, tentativas + 1):
        try:
            seta = page.locator("#crudTable_paginate > a:has(i.fa-angle-right)")

            if await seta.count() == 0:
                return False

            classes = await seta.first.get_attribute("class") or ""

            if "disabled" in classes:
                return False

            async with page.expect_response(lambda r: "compras/search" in r.url, timeout=30000):
                await seta.first.click()

            await page.wait_for_timeout(500)
            return True

        except Exception as e:
            log(f"Tentativa {tentativa}/{tentativas} de avançar página da lista de compras falhou: {e}")
            await page.wait_for_timeout(1500)

    return False


async def coletar_links_pregoes(page, url_lista=URL_LISTA_BASE, ano_alvo=None) -> list[dict]:
    log(f"Indo para lista de compras: {url_lista}")

    await page.goto(url_lista, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    await page.wait_for_selector("table tbody tr", timeout=20000)

    total_paginas = 1

    try:
        info_inicial = await page.evaluate(
            """() => {
                const $ = window.jQuery;
                const dt = $('#crudTable').DataTable();
                return dt.page.info();
            }"""
        )

        # O DataTables restaura a ultima pagina vista (via localStorage) em vez de
        # comecar da pagina 1. Forca voltar para a pagina 1 antes de coletar.
        if info_inicial.get("page", 0) != 0:
            log(f"Tabela restaurou na página {info_inicial.get('page') + 1}/{info_inicial.get('pages')}. Forçando volta para a página 1...")

            async with page.expect_response(lambda r: "compras/search" in r.url, timeout=20000):
                await page.evaluate(
                    """() => {
                        const $ = window.jQuery;
                        $('#crudTable').DataTable().page(0).draw('page');
                    }"""
                )

            await page.wait_for_timeout(800)

        info = await page.evaluate(
            """() => {
                const $ = window.jQuery;
                const dt = $('#crudTable').DataTable();
                const i = dt.page.info();
                return {pages: i.pages, recordsDisplay: i.recordsDisplay};
            }"""
        )
        total_paginas = int(info.get("pages") or 1)
        log(f"Total de registros na listagem (todas modalidades/anos misturados na página): {info.get('recordsDisplay')} em {total_paginas} página(s)")
    except Exception as e:
        log(f"Falha ao ler total de páginas da lista de compras: {e}")

    registros = []
    vistos = set()

    for pagina in range(1, total_paginas + 1):
        linhas = await page.query_selector_all("table tbody tr")

        for i, linha in enumerate(linhas, start=1):
            try:
                reg = await extrair_registro_linha_pregao(linha, i, ano_alvo)

                if not reg:
                    continue

                chave = reg["detalhe_url"] or reg["numero_ano_lista"]

                if chave in vistos:
                    continue

                vistos.add(chave)
                registros.append(reg)

            except Exception as e:
                log(f"Erro linha {i} (página {pagina}): {e}")

        if pagina < total_paginas:
            avancou = await avancar_pagina_compras(page)

            if not avancou:
                log(f"Não foi possível avançar da página {pagina}/{total_paginas} da lista de compras. Parando com o que foi coletado.")
                break

    log(f"Total de pregões encontrados no portal: {len(registros)}")

    return registros


async def coletar_links_pregoes_todos_anos(page) -> list[dict]:
    todos = []
    chaves = set()

    for ano in ANOS_BUSCA:
        try:
            log(f"Buscando pregões do ano {ano}...")
            url_ano = montar_url_lista_por_ano(ano)

            registros_ano = await coletar_links_pregoes(
                page,
                url_lista=url_ano,
                ano_alvo=ano
            )

            for reg in registros_ano:
                chave = reg.get("numero_ano_lista") or reg.get("detalhe_url")
                if chave and chave not in chaves:
                    chaves.add(chave)
                    todos.append(reg)

            log(f"Ano {ano}: {len(registros_ano)} pregões encontrados.")

        except Exception as e:
            log(f"Falha ao buscar ano {ano}: {e}")

    if not todos:
        log("Nenhum pregão por ano. Tentando URL base sem filtro de ano...")
        todos = await coletar_links_pregoes(page, url_lista=URL_LISTA_BASE)

    log(f"Total consolidado de pregões: {len(todos)}")

    return todos


# =========================================================
# PORTAL - LISTA DE ITENS COM PAGINAÇÃO
# =========================================================

async def coletar_links_itens(page, itens_url: str) -> tuple[list[dict], bool]:
    encontrados = []
    vistos = set()
    houve_erro_leitura = False

    await page.goto(itens_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    try:
        selects = await page.query_selector_all("select")

        for sel in selects:
            try:
                opcoes = await sel.query_selector_all("option")
                textos = [normalizar_espacos(await o.inner_text()) for o in opcoes]
                valores = [await o.get_attribute("value") for o in opcoes]

                escolhido = None

                for i, txt in enumerate(textos):
                    txt_low = txt.lower()

                    if txt_low in {"todos", "todas"} or "todos" in txt_low:
                        escolhido = valores[i] if i < len(valores) else None
                        break

                if escolhido is None:
                    for i, txt in enumerate(textos):
                        if txt in {"100", "100 registros por página", "100 registros"}:
                            escolhido = valores[i] if i < len(valores) else "100"
                            break

                if escolhido is None:
                    for i, txt in enumerate(textos):
                        if txt in {"50", "50 registros por página", "50 registros"}:
                            escolhido = valores[i] if i < len(valores) else "50"
                            break

                if escolhido is not None:
                    await sel.select_option(value=escolhido)
                    await page.wait_for_timeout(3000)
                    log(f"Quantidade por página ajustada para: {escolhido}")
                    break

            except Exception as e:
                log(f"Falha ao ajustar quantidade por página: {e}")

    except Exception:
        pass

    async def ler_pagina_atual():
        await page.wait_for_selector("table tbody tr", timeout=20000)
        await page.wait_for_timeout(1200)

        linhas = await page.query_selector_all("table tbody tr")
        qtd_novos = 0

        for linha in linhas:
            try:
                cels = await linha.query_selector_all("td")
                textos = [normalizar_espacos(await c.inner_text()) for c in cels]

                href = ""
                anchors = await linha.query_selector_all("a")

                for a in anchors:
                    h = await a.get_attribute("href")

                    if h:
                        href = urljoin(URL_HOME, h)
                        break

                if href and href not in vistos:
                    vistos.add(href)

                    encontrados.append({
                        "numero_lista": textos[0] if len(textos) > 0 else "",
                        "tipo_item_lista": textos[1] if len(textos) > 1 else "",
                        "descricao_lista": textos[2] if len(textos) > 2 else "",
                        "qtd_lista": textos[3] if len(textos) > 3 else "",
                        "detalhe_url": href,
                    })

                    qtd_novos += 1

            except Exception as e:
                nonlocal houve_erro_leitura
                houve_erro_leitura = True
                log(f"Erro lendo linha de item: {e}")

        return qtd_novos

    log("Coletando itens da página 1...")
    await ler_pagina_atual()

    paginas = {1}

    try:
        links_paginacao = await page.query_selector_all("a, button")

        for el in links_paginacao:
            try:
                txt = normalizar_espacos(await el.inner_text())

                if txt.isdigit():
                    paginas.add(int(txt))

            except Exception:
                pass

    except Exception:
        pass

    paginas = sorted(paginas)

    for pag in paginas:
        if pag == 1:
            continue

        log(f"Coletando itens da página {pag}...")

        clicou = False

        for _ in range(3):
            try:
                candidatos = [
                    page.locator(f"a:has-text('{pag}')").first,
                    page.locator(f"button:has-text('{pag}')").first,
                    page.locator(f"text='{pag}'").first,
                ]

                for botao in candidatos:
                    try:
                        if await botao.count() > 0 and await botao.is_visible():
                            await botao.click()
                            await page.wait_for_timeout(2500)
                            clicou = True
                            break

                    except Exception:
                        pass

                if clicou:
                    break

            except Exception:
                pass

            await page.wait_for_timeout(1500)

        if not clicou:
            log(f"Não consegui abrir a página {pag}.")
            continue

        total_antes = len(vistos)
        novos = await ler_pagina_atual()
        log(f"Página {pag}: +{len(vistos) - total_antes} itens novos")

        if novos == 0:
            await page.wait_for_timeout(1000)

    tentativas_next = 0

    while tentativas_next < 10:
        try:
            total_antes = len(vistos)

            seletores_next = [
                "a[aria-label='Next']",
                "li.paginate_button.next:not(.disabled) a",
                ".pagination .next:not(.disabled) a",
                "a:has-text('›')",
                "a:has-text('>')",
                "button:has-text('›')",
                "button:has-text('>')",
                "a:has-text('Próximo')",
                "button:has-text('Próximo')",
                "a:has-text('Proximo')",
                "button:has-text('Proximo')",
            ]

            clicou = False

            for sel in seletores_next:
                try:
                    btn = page.locator(sel).first

                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(2500)
                        clicou = True
                        break

                except Exception:
                    pass

            if not clicou:
                break

            novos = await ler_pagina_atual()

            if len(vistos) == total_antes and novos == 0:
                tentativas_next += 1
            else:
                tentativas_next = 0

        except Exception:
            break

    encontrados.sort(
        key=lambda x: int(re.sub(r"\D", "", str(x.get("numero_lista", ""))) or "999999")
    )

    log(f"Total final de itens coletados: {len(encontrados)}")

    return encontrados, houve_erro_leitura


# =========================================================
# PORTAL - EXTRAÇÃO DE DADOS DO ITEM
# =========================================================

async def extrair_ata_fornecedor(page) -> dict:
    resultado = {
        "numero_ata": "",
        "fornecedor": ""
    }

    tabelas = await page.query_selector_all("table")

    for tabela in tabelas:
        try:
            linhas = await tabela.query_selector_all("tr")

            if len(linhas) < 2:
                continue

            header_cells = await linhas[0].query_selector_all("th, td")
            headers = [normalizar_espacos(await c.inner_text()) for c in header_cells]
            headers_norm = [h.lower().replace(".", "") for h in headers]

            if not any(h == "número" or h == "numero" for h in headers_norm):
                continue

            if not any("unidade gerenciadora" in h for h in headers_norm):
                continue

            if not any("fornecedor" in h for h in headers_norm):
                continue

            idx_numero = next(
                i for i, h in enumerate(headers_norm)
                if h == "número" or h == "numero"
            )

            idx_fornecedor = next(
                i for i, h in enumerate(headers_norm)
                if "fornecedor" in h
            )

            for linha in linhas[1:]:
                cels = await linha.query_selector_all("th, td")

                if len(cels) <= max(idx_numero, idx_fornecedor):
                    continue

                numero_ata = normalizar_espacos(await cels[idx_numero].inner_text())
                fornecedor = normalizar_espacos(await cels[idx_fornecedor].inner_text())

                if numero_ata or fornecedor:
                    resultado["numero_ata"] = numero_ata
                    resultado["fornecedor"] = fornecedor
                    return resultado

        except Exception:
            continue

    return resultado


async def extrair_quantidade_valor_unitario(page) -> dict:
    resultado = {
        "quantidade": "",
        "valor_unitario": "",
        "vig_inicio": "",
        "vig_fim": "",
    }

    tabelas = await page.query_selector_all("table")

    for tabela in tabelas:
        try:
            linhas = await tabela.query_selector_all("tr")

            if len(linhas) < 2:
                continue

            header_cells = await linhas[0].query_selector_all("th, td")
            headers = [normalizar_espacos(await c.inner_text()) for c in header_cells]
            headers_norm = [h.lower().replace(".", "") for h in headers]

            tem_ini = any("vigência início" in h or "vigencia inicio" in h for h in headers_norm)
            tem_fim = any("vigência fim" in h or "vigencia fim" in h for h in headers_norm)
            tem_qtd = any("quantidade" in h for h in headers_norm)
            tem_valor = any("valor unitário" in h or "valor unitario" in h for h in headers_norm)

            if not (tem_ini and tem_fim and tem_qtd and tem_valor):
                continue

            idx_ini = next(
                i for i, h in enumerate(headers_norm)
                if "vigência início" in h or "vigencia inicio" in h
            )

            idx_fim = next(
                i for i, h in enumerate(headers_norm)
                if "vigência fim" in h or "vigencia fim" in h
            )

            idx_qtd = next(
                i for i, h in enumerate(headers_norm)
                if "quantidade" in h
            )

            idx_vu = next(
                i for i, h in enumerate(headers_norm)
                if "valor unitário" in h or "valor unitario" in h
            )

            for linha in linhas[1:]:
                cels = await linha.query_selector_all("th, td")

                if len(cels) <= max(idx_ini, idx_fim, idx_qtd, idx_vu):
                    continue

                resultado["vig_inicio"] = normalizar_espacos(await cels[idx_ini].inner_text())
                resultado["vig_fim"] = normalizar_espacos(await cels[idx_fim].inner_text())
                resultado["quantidade"] = normalizar_espacos(await cels[idx_qtd].inner_text())
                resultado["valor_unitario"] = normalizar_espacos(await cels[idx_vu].inner_text())

                return resultado

        except Exception:
            continue

    return resultado


async def pegar_qtds_pmb(page) -> dict:
    resultado = {
        "qtd_autorizada": "0",
        "qtd_saldo": "0"
    }

    tabelas = await page.query_selector_all("table")

    for tabela in tabelas:
        try:
            linhas = await tabela.query_selector_all("tr")

            if len(linhas) < 2:
                continue

            header_cells = await linhas[0].query_selector_all("th, td")
            headers = [normalizar_espacos(await c.inner_text()) for c in header_cells]
            headers_norm = [h.lower().replace(".", "") for h in headers]

            if not any("unidade" in h for h in headers_norm):
                continue

            if not any("tipo uasg" in h for h in headers_norm):
                continue

            if not any("qtd autorizada" in h for h in headers_norm):
                continue

            if not any("qtd saldo" in h for h in headers_norm):
                continue

            idx_unidade = next(
                i for i, h in enumerate(headers_norm)
                if "unidade" in h
            )

            idx_aut = next(
                i for i, h in enumerate(headers_norm)
                if "qtd autorizada" in h
            )

            idx_saldo = next(
                i for i, h in enumerate(headers_norm)
                if "qtd saldo" in h
            )

            for linha in linhas[1:]:
                cels = await linha.query_selector_all("th, td")

                if len(cels) <= max(idx_unidade, idx_aut, idx_saldo):
                    continue

                unidade = normalizar_espacos(await cels[idx_unidade].inner_text())

                if UNIDADE_PMB in unidade:
                    resultado["qtd_autorizada"] = normalizar_espacos(await cels[idx_aut].inner_text())
                    resultado["qtd_saldo"] = normalizar_espacos(await cels[idx_saldo].inner_text())
                    return resultado

        except Exception:
            continue

    return resultado


# =========================================================
# PORTAL - DETALHE DO PREGÃO / ITEM
# =========================================================

async def extrair_detalhe_pregao(page, detalhe_url: str) -> dict:
    await page.goto(detalhe_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(PAUSA_NAVEGACAO)

    rotulos = [
        "Unidade Origem",
        "Unidade Sub-rogada",
        "Unidade Beneficiária",
        "Tipo Compra",
        "Modalidade",
        "Número/Ano",
        "Inciso",
        "Lei",
        "Criado(a) em",
        "Alterado em",
    ]

    return await extrair_por_rotulos(page, rotulos)


async def extrair_item_detalhado(page, item_info: dict) -> dict:
    await page.goto(item_info["detalhe_url"], wait_until="domcontentloaded")
    await page.wait_for_timeout(PAUSA_NAVEGACAO)

    dados = await extrair_por_rotulos(
        page,
        ["Número", "Tipo Item", "Descrição", "Descrição detalhada"]
    )

    ata_fornecedor = await extrair_ata_fornecedor(page)
    qtd_valor = await extrair_quantidade_valor_unitario(page)
    qtds_pmb = await pegar_qtds_pmb(page)

    vig_ini = qtd_valor.get("vig_inicio", "")
    vig_fim = qtd_valor.get("vig_fim", "")

    return {
        "Número Item": dados.get("Número") or item_info.get("numero_lista", ""),
        "Tipo Item": dados.get("Tipo Item") or item_info.get("tipo_item_lista", ""),
        "Descrição": dados.get("Descrição") or item_info.get("descricao_lista", ""),
        "Descrição detalhada": dados.get("Descrição detalhada", ""),
        "Número Ata": ata_fornecedor.get("numero_ata", ""),
        "Fornecedor": ata_fornecedor.get("fornecedor", ""),
        "Valor Unitário": qtd_valor.get("valor_unitario", ""),
        "Total": qtd_valor.get("quantidade", ""),
        "autorizada ": qtds_pmb.get("qtd_autorizada", "0"),
        "Saldo": qtds_pmb.get("qtd_saldo", "0"),
        "Vig Início": vig_ini,
        "Vig Fim": vig_fim,
        "Faixa Vigência": faixa_vigencia(vig_fim),
        "Link detalhe item": item_info["detalhe_url"],
    }


# =========================================================
# NOVA ROTINA: LIMPEZA DE ABAS E ITENS VENCIDOS NA PLANILHA
# =========================================================

def fazer_limpeza_atas_vencidas(planilha):
    log("Iniciando varredura para limpeza de ITENS e ABAS vencidas na planilha (com base na data de hoje)...")
    try:
        abas = planilha.worksheets()
        
        for ws in abas:
            nome = ws.title.strip()
            # Ignora as abas de sistema (BD_CONSOLIDADO, INDICE, CONTROLE, etc)
            if nome in ABAS_FIXAS:
                continue
            
            try:
                # Retry embutido para ler valores sem estourar cota do Google
                valores = []
                for t in range(5):
                    try:
                        valores = ws.get_all_values()
                        time.sleep(1.5)
                        break
                    except Exception as e:
                        if "429" in str(e):
                            time.sleep((t + 1) * 10)
                        else:
                            raise
                            
                idx_vig_fim = -1
                linha_cab = -1
                
                # Procura a linha do cabeçalho que contém 'Vig Fim'
                for i, linha in enumerate(valores):
                    if "Vig Fim" in linha:
                        idx_vig_fim = linha.index("Vig Fim")
                        linha_cab = i
                        break
                        
                if idx_vig_fim == -1 or linha_cab == -1:
                    continue # Não tem a estrutura esperada de itens, pula.
                    
                linhas_para_excluir = []
                tem_ativo = False
                
                # Checa as datas linha por linha, de baixo para cima (para poder deletar sem quebrar os índices)
                for i in range(len(valores) - 1, linha_cab, -1):
                    linha = valores[i]
                    if len(linha) > idx_vig_fim and normalizar_espacos(linha[idx_vig_fim]):
                        dt = parse_data_br(linha[idx_vig_fim])
                        if dt and dt >= date.today():
                            tem_ativo = True
                        else:
                            # Item venceu (a data passou ou é inválida) -> marca a linha para exclusão
                            linhas_para_excluir.append(i)
                            
                # Se não tem NENHUM item ativo sobrou, a ata inteira venceu
                if not tem_ativo:
                    planilha.del_worksheet(ws)
                    salvar_status_controle_bot(planilha, nome, "VENCIDO")
                    log(f"🧹 Aba 100% vencida EXCLUÍDA: {nome}")
                    time.sleep(2)
                
                # Se tem itens ativos, mas também encontrou itens vencidos isolados
                elif linhas_para_excluir:
                    requests = []
                    for idx_linha in linhas_para_excluir:
                        requests.append({
                            "deleteDimension": {
                                "range": {
                                    "sheetId": ws.id,
                                    "dimension": "ROWS",
                                    "startIndex": idx_linha,
                                    "endIndex": idx_linha + 1
                                }
                            }
                        })
                    
                    if requests:
                        ws.spreadsheet.batch_update({"requests": requests})
                        log(f"🧹 {len(requests)} item(ns) vencido(s) apagado(s) da aba ativa: {nome}")
                        time.sleep(PAUSA_SHEETS)
                    
            except Exception as e:
                log(f"Falha ao inspecionar aba {nome} para limpeza: {e}")
                
        log("Faxina de itens e abas vencidas concluída!")
        
    except Exception as e:
        log(f"Erro geral na rotina de limpeza: {e}")


# =========================================================
# PLANILHA - SALVAMENTO SEM FORMATAÇÃO E LIMPEZAS INDIVIDUAIS
# =========================================================

def item_para_linha_bot(item: dict):
    return [item.get(h, "") for h in BOT_HEADERS]


def limpar_itens_removidos_da_aba(ws, linha_cab_itens, itens_ativos):
    """Deleta as linhas inteiras dos itens que de fato não estão mais ativos na busca do portal."""
    valores = ws.get_all_values()
    
    # Função para extrair apenas o número puro (ignora zeros a esquerda e casas decimais "01" vira "1")
    def extrair_id_puro(txt):
        s = re.sub(r"\D", "", normalizar_espacos(txt))
        return str(int(s)) if s.isdigit() else normalizar_espacos(txt).lower()
        
    numeros_ativos = {extrair_id_puro(item.get("Número Item", "")) for item in itens_ativos}
    
    requests = []
    for i in range(len(valores) - 1, linha_cab_itens, -1): 
        row = valores[i]
        numero_aba = extrair_id_puro(row[0]) if len(row) > 0 else ""
        
        if numero_aba and numero_aba not in numeros_ativos:
            requests.append({
                "deleteDimension": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "ROWS",
                        "startIndex": i,
                        "endIndex": i + 1
                    }
                }
            })
            
    if requests:
        try:
            ws.spreadsheet.batch_update({"requests": requests})
            time.sleep(PAUSA_SHEETS)
            log(f"Foram limpas {len(requests)} linha(s) antigas/removidas nesta aba.")
        except Exception as e:
            log(f"Falha ao apagar linhas limpas: {e}")


def atualizar_tabela_itens_sem_apagar_manual(ws, linha_cab_itens, itens):
    valores = ws.get_all_values()
    total_linhas = max(len(valores), linha_cab_itens + len(itens) + 50)

    matriz = []
    for i in range(total_linhas):
        linha = list(valores[i]) if i < len(valores) else []
        while len(linha) < BOT_COLS:
            linha.append("")
        matriz.append(linha)

    matriz[linha_cab_itens - 1][:BOT_COLS] = BOT_HEADERS

    # Padroniza para encontrar o item independentemente da formatação do Google Sheets
    def extrair_id_puro(txt):
        s = re.sub(r"\D", "", normalizar_espacos(txt))
        return str(int(s)) if s.isdigit() else normalizar_espacos(txt).lower()

    mapa = {}
    for idx in range(linha_cab_itens + 1, len(matriz) + 1):
        row = matriz[idx - 1]
        numero = extrair_id_puro(row[0]) if len(row) >= 1 else ""
        if numero:
            mapa[numero] = idx

    linha_atual = linha_cab_itens + 1
    usados = set()

    for item in itens:
        numero = extrair_id_puro(item.get("Número Item", ""))

        if numero and numero in mapa:
            destino = mapa[numero]
        else:
            while linha_atual in usados:
                linha_atual += 1
            destino = linha_atual
            linha_atual += 1

        usados.add(destino)
        matriz[destino - 1][:BOT_COLS] = item_para_linha_bot(item)

    ultima = max(usados) if usados else linha_cab_itens

    for i in range(ultima, min(len(matriz), ultima + 300)):
        matriz[i][:BOT_COLS] = [""] * BOT_COLS

    ultima_real = linha_cab_itens
    for i, row in enumerate(matriz, start=1):
        if any(normalizar_espacos(c) for c in row[:BOT_COLS]):
            ultima_real = i

    bloco = [row[:BOT_COLS] for row in matriz[:ultima_real]]

    update_com_retry(
        ws,
        f"A1:{rowcol_to_a1(ultima_real, BOT_COLS)}",
        bloco
    )


def escribir_bloco_resumo_sem_apagar_tudo(ws, numero_ano, dados_pregao, itens):
    qtd_total_consolidada = sum(extrair_numero(item.get("Total", "0")) for item in itens)
    qtd_aut_total_pmb = sum(extrair_numero(item.get("autorizada ", "0")) for item in itens)
    saldo_total_pmb = sum(extrair_numero(item.get("Saldo", "0")) for item in itens)
    qtd_com_detalhe = sum(1 for item in itens if item.get("Descrição detalhada"))
    resumo_vig = resumo_vigencia(itens)

    linhas = [
        [f"COMPRA / PREGÃO: {numero_ano}"],
        [f"Modalidade: {dados_pregao.get('Modalidade', MODALIDADE_ALVO)} | PMB atualizada em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"],
        [],
        ["DADOS GERAIS", "VALOR"],
        ["Unidade Origem", dados_pregao.get("Unidade Origem", "")],
        ["Unidade Sub-rogada", dados_pregao.get("Unidade Sub-rogada", "")],
        ["Unidade Beneficiária", dados_pregao.get("Unidade Beneficiária", "")],
        ["Tipo Compra", dados_pregao.get("Tipo Compra", "")],
        ["Modalidade", dados_pregao.get("Modalidade", "")],
        ["Número/Ano", dados_pregao.get("Número/Ano", "")],
        ["Inciso", dados_pregao.get("Inciso", "")],
        ["Lei", dados_pregao.get("Lei", "")],
        ["Criado(a) em", dados_pregao.get("Criado(a) em", "")],
        ["Alterado em", dados_pregao.get("Alterado em", "")],
        [],
        ["RESUMO DOS ITENS ATIVOS", "VALOR"],
        ["Quantidade de itens ativos", len(itens)],
        ["Qtd. Total consolidada", qtd_total_consolidada],
        ["Qtd. autorizada total PMB", qtd_aut_total_pmb],
        ["Qtd. saldo total PMB", saldo_total_pmb],
        ["Itens com descrição detalhada", qtd_com_detalhe],
        ["Menor Vig. Fim ARP", resumo_vig["menor_vig_fim"]],
        ["Maior Vig. Fim ARP", resumo_vig["maior_vig_fim"]],
        [],
    ]

    update_com_retry(ws, "A1:B24", linhas)
    return 25


def salvar_aba_completa(planilha, numero_ano: str, dados_pregao: dict, itens: list[dict]) -> None:
    log(f"Salvando/atualizando aba individual (sem formatação): {numero_ano}")

    nome = nome_limpo(numero_ano)
    ws = get_or_create_worksheet(planilha, nome, rows=5000, cols=40)

    itens = deduplicar_itens_por_link(itens)

    linha_cab_itens = escribir_bloco_resumo_sem_apagar_tudo(ws, numero_ano, dados_pregao, itens)

    atualizar_tabela_itens_sem_apagar_manual(ws, linha_cab_itens, itens)
    limpar_itens_removidos_da_aba(ws, linha_cab_itens, itens)

    log(f"Aba individual atualizada com sucesso: {nome}")


# =========================================================
# BANCO DE DADOS CONSOLIDADO (SEM FORMATAÇÃO)
# =========================================================

def atualizar_aba_banco_dados(planilha, registros_com_itens):
    """Cria/Limpa a aba BD_CONSOLIDADO e descarrega todos os dados puros."""
    log("Alimentando aba de Banco de Dados Consolidado (bruto/sem formatação)...")
    
    ws = get_or_create_worksheet(planilha, "BD_CONSOLIDADO", rows=10000, cols=20)
    clear_com_retry(ws)
    
    bd_headers = ["Pregão Origem"] + BOT_HEADERS
    linhas_bd = [bd_headers]
    
    for reg in registros_com_itens:
        numero_pregao = reg.get("numero_ano", "")
        itens = reg.get("itens_dados", [])
        
        for item in itens:
            nova_linha = [numero_pregao] + [item.get(h, "") for h in BOT_HEADERS]
            linhas_bd.append(nova_linha)
            
    if len(linhas_bd) > 1:
        fim_a1 = rowcol_to_a1(len(linhas_bd), len(bd_headers))
        update_com_retry(ws, f"A1:{fim_a1}", linhas_bd)
        log(f"BD_CONSOLIDADO atualizado! {len(linhas_bd) - 1} linhas inseridas.")
    else:
        update_com_retry(ws, "A1", [bd_headers])
        log("Nenhum item ativo localizado para alimentar a base de dados.")


# =========================================================
# ÍNDICE GERAL DOS PREGÕES
# =========================================================

def atualizar_indice(planilha, registros_salvos: list[dict]) -> None:
    ws = get_or_create_worksheet(planilha, "INDICE_PREGOES", rows=2000, cols=10)
    clear_com_retry(ws)

    linhas = [[
        "Pregão", "Modalidade", "Itens ativos", "Qtd. autorizada PMB",
        "Qtd. saldo PMB", "Menor vigência fim", "Maior vigência fim", "Status", "Última atualização"
    ]]

    for r in registros_salvos:
        linhas.append([
            r.get("numero_ano", ""),
            r.get("modalidade", ""),
            r.get("itens_vigentes", 0),
            r.get("qtd_autorizada_total_pmb", 0),
            r.get("qtd_saldo_total_pmb", 0),
            r.get("menor_vig_fim", ""),
            r.get("maior_vig_fim", ""),
            r.get("status", ""),
            datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        ])

    update_com_retry(ws, "A1", linhas)


# =========================================================
# PROCESSAMENTO DE CADA PREGÃO
# =========================================================

async def processar_pregao(page, planilha, reg: dict):
    pregao_ref = reg.get("numero_ano_lista", "")

    if not reg.get("detalhe_url"):
        log("Sem link de detalhe. Pulando.")
        registrar_erro(planilha, "PREGAO", pregao_ref, "", "Sem link de detalhe")
        return None

    dados_pregao = await extrair_detalhe_pregao(page, reg["detalhe_url"])
    numero_ano = (dados_pregao.get("Número/Ano") or pregao_ref or "").strip()

    if not numero_ano:
        registrar_erro(planilha, "PREGAO", pregao_ref, "", "Número/Ano não encontrado")
        return None

    modalidade_detalhe = dados_pregao.get("Modalidade", "").strip()
    if modalidade_detalhe:
        mod_up = modalidade_detalhe.upper()
        if "05" not in mod_up or ("PREGÃO" not in mod_up and "PREGAO" not in mod_up):
            log(f"⛔ Ignorando, não é pregão: {modalidade_detalhe}")
            return None

    itens = []
    links_itens = []
    erros_extracao = 0
    if reg.get("itens_url"):
        log("Entrando na lista de itens...")
        links_itens, houve_erro_leitura_lista = await coletar_links_itens(page, reg["itens_url"])
        log(f"Total de links de itens encontrados: {len(links_itens)}")

        if houve_erro_leitura_lista:
            erros_extracao += 1

        for j, item_info in enumerate(links_itens, start=1):
            item_ref = item_info.get("numero_lista", "")
            try:
                log(f"  Item {j}/{len(links_itens)}")
                item = await extrair_item_detalhado(page, item_info)
                valido, motivo = item_valido(item)
                if not valido:
                    registrar_erro(planilha, "ITEM_INVALIDO", numero_ano, item_ref, motivo)
                    continue
                itens.append(item)
                await page.wait_for_timeout(int(PAUSA_ITEM * 1000))
            except Exception as e_item:
                log(f"  ERRO no item {j}: {e_item}")
                registrar_erro(planilha, "ITEM_ERRO", numero_ano, item_ref, str(e_item))
                erros_extracao += 1

    log(f"Itens totais extraídos: {len(itens)}")
    itens = deduplicar_itens_por_link(itens)

    itens_ativos = [x for x in itens if eh_item_vigente_estrito(x)]

    if SOMENTE_VIGENCIA_ATIVA:
        if not itens_ativos:
            if erros_extracao > 0:
                log(
                    f"⚠ {erros_extracao} erro(s) ao extrair item(ns) deste pregão "
                    f"(de {len(links_itens)} encontrado(s) no portal). NÃO vou apagar a aba: "
                    f"sem certeza de que os itens venceram, pode ser instabilidade do portal."
                )
                registrar_erro(
                    planilha, "PREGAO_EXTRACAO_INCERTA", numero_ano, "",
                    f"{erros_extracao} erro(s) de extração de {len(links_itens)} item(ns); aba preservada por segurança"
                )
                return None

            log("⚠ Pregão sem itens ativos após varredura completa e sem erros de extração. Garantindo que a aba não exista...")
            try:
                aba_vencida = planilha.worksheet(nome_limpo(numero_ano))
                planilha.del_worksheet(aba_vencida)
                log(f"Aba do pregão {numero_ano} deletada.")
            except: pass
            salvar_status_controle_bot(planilha, numero_ano, "VENCIDO")
            return None
        itens_salvar = itens_ativos
    else:
        itens_salvar = itens

    # Processa e gera a aba individual normalmente (sem formatação via código)
    salvar_aba_completa(planilha, numero_ano, dados_pregao, itens_salvar)
    salvar_status_controle_bot(planilha, numero_ano, "ATIVO")

    resumo_vig = resumo_vigencia(itens_salvar)
    qtd_aut_total = sum(extrair_numero(x.get("autorizada ", "0")) for x in itens_salvar)
    qtd_saldo_total = sum(extrair_numero(x.get("Saldo", "0")) for x in itens_salvar)
    status = classificar_status_pregao(itens_salvar)

    return {
        "numero_ano": numero_ano,
        "modalidade": dados_pregao.get("Modalidade", MODALIDADE_ALVO),
        "itens_vigentes": len(itens_salvar),
        "itens_ativos": len(itens_salvar),
        "qtd_autorizada_total_pmb": round(qtd_aut_total, 2),
        "qtd_saldo_total_pmb": round(qtd_saldo_total, 2),
        "menor_vig_fim": resumo_vig["menor_vig_fim"],
        "maior_vig_fim": resumo_vig["maior_vig_fim"],
        "status": status,
        "itens_dados": itens_salvar, # Transporta os dados puros para alimentar o banco consolidado
    }


# =========================================================
# ORQUESTRAÇÃO PRINCIPAL
# =========================================================

async def main():
    try:
        abrir_chrome()
        planilha = conectar_google()
        testar_google(planilha)

        # Roda a limpeza profunda das abas fisicamente vencidas direto na planilha, antes de abrir o Playwright
        fazer_limpeza_atas_vencidas(planilha)

        async with async_playwright() as p:
            log("Conectando ao Chrome via CDP...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")

            if not browser.contexts: raise RuntimeError("Nenhum contexto encontrado no Chrome.")
            context = browser.contexts[0]
            page = await context.new_page()  # aba nova e dedicada, nao reaproveita abas ja abertas/ocupadas

            log("Abrindo portal inicial...")
            await page.goto(URL_HOME, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            registros = await coletar_links_pregoes_todos_anos(page)
            if not registros:
                log("Nenhum pregão localizado no portal.")
                return

            controle_bot = carregar_controle_bot(planilha)
            registros_filtrados = []

            for reg in registros:
                numero_ano = normalizar_espacos(reg.get("numero_ano_lista", ""))
                if controle_bot.get(numero_ano, {}).get("status") == "VENCIDO":
                    log(f"Pulando histórico vencido: {numero_ano}")
                    continue
                registros_filtrados.append(reg)

            log(f"Pregões para varredura de saldo/vigência: {len(registros_filtrados)}")
            registros_indice = []

            for i, reg in enumerate(registros_filtrados, start=1):
                try:
                    numero_ano = reg.get("numero_ano_lista", "").strip()
                    log(f"Processando {i}/{len(registros_filtrados)}: {numero_ano}")

                    resultado = await processar_pregao(page, planilha, reg)
                    if resultado: registros_indice.append(resultado)

                except Exception as e:
                    log(f"Erro em {reg.get('numero_ano_lista', '')}: {e}")
                    registrar_erro(planilha, "VARREDURA_COMPLETA_PREGAO", reg.get("numero_ano_lista", ""), "", str(e))
                    traceback.print_exc()

            if registros_indice:
                # 1. Atualiza o Índice estruturado e formatado
                atualizar_indice(planilha, registros_indice)
                
                # 2. Descarrega todos os itens de forma corrida na base crua (Banco de Dados)
                atualizar_aba_banco_dados(planilha, registros_indice)

            log("Processo finalizado com sucesso.")
            return

    except Exception as e:
        log(f"ERRO FATAL: {e}")
        traceback.print_exc()
        try:
            planilha = conectar_google()
            registrar_erro(planilha, "ERRO_FATAL", "", "", str(e))
        except: pass
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())