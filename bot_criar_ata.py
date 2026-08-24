import asyncio
import base64
import json
import re
import sys
import traceback
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pdfplumber
from playwright.async_api import async_playwright

from comum import (
    abrir_chrome,
    log,
)

# =========================================================
# BOT CRIAR ATA - Cria Atas de Registro de Preços clonando um
# modelo já existente, no Compras.gov.br (área de trabalho:
# cnetmobile.estaleiro.serpro.gov.br/comprasnet-area-trabalho-web)
# =========================================================
# STATUS: EM CONSTRUÇÃO — funções de COLETA de dados prontas e
# testadas (só leitura, zero risco). A parte de PREENCHIMENTO/
# CRIAÇÃO da ata em si (clonar, editar, salvar) ainda não está
# implementada como função reutilizável — só foi validada
# manualmente, passo a passo, numa ata de teste.
#
# REGRAS DE SEGURANÇA (definidas pelo usuário, nunca quebrar):
#   - Nunca editar a Ata modelo original diretamente — sempre
#     clonar primeiro e editar só a cópia.
#   - Nunca inventar informação. Se um dado obrigatório não for
#     encontrado (ex: item sem Especificação no TR), a automação
#     deve avisar o usuário, não adivinhar.
#   - Nunca clicar "Concluir" sozinho — deixar a ata em rascunho
#     pronta pra revisão humana antes de finalizar.
#   - Cada Ata de uma empresa deve conter SOMENTE os itens
#     homologados para aquela empresa (nunca itens de outras
#     empresas, nunca itens só cotados).
#
# FLUXO GERAL (ver conversa/planejamento para o desenho completo):
#   1. Coletar fornecedores + itens homologados do pregão
#      (TODO: função ainda não escrita/testada via script — a
#      tela fica em .../comprasnet-web/seguro/governo/
#      selecao-fornecedores?identificador=<UASG+modalidade+numero+ano>
#      &etapa=AH, aba "Fornecedores", expandir cada fornecedor e
#      cada item pra pegar quantidade/marca/modelo/valor).
#   2. Extrair Especificação + Unidade de Medida do PDF do Termo de
#      Referência vinculado (ver extrair_itens_tr() abaixo — testado).
#   3. Para cada fornecedor: clonar a Ata modelo, abrir a clone no
#      editor, preencher a tabela de itens (seção "DOS PREÇOS,
#      ESPECIFICAÇÕES E QUANTITATIVOS") e remover linhas dos itens
#      que não são daquele fornecedor nas 3 tabelas de UGG/UGP
#      (seção "ÓRGÃO(S) GERENCIADOR E PARTICIPANTE(S)"). Mecanismo
#      de edição validado manualmente (ver notas técnicas abaixo).
#   4. Conferir tudo antes de considerar a ata pronta. Nunca clicar
#      "Concluir" automaticamente.
#
# NOTAS TÉCNICAS DE COMO EDITAR O DOCUMENTO (validado em teste real):
#   - O corpo de cada seção da ata é um editor de texto rico
#     (contenteditable) dentro de um <iframe>. Pra editar uma célula
#     de tabela: localizar o <td> (via table/tr/td por índice) e
#     simplesmente `.click()` + `page.keyboard.type(texto)`.
#   - Pra inserir/remover linha da tabela: o clique direito comum do
#     Playwright (`.click(button="right")`) NÃO abre o menu de
#     contexto desse editor. É preciso simular o clique via CDP puro:
#       cdp = await page.context.new_cdp_session(page)
#       await cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "right", "clickCount": 1})
#       await cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "right", "clickCount": 1})
#     Depois disso o menu ("Colar", "Célula", "Linha", "Coluna",
#     "Apagar Tabela", "Formatar Tabela") aparece em outro frame da
#     página (não necessariamente o mesmo frame_editor) — é preciso
#     procurar o texto "Linha" / "Inserir linha abaixo" / "Remover
#     Linhas" em TODOS os frames da página, não só no frame do
#     editor.
#   - Navegar direto por URL pra um artefato específico NÃO funciona
#     (o app redireciona pra Área de Trabalho) — é preciso navegar
#     clicando pela própria interface (Área de Trabalho > Artefatos
#     Digitais > clicar na linha).
# =========================================================

URL_AREA_TRABALHO = "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-area-trabalho-web/seguro/governo/area-trabalho"


# =========================================================
# TERMO DE REFERÊNCIA (PDF) - Especificação + Unidade por item
# =========================================================

def _limpar(txt) -> str:
    if txt is None:
        return ""
    return re.sub(r"\s+", " ", txt).strip()


def extrair_itens_tr(caminho_pdf: str) -> dict:
    """Extrai do PDF do Termo de Referência, por número de item:
    grupo, especificação e unidade de medida (únicos dados que devem
    vir do TR — o resto vem do sistema do pregão).

    Retorna {numero_item_str: {"grupo": str, "especificacao": str, "unidade": str}}.

    Testado com um TR real de 87 itens / 40 páginas: extrai ~93% dos
    itens corretamente. O restante fica de fora do dict propositalmente
    (regra: nunca inventar dado) — quem chamar essa função DEVE
    conferir se todos os itens esperados estão presentes e avisar o
    usuário sobre os que faltarem, em vez de seguir sem eles.
    """
    itens = {}
    grupo_atual = ""

    with pdfplumber.open(caminho_pdf) as pdf:
        for pagina in pdf.pages:
            for tabela in pagina.extract_tables():
                for linha in tabela:
                    if len(linha) < 5:
                        continue

                    grupo, item, _catmat, especificacao, unidade = linha[0], linha[1], linha[2], linha[3], linha[4]
                    item_limpo = _limpar(item)
                    unidade_limpa = _limpar(unidade)
                    especificacao_limpa = _limpar(especificacao)

                    if not item_limpo.isdigit():
                        continue
                    if len(unidade_limpa) > 15 or not unidade_limpa:
                        continue
                    if len(especificacao_limpa) < 5 or not re.search(r"[A-Za-zÀ-ÿ]{3,}", especificacao_limpa):
                        continue

                    m_grupo = re.match(r"^(\d+)\b", _limpar(grupo))
                    if m_grupo:
                        grupo_atual = m_grupo.group(1)

                    itens[item_limpo] = {
                        "grupo": grupo_atual,
                        "especificacao": especificacao_limpa,
                        "unidade": unidade_limpa,
                    }

    return itens


def conferir_itens_faltantes(itens_tr: dict, numeros_esperados: list[str]) -> list[str]:
    """Retorna a lista de números de item esperados (ex: vindos do
    sistema do pregão) que NÃO foram encontrados no TR. Se não for
    vazia, a automação deve parar e avisar o usuário — nunca seguir
    sem essa informação (regra 14 do projeto)."""
    return [n for n in numeros_esperados if n not in itens_tr]


async def baixar_pdf_artefato(pagina_view, destino: str) -> None:
    """Baixa os bytes reais do PDF a partir de uma página de
    visualização de artefato já aberta (tipo=TR, modo /view, com o
    visualizador pdf.js carregado) e salva em `destino`. O PDF fica
    como blob: no navegador, só acessível via fetch() dentro da
    própria página."""
    frame_pdf = None
    for fr in pagina_view.frames:
        if "viewer.html" in fr.url:
            frame_pdf = fr
            break

    if frame_pdf is None:
        raise RuntimeError("Visualizador de PDF (pdf.js) não encontrado na página. Confira se é uma página de artefato em modo de visualização (tipo=TR, .../view/...).")

    qs = parse_qs(urlparse(frame_pdf.url).query)
    blob_url = unquote(qs["file"][0])

    base64_pdf = await pagina_view.evaluate(
        """async (blobUrl) => {
            const resp = await fetch(blobUrl);
            const buf = await resp.arrayBuffer();
            let binary = '';
            const bytes = new Uint8Array(buf);
            const chunkSize = 0x8000;
            for (let i = 0; i < bytes.length; i += chunkSize) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
            }
            return btoa(binary);
        }""",
        blob_url,
    )

    Path(destino).write_bytes(base64.b64decode(base64_pdf))
    log(f"PDF salvo em: {destino}")


# =========================================================
# FORNECEDORES / ITENS HOMOLOGADOS DO PREGÃO
# =========================================================
# Tela: .../comprasnet-web/seguro/governo/selecao-fornecedores
# ?identificador=<UASG><modalidade 2 dig><numero 5 dig><ano>&etapa=AH
# (ou etapa=FR se o pregão estiver em Fase Recursal — a etapa exata
# não importa pro conteúdo da aba Fornecedores, só muda o título).
#
# IMPORTANTE: não dá pra abrir essa URL direto (nem em aba nova) —
# o app redireciona pra "acesso não autorizado". É preciso navegar
# clicando a partir da Área de Trabalho (achar o card do pregão e
# clicar no link/ação dele), com espera generosa (~8s) depois de
# carregar a Área de Trabalho antes de clicar, senão o clique cai
# num estado stale e a navegação falha ("Compra não encontrada").
#
# Na aba "Fornecedores": cada fornecedor é uma linha com CNPJ, razão
# social e "Itens habilitados: X de Y". Pra expandir e ver os itens,
# NÃO adianta clicar na linha/texto — precisa clicar especificamente
# no <button aria-expanded="..."> daquela linha (ícone de seta que
# gira). Expandido, aparecem 2 blocos de itens:
#   - "Itens em que o fornecedor é o melhor classificado" -> USAR
#     (são os itens homologados de fato pra esse fornecedor)
#   - "Itens em que o fornecedor não é o melhor classificado" ->
#     IGNORAR (mesmo que o status individual diga "Homologado" —
#     confirmado pelo usuário que esses NÃO contam pra esse fornecedor)
#
# Cada item nesses blocos mostra: número, descrição curta, status,
# valor estimado e valor ofertado. Quantidade ofertada / Marca /
# Modelo (que também precisamos, ver estrutura da Ata) ficam atrás
# de expansões adicionais — MECÂNICA TESTADA E CONFIRMADA:
#
# Tanto item avulso quanto linha de grupo ficam numa <tr> dentro da
# tabela "Itens em que o fornecedor é o melhor classificado" (a
# PRIMEIRA <table> dentro da linha do fornecedor expandido — a
# segunda table é a de "...não é o melhor classificado", que a gente
# ignora). Cada <tr> tem um botão com ícone `fa-plus-square`.
#
# ITEM AVULSO (não-grupo, ex: "40 BLOCO DE CONCRETO"): 2 passos —
#   1. Clicar no `fa-plus-square` da <tr> -> navega pra
#      .../item/<numero>?identificador=... (visão geral do ITEM, lista
#      todos os fornecedores que ofertaram nele).
#   2. Achar a linha do CNPJ alvo (div.cp-item-expansivel) e clicar no
#      `button[aria-expanded]` -> expande e revela direto a aba
#      "PROPOSTA" com Quantidade ofertada, Marca/Fabricante,
#      Modelo/Versão (SEM "Descrição detalhada" — esse campo só existe
#      na tela de itens de grupo, ver abaixo).
#   Ver expandir_item_avulso_e_extrair(). 1x "Voltar" pra retornar.
#
# ITEM VENCIDO POR GRUPO (linha "GRUPO N | X itens"): 3 passos —
#   1. Clicar no `fa-plus-square` da <tr> do grupo -> navega pra
#      .../item/<id-negativo>?identificador=... (visão geral do GRUPO,
#      lista todos os fornecedores que disputaram aquele grupo).
#   2. Achar a linha do CNPJ alvo e clicar no `button[aria-expanded]`
#      (aria-label="Mostrar proposta do grupo") -> expande e revela um
#      bloco "PROPOSTA" terminando num botão/link "Itens do grupo >>"
#      (cuidado: não confundir com o texto explicativo mais longo
#      "Visualize as propostas dos itens do grupo..." que também
#      contém essa substring — procurar um <button>/<a> especificamente
#      com esse texto, não texto solto).
#   3. Clicar em "Itens do grupo >>" -> navega pra
#      .../item/<id>/itens-grupo/participante/<cnpj-sem-formatacao>,
#      que lista cada item do grupo como div.cp-item-expansivel (aqui
#      SIM, estrutura diferente da <tr> da listagem principal), cada
#      um com um botão-chevron (`button:has(i.fa-chevron-down)`) que
#      expande INLINE e revela Descrição detalhada + Quantidade
#      ofertada + Marca/Fabricante + Modelo/Versão.
#   Ver expandir_grupo_e_coletar_itens() / expandir_item_individual().
#   2x "Voltar" pra retornar (participante -> overview -> lista).
#
# coletar_fornecedores_itens() já orquestra tudo isso automaticamente.


async def _expandir_fornecedor(page, linha_locator) -> bool:
    """Clica no botão de expandir (aria-expanded) dentro da linha do
    fornecedor. Retorna True se conseguiu expandir."""
    botao = linha_locator.locator("button[aria-expanded]").first

    if await botao.count() == 0:
        return False

    if (await botao.get_attribute("aria-expanded")) == "true":
        return True  # já estava expandido

    await botao.scroll_into_view_if_needed()
    await botao.click()

    # Espera ativa pelo atributo virar "true" (até 8s), em vez de um
    # timeout fixo — mesma classe de bug de timing confirmada em outros
    # pontos do arquivo (ver expandir_item_avulso_e_extrair). Depois que
    # vira "true", uma folga curta pro conteúdo interno assentar.
    for _ in range(40):
        if (await botao.get_attribute("aria-expanded")) == "true":
            await page.wait_for_timeout(300)
            return True
        await page.wait_for_timeout(200)

    return False


_RE_ITEM_EXPANDIDO = re.compile(
    r"(?:Descrição detalhada\n(?P<descricao_detalhada>[^\n]+)\n.*?)?"
    r"Quantidade ofertada\n(?P<quantidade_ofertada>[\d.,]+)\n"
    r"Marca/Fabricante\n(?P<marca>[^\n]*)\n"
    r"Modelo/Vers[ãa]o\n(?P<modelo>[^\n]*)",
    re.DOTALL,
)


def _parsear_item_expandido(texto: str) -> dict:
    """Extrai os dados que só aparecem depois de expandir um item:
    quantidade ofertada, marca/fabricante, modelo/versão e, quando
    presente, descrição detalhada (só aparece na tela 'Itens do
    grupo' — a tela de detalhe de item avulso, .../item/<numero>,
    não tem esse campo)."""
    m = _RE_ITEM_EXPANDIDO.search(texto)
    if not m:
        return {}
    resultado = {
        "quantidade_ofertada": _limpar(m.group("quantidade_ofertada")),
        "marca": _limpar(m.group("marca")),
        "modelo": _limpar(m.group("modelo")),
    }
    if m.group("descricao_detalhada"):
        resultado["descricao_detalhada"] = _limpar(m.group("descricao_detalhada"))
    return resultado


async def expandir_item_individual(item_locator, page) -> dict:
    """Clica no chevron de expansão de UM item (funciona tanto pra
    item avulso na aba Fornecedores quanto pra item dentro da tela
    'Itens do grupo') e retorna o detalhe extraído (ver
    _parsear_item_expandido). Não faz nada se já estiver expandido."""
    chevron = item_locator.locator("button:has(i.fa-chevron-down)").first

    if await chevron.count() > 0:
        await chevron.scroll_into_view_if_needed()
        await chevron.click()
        # Espera ativa pelo conteúdo expandido aparecer (mesma classe de
        # bug de timing confirmada em outros pontos do arquivo).
        for _ in range(40):
            if "Quantidade ofertada" in await item_locator.inner_text():
                break
            await page.wait_for_timeout(200)

    texto = await item_locator.inner_text()
    detalhe = _parsear_item_expandido(texto)
    if not detalhe:
        log("  ⚠ Não consegui extrair detalhe do item expandido (conteúdo não apareceu a tempo).")
    return detalhe


async def expandir_item_avulso_e_extrair(page, tr_locator, cnpj_fornecedor: str) -> dict:
    """A partir da linha <tr> de UM item avulso (não-grupo) já visível
    (dentro do fornecedor expandido, na aba Fornecedores), clica no "+"
    (navega pra .../item/<numero>, visão geral do item — lista todos os
    fornecedores que ofertaram nele), acha a linha do fornecedor alvo e
    expande a proposta. Retorna o detalhe extraído (quantidade ofertada,
    marca, modelo — sem descrição detalhada, que não existe nessa tela,
    só na tela 'Itens do grupo').

    Deixa `page` na tela .../item/<numero> — quem chamar essa função é
    responsável por voltar/renavegar pra continuar coletando os
    próximos itens/fornecedores depois.

    STATUS: validado de ponta a ponta com dados reais (pregão 44/2026,
    item 40 BLOCO DE CONCRETO, fornecedor F DE OLIVEIRA -> quantidade
    ofertada 16, marca PRÉ MOLDADO, modelo C/EDITAL, tudo batendo).
    """
    botao_mais = tr_locator.locator("button:has(i.fa-plus-square)").first
    if await botao_mais.count() == 0:
        log("  ⚠ Não achei o botão '+' na linha do item.")
        return {}

    await botao_mais.scroll_into_view_if_needed()
    await botao_mais.click()

    # Espera ativa pelo CNPJ aparecer (em vez de timeout fixo) — em
    # itens com mais fornecedores pra renderizar, 2000ms fixos às vezes
    # não bastavam e a função concluía (errado) que o fornecedor não
    # estava na tela, quando na verdade só ainda não tinha carregado.
    linha_fornecedor = page.get_by_text(cnpj_fornecedor, exact=False).first
    try:
        await linha_fornecedor.wait_for(state="visible", timeout=8000)
    except Exception:
        log(f"  ⚠ Não achei o fornecedor {cnpj_fornecedor} na visão geral do item.")
        return {}

    linha_fornecedor_container = linha_fornecedor.locator(
        "xpath=ancestor::div[contains(@class,'cp-item-expansivel')]"
    ).first

    if not await _expandir_fornecedor(page, linha_fornecedor_container):
        log(f"  ⚠ Não consegui expandir a proposta do fornecedor {cnpj_fornecedor} no item.")
        return {}

    # aria-expanded="true" não garante que o conteúdo (Quantidade
    # ofertada/Marca/Modelo) já renderizou — confirmado: em execuções
    # mais longas, vários itens seguidos vinham com esse conteúdo ainda
    # vazio mesmo com a proposta "expandida". Espera ativa pelo texto
    # aparecer antes de tentar extrair.
    for _ in range(40):
        if "Quantidade ofertada" in await linha_fornecedor_container.inner_text():
            break
        await page.wait_for_timeout(200)

    texto = await linha_fornecedor_container.inner_text()
    detalhe = _parsear_item_expandido(texto)
    if not detalhe:
        log(f"  ⚠ Proposta do fornecedor {cnpj_fornecedor} expandiu mas o conteúdo não apareceu a tempo.")
    return detalhe


async def expandir_grupo_e_coletar_itens(page, linha_grupo_locator, cnpj_fornecedor: str) -> list[dict]:
    """A partir da linha 'GRUPO N | X itens' já visível (dentro do
    fornecedor expandido, na aba Fornecedores), navega pela cadeia de
    3 cliques documentada no topo do arquivo e retorna a lista de
    itens do grupo com todos os dados (número, descrição, marca,
    modelo, quantidade ofertada, valor unitário ofertado).

    Deixa `page` na tela final (.../itens-grupo/participante/<cnpj>)
    — quem chamar essa função é responsável por voltar/renavegar pra
    continuar coletando os próximos fornecedores/grupos depois.

    STATUS: validada de ponta a ponta com dados reais (pregão 44/2026,
    fornecedor F DE OLIVEIRA, GRUPO 3 -> 3 itens extraídos corretamente
    com número, descrição, marca, modelo, quantidade ofertada e valor
    unitário todos batendo com o esperado). Validado com o pregão na
    etapa "Adjudicação/Homologação" (etapa=AH). Numa tentativa anterior,
    com o pregão na etapa "Fase Recursal" (etapa=FR), o passo de achar
    `cnpj_fornecedor` na visão geral do grupo falhou — foi confirmado
    depois que ESSE MESMO tipo de falha ("não achei o fornecedor")
    também acontecia em etapa=AH por causa de timing (2000ms fixos não
    bastavam sempre; trocado por espera ativa, ver código). Muito
    provavelmente a falha em FR era o mesmo problema, mas isso não foi
    reconfirmado especificamente em FR — se for chamar essa função com
    o pregão numa fase diferente de AH, vale testar de novo antes de
    confiar no resultado.
    """
    botao_mais = linha_grupo_locator.locator("button:has(i.fa-plus-square)").first
    if await botao_mais.count() == 0:
        log("  ⚠ Não achei o botão '+' na linha do grupo.")
        return []

    await botao_mais.scroll_into_view_if_needed()
    await botao_mais.click()

    # Visão geral do grupo: lista TODOS os fornecedores que disputaram
    # esse grupo, não só o nosso alvo — precisa achar a linha certa.
    # Espera ativa (em vez de timeout fixo) — ver nota em
    # expandir_item_avulso_e_extrair, mesmo bug de timing confirmado lá.
    linha_fornecedor = page.get_by_text(cnpj_fornecedor, exact=False).first
    try:
        await linha_fornecedor.wait_for(state="visible", timeout=8000)
    except Exception:
        log(f"  ⚠ Não achei o fornecedor {cnpj_fornecedor} na visão geral do grupo.")
        return []

    linha_fornecedor_container = linha_fornecedor.locator(
        "xpath=ancestor::div[contains(@class,'cp-item-expansivel')]"
    ).first

    if not await _expandir_fornecedor(page, linha_fornecedor_container):
        log(f"  ⚠ Não consegui expandir a proposta do fornecedor {cnpj_fornecedor} no grupo.")
        return []

    # O link só existe no DOM depois que a proposta termina de
    # expandir/renderizar. Espera ativa (8s) normalmente resolve, mas em
    # alguns casos reais (confirmado: mesmo fornecedor/pregão, ora falha
    # ora não, sem diferença estrutural visível) 8s não bastaram — então
    # tenta de novo fechando e reabrindo a proposta, até 3 tentativas,
    # em vez de desistir na primeira falha e perder itens silenciosamente.
    link_itens_grupo = page.locator("button, a").filter(has_text="Itens do grupo").first
    achou_link = False

    for tentativa in range(3):
        try:
            await link_itens_grupo.wait_for(state="visible", timeout=8000)
            achou_link = True
            break
        except Exception:
            if tentativa < 2:
                log(f"  ⚠ Link 'Itens do grupo >>' não apareceu (tentativa {tentativa + 1}/3) — fechando e reabrindo a proposta...")
                await linha_fornecedor_container.locator("button[aria-expanded]").first.click()
                await page.wait_for_timeout(800)
                if not await _expandir_fornecedor(page, linha_fornecedor_container):
                    continue

    if not achou_link:
        log("  ⚠ Não achei o link 'Itens do grupo >>' após 3 tentativas.")
        return []

    await link_itens_grupo.scroll_into_view_if_needed()
    await link_itens_grupo.click()
    await page.wait_for_timeout(2500)

    cards_item = page.locator("div.cp-item-expansivel:visible")
    total_cards = await cards_item.count()

    itens = []
    for i in range(total_cards):
        card = cards_item.nth(i)
        texto_fechado = await card.inner_text()

        m_num = re.match(r"(\d+)\s+([^\n]+)", texto_fechado)
        if not m_num:
            continue

        detalhe = await expandir_item_individual(card, page)
        m_valor = re.search(r"Valor ofertado \(unitário\)\D*R\$\s*([\d.,]+)", texto_fechado)

        itens.append({
            "eh_grupo": False,
            "numero_item": m_num.group(1),
            "descricao_curta": _limpar(m_num.group(2)),
            "valor_unitario_ofertado": m_valor.group(1) if m_valor else "",
            **detalhe,
        })

    return itens


async def _voltar(page, vezes: int = 1) -> None:
    """Clica no botão/link 'Voltar' `vezes` vezes seguidas, esperando
    a navegação entre cada clique."""
    for _ in range(vezes):
        voltar = page.get_by_text("Voltar", exact=True).first
        # Espera ativa pelo botão aparecer — a tela anterior (proposta
        # do item/grupo recém expandida) pode levar um instante a mais
        # pra renderizar o "Voltar" do que os 200-300ms que os passos
        # anteriores já esperaram; sem isso, o quem chama seguia achando
        # que já tinha voltado quando na real ainda não navegou nada.
        try:
            await voltar.wait_for(state="visible", timeout=8000)
        except Exception:
            log("  ⚠ Botão 'Voltar' não encontrado ao tentar navegar de volta.")
            return
        await voltar.click()
        await page.wait_for_timeout(3000)


async def _tem_proxima_pagina(linha_fornecedor_locator) -> bool:
    """Verifica se a tabela 'melhor classificado' da linha do fornecedor
    tem mais uma página disponível. O componente p-paginator do PrimeNG
    fica sempre presente no DOM (mesmo com só 1 página) mas só fica
    visível quando há mais itens do que cabem numa página (10 por
    página, confirmado em teste real: fornecedor com 19 linhas
    distribuídas em página 1 com 10 + página 2 com 9)."""
    paginador = linha_fornecedor_locator.locator("p-paginator").first
    if await paginador.count() == 0 or not await paginador.is_visible():
        return False

    proximo = paginador.locator(".p-paginator-next").first
    if await proximo.count() == 0:
        return False

    return (await proximo.get_attribute("disabled")) is None


async def _ir_para_proxima_pagina(page, linha_fornecedor_locator) -> None:
    proximo = linha_fornecedor_locator.locator("p-paginator .p-paginator-next").first
    await proximo.scroll_into_view_if_needed()
    await proximo.click()
    await page.wait_for_timeout(1500)


async def coletar_fornecedores_itens(page, arquivo_progresso: str | None = None) -> list[dict]:
    """Deve ser chamada com `page` já na tela de seleção de
    fornecedores (aba 'Fornecedores' visível — clique nela se
    necessário antes de chamar esta função).

    Se `arquivo_progresso` for informado, salva o resultado parcial
    (JSON) em disco depois de CADA fornecedor processado — uma
    varredura completa do pregão pode levar dezenas de minutos, e a
    sessão do Chrome já se mostrou instável nesse projeto (login caiu,
    conexão CDP travou), então não vale a pena arriscar perder tudo por
    causa de uma falha no meio do caminho.

    Expande cada fornecedor com itens habilitados > 0, entra em CADA
    item (avulso ou de grupo) via expandir_item_avulso_e_extrair() /
    expandir_grupo_e_coletar_itens(), e retorna uma lista de
    {"cnpj", "fornecedor", "itens": [...]} já com os itens totalmente
    detalhados (número, descrição, quantidade ofertada, marca, modelo,
    valor unitário — itens de grupo já vêm expandidos em itens
    individuais, sem entrada "eh_grupo" no resultado final).

    ATENÇÃO: isso navega bastante (entra e sai da tela de cada item),
    então é lento — para um fornecedor com N itens, são ~N idas e
    voltas. Trata paginação da lista "melhor classificado" (10 linhas
    por página, confirmado em teste real — um fornecedor com "19 de 19"
    linhas habilitadas tinha 10 na página 1 e 9 na página 2; sem tratar
    isso, os itens da página 2 eram silenciosamente perdidos mesmo o
    total de itens coletados "batendo" por coincidência com a contagem
    de linhas da página 1 sozinha).
    """
    aba_fornecedores = page.get_by_text("Fornecedores", exact=True).first
    if await aba_fornecedores.count() > 0:
        await aba_fornecedores.click()
        await page.wait_for_timeout(2000)

    # ":visible" é essencial aqui — a aba "Itens" (não selecionada)
    # continua no DOM escondida, e também usa a classe
    # cp-item-expansivel, então sem esse filtro os índices ficam
    # todos errados (itens ocultos entram na contagem antes dos
    # fornecedores de verdade).
    linhas = page.locator("div.cp-item-expansivel:visible")
    total = await linhas.count()
    log(f"Total de fornecedores na listagem: {total}")

    resultado = []

    for i in range(total):
        linha = linhas.nth(i)
        texto_linha_fechada = await linha.inner_text()

        m_habilitados = re.search(r"Itens habilitados\s*\n?\s*(\d+) de (\d+)", texto_linha_fechada)
        if not m_habilitados or m_habilitados.group(1) == "0":
            continue

        m_cnpj = re.search(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", texto_linha_fechada)
        cnpj = m_cnpj.group(0) if m_cnpj else ""

        nome_match = re.search(r"\n([A-ZÀ-Ÿ0-9][^\n]*)\nItens habilitados", texto_linha_fechada)
        nome = _limpar(nome_match.group(1)) if nome_match else ""

        expandiu = await _expandir_fornecedor(page, linha)
        if not expandiu:
            log(f"  ⚠ Não consegui expandir fornecedor {cnpj} — pulando.")
            continue

        itens_completos = []
        pagina_atual = 1

        while True:
            # Localiza a PRIMEIRA tabela dentro da linha do fornecedor — é
            # sempre a de "...é o melhor classificado" (vem antes da de
            # "...não é o melhor classificado" na ordem do documento).
            tabela_melhor = linha.locator("table").first
            linhas_tabela = tabela_melhor.locator("tbody tr")
            total_linhas_pagina = await linhas_tabela.count()

            for j in range(total_linhas_pagina):
                linha_item = linhas_tabela.nth(j)
                texto_item = await linha_item.inner_text()

                # Linha de grupo: "GRUPO 3 | 3 itens..." (começa com a
                # palavra GRUPO, não com dígito). Checar isso ANTES do
                # padrão de item avulso, senão nunca bate (não começa com
                # dígito) e a linha de grupo inteira é silenciosamente
                # pulada — bug real que já aconteceu aqui.
                m_grupo = re.match(r"GRUPO\s+(\d+)\s*\|\s*(\d+)\s*itens?", texto_item, re.IGNORECASE)
                m_num = None if m_grupo else re.match(r"(\d+)\s+([^\n]+)", texto_item)

                # Nenhum dos dois padrões bateu — pode ser a linha ainda
                # carregando (texto vazio/parcial) logo após trocar de
                # página/reabrir o fornecedor. Espera ativa e tenta de
                # novo antes de desistir; SEMPRE loga se perder a linha,
                # nunca pular um item em silêncio (já aconteceu: um
                # GRUPO inteiro sumiu sem nenhum log quando isso não
                # existia aqui).
                if not m_grupo and not m_num:
                    for _ in range(15):
                        await page.wait_for_timeout(300)
                        texto_item = await linha_item.inner_text()
                        m_grupo = re.match(r"GRUPO\s+(\d+)\s*\|\s*(\d+)\s*itens?", texto_item, re.IGNORECASE)
                        m_num = None if m_grupo else re.match(r"(\d+)\s+([^\n]+)", texto_item)
                        if m_grupo or m_num:
                            break

                if m_grupo:
                    log(f"    Grupo {m_grupo.group(1)} ({m_grupo.group(2)} itens)...")
                    itens_do_grupo = await expandir_grupo_e_coletar_itens(page, linha_item, cnpj)
                    itens_completos.extend(itens_do_grupo)
                    await _voltar(page, 2)
                else:
                    if not m_num:
                        log(f"  ⚠ Linha {j} do fornecedor {cnpj} não bateu em nenhum padrão (grupo/item) mesmo após esperar — PULADA. Texto: {texto_item[:100]!r}")
                        continue

                    # A linha compacta só tem o rótulo "Valor estimado :" seguido
                    # de DOIS números (estimado, depois ofertado, sem rótulo repetido).
                    m_valor = re.search(r"Valor estimado\s*:\s*R\$\s*([\d.,]+)\s*R\$\s*([\d.,]+)", texto_item)
                    detalhe = await expandir_item_avulso_e_extrair(page, linha_item, cnpj)
                    itens_completos.append({
                        "numero_item": m_num.group(1),
                        "descricao_curta": _limpar(m_num.group(2)),
                        "valor_unitario_ofertado": m_valor.group(2) if m_valor else "",
                        **detalhe,
                    })
                    await _voltar(page, 1)

                # Depois de voltar, o "Voltar" costuma cair na aba "Itens"
                # (estado padrão da URL), não mantém a aba "Fornecedores"
                # selecionada — precisa clicar nela nome antes de re-expandir.
                # Re-expandir também sempre volta a tabela pra PÁGINA 1, então
                # se estávamos numa página > 1, precisa re-navegar até ela.
                aba_fornecedores = page.get_by_text("Fornecedores", exact=True).first
                await aba_fornecedores.click()
                await page.wait_for_timeout(2000)

                linha = linhas.nth(i)
                await _expandir_fornecedor(page, linha)

                for _ in range(pagina_atual - 1):
                    await _ir_para_proxima_pagina(page, linha)

                tabela_melhor = linha.locator("table").first
                linhas_tabela = tabela_melhor.locator("tbody tr")

            if not await _tem_proxima_pagina(linha):
                break

            log(f"    Indo para página {pagina_atual + 1} de itens do fornecedor...")
            await _ir_para_proxima_pagina(page, linha)
            pagina_atual += 1

        # "Itens habilitados" no card do fornecedor conta LINHAS da
        # tabela (uma linha de grupo = 1 linha, mas vira N itens depois
        # de expandida) — não é diretamente comparável a len(itens_completos)
        # quando há grupos. Serve só de referência, não de validação exata.
        log(f"  {cnpj} {nome}: {len(itens_completos)} item(ns) coletados (linhas habilitadas no card: {m_habilitados.group(1)} de {m_habilitados.group(2)})")

        resultado.append({"cnpj": cnpj, "fornecedor": nome, "itens": itens_completos})

        if arquivo_progresso:
            Path(arquivo_progresso).write_text(
                json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    return resultado


def montar_identificador(uasg: str, numero: str, ano: str, modalidade: str = "05") -> str:
    """Monta o `identificador` usado na URL de seleção de fornecedores
    a partir de UASG + modalidade (05 = Pregão Eletrônico) + número
    (5 dígitos, zero-padded) + ano."""
    return f"{uasg}{modalidade}{int(numero):05d}{ano}"


# =========================================================
# CRIAÇÃO DA ATA (clonar + preencher) - AINDA NÃO IMPLEMENTADO
# =========================================================
# Ver notas técnicas no topo do arquivo: mecânica de clique em
# célula e inserção/remoção de linha já validada manualmente, mas
# ainda não encapsulada em funções reutilizáveis. Próximo passo,
# depois de fechar a coleta de dados.


async def main():
    try:
        abrir_chrome()

        async with async_playwright() as p:
            log("Conectando ao Chrome via CDP...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")

            if not browser.contexts:
                raise RuntimeError("Nenhum contexto encontrado no Chrome.")

        log("Nada a executar ainda — bot em construção. Use as funções deste "
            "módulo (extrair_itens_tr, baixar_pdf_artefato) individualmente por enquanto.")

    except Exception as e:
        log(f"ERRO FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
