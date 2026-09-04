import asyncio
import base64
import csv
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
import processos_repo

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

# Ata MODELO (nunca editar diretamente — só clonar a partir dela).
ARTEFATO_MODELO_NUMERO = "279"
ARTEFATO_MODELO_ANO = "2026"


async def navegar_para_artefatos_digitais(page_area_trabalho):
    """A partir de uma página já na Área de Trabalho, navega até a
    listagem de Artefatos Digitais (.../comprasnet-artefatos-web/leitor-artefato)
    clicando pela interface (o menu de módulos abre em uma aba NOVA,
    não navega a mesma página).

    Mecânica confirmada: o botão do avatar do usuário (canto superior
    direito, classe `.br-sign-in`) abre um menu com ícones de módulos
    (MentorIA, Pregão/Concorrência, Cotação/Dispensa, PGC, ETP,
    Artefatos Digitais, Gestão de Riscos, IRP Digital...); clicar em
    "Artefatos Digitais" abre a listagem numa aba nova.

    Retorna a nova Page (aba) já na listagem."""
    context = page_area_trabalho.context

    # fecha qualquer seleção/menu residual antes de abrir o menu de módulos
    await page_area_trabalho.mouse.click(50, 50)
    await page_area_trabalho.wait_for_timeout(500)

    gatilho = page_area_trabalho.locator(".br-sign-in").first
    if await gatilho.count() == 0:
        raise RuntimeError("Não achei o botão do avatar (.br-sign-in) na Área de Trabalho.")

    await gatilho.click()
    await page_area_trabalho.wait_for_timeout(1500)

    item_artefatos = page_area_trabalho.get_by_text("Artefatos Digitais", exact=False).first
    try:
        await item_artefatos.wait_for(state="visible", timeout=8000)
    except Exception:
        raise RuntimeError("Cliquei no avatar mas 'Artefatos Digitais' não apareceu no menu de módulos.")

    paginas_antes = set(context.pages)
    await item_artefatos.click()

    # a navegação abre uma aba NOVA — espera ela aparecer e carregar
    nova_pagina = None
    for _ in range(20):
        await page_area_trabalho.wait_for_timeout(500)
        novas = [p for p in context.pages if p not in paginas_antes]
        if novas:
            nova_pagina = novas[0]
            break
    if nova_pagina is None:
        raise RuntimeError("Cliquei em 'Artefatos Digitais' mas nenhuma aba nova apareceu.")

    try:
        await nova_pagina.wait_for_url(re.compile(r"leitor-artefato"), timeout=15000)
    except Exception:
        pass  # segue mesmo assim — a URL pode já estar certa antes do evento disparar

    await nova_pagina.wait_for_timeout(1500)
    return nova_pagina


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
                    # Limite generoso pra não descartar unidades reais
                    # mais longas — confirmado no PDF real: "ROLO COM
                    # 100 METROS" (20 caracteres) é uma unidade válida
                    # que o limite antigo (15) descartava, deixando o
                    # item de fora do dict inteiro (itens 77/78/79 do
                    # pregão 44/2026 sumiam silenciosamente).
                    if len(unidade_limpa) > 40 or not unidade_limpa:
                        continue
                    if len(especificacao_limpa) < 5 or not re.search(r"[A-Za-zÀ-ÿ]{3,}", especificacao_limpa):
                        continue

                    # No PDF, a coluna GRUPO mostra "-" explicitamente
                    # para itens avulsos (confirmado: item 80, pregão
                    # 44/2026). Precisa resetar grupo_atual pra "-"
                    # nesse caso — senão ele ficava "grudado" no último
                    # GRUPO numérico visto, marcando itens avulsos
                    # como se pertencessem a um grupo que já tinha
                    # acabado (bug real: item 80 saiu com "grupo": "6"
                    # quando deveria ser "-"). Célula VAZIA (sem "-" e
                    # sem número) é diferente — é a célula mesclada de
                    # continuação de um grupo real no PDF, e aí sim
                    # mantém grupo_atual como está.
                    grupo_limpo = _limpar(grupo)
                    m_grupo = re.match(r"^(\d+)\b", grupo_limpo)
                    if m_grupo:
                        grupo_atual = m_grupo.group(1)
                    elif grupo_limpo == "-":
                        grupo_atual = "-"

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

        try:
            await _processar_um_fornecedor(
                page, linhas, i, linha, cnpj, nome, m_habilitados, resultado
            )
        except Exception as e:
            # Um fornecedor com muitos itens/grupos pode levar muitos
            # minutos de idas e voltas — se o SPA degradar no meio
            # disso (confirmado: aconteceu numa varredura real, depois
            # de ~8min seguidos de navegação), uma exceção não tratada
            # aqui matava o processo inteiro e derrubava os fornecedores
            # ainda não processados junto. Loga, tenta recuperar um
            # estado conhecido (aba Fornecedores) e segue pro próximo —
            # o CONTROLE_BOT/log deixa claro que esse fornecedor ficou
            # incompleto, pra ser reprocessado depois em vez de travar
            # a varredura toda.
            log(f"  ⚠ Falha ao processar fornecedor {cnpj} ({nome}) — PULADO, retomando no próximo. Erro: {e}")
            try:
                aba_fornecedores_recuperar = page.get_by_text("Fornecedores", exact=True).first
                await aba_fornecedores_recuperar.click()
                await page.wait_for_timeout(2000)
            except Exception:
                pass

        if arquivo_progresso:
            Path(arquivo_progresso).write_text(
                json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    return resultado


async def _processar_um_fornecedor(page, linhas, i, linha, cnpj, nome, m_habilitados, resultado) -> None:
    """Processa um único fornecedor já localizado na lista (índice `i`),
    coleta todos os itens (com paginação) e adiciona o resultado a
    `resultado` (mutado in-place). Extraída de coletar_fornecedores_itens
    para poder envolver o processamento de CADA fornecedor num try/except
    sem derrubar os demais se um falhar no meio (ver nota no chamador)."""
    expandiu = await _expandir_fornecedor(page, linha)
    if not expandiu:
        log(f"  ⚠ Não consegui expandir fornecedor {cnpj} — pulando.")
        return

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


def montar_identificador(uasg: str, numero: str, ano: str, modalidade: str = "05") -> str:
    """Monta o `identificador` usado na URL de seleção de fornecedores
    a partir de UASG + modalidade (05 = Pregão Eletrônico) + número
    (5 dígitos, zero-padded) + ano."""
    return f"{uasg}{modalidade}{int(numero):05d}{ano}"


# =========================================================
# ETAPA 2 (Checkpoint 2) — ANCORAGEM NO processo_id
# =========================================================
# processos.json (Etapa 1 — Identificação do Processo) é a única fonte
# de verdade sobre qual Pregão/TR/Processo Administrativo/UASG foi
# confirmado pelo usuário. Nada aqui pode assumir que a `page` recebida
# está no lugar certo só porque quem chamou disse que está — sempre
# valida o processo_id (processos_repo.validar_processo) e confirma que
# a página realmente corresponde a esse pregão antes de raspar ou
# preencher qualquer coisa.


async def validar_pagina_pertence_ao_processo(page, processo: dict) -> None:
    """Confirma que a `page` atual (já na tela de Seleção de
    Fornecedores) corresponde de fato ao pregão do `processo` validado,
    lendo o parâmetro `identificador=` da própria URL — nunca confia
    que quem chamou navegou pro lugar certo. Levanta
    processos_repo.ProcessoNaoConfirmado se não bater."""
    esperado = montar_identificador(processo["uasg"], processo["numeroPregao"], processo["anoPregao"])
    if f"identificador={esperado}" not in page.url:
        raise processos_repo.ProcessoNaoConfirmado(
            f"A página atual não corresponde ao pregão do processo {processo.get('pregaoCompleto')!r} "
            f"(esperado identificador={esperado!r} na URL, url atual: {page.url})."
        )


async def coletar_fornecedores_itens_do_processo(
    page, processo_id: str, arquivo_progresso: str | None = None
) -> list[dict]:
    """Mesma coleta de coletar_fornecedores_itens(), mas ancorada num
    processo_id confirmado: valida o processo E confirma que `page`
    está de fato no pregão desse processo antes de raspar qualquer
    dado — nunca carrega itens de um pregão diferente do confirmado.
    Não altera a lógica de raspagem em si (coletar_fornecedores_itens
    permanece intocada, regra de "melhor classificado" preservada)."""
    processo = processos_repo.validar_processo(processo_id)
    await validar_pagina_pertence_ao_processo(page, processo)
    return await coletar_fornecedores_itens(page, arquivo_progresso)


# =========================================================
# CRIAÇÃO DA ATA (clonar + preencher)
# =========================================================
# Ver notas técnicas no topo do arquivo: mecânica de clique em
# célula e inserção/remoção de linha já validada manualmente, mas
# ainda não encapsulada em funções reutilizáveis (preenchimento da
# tabela em si vem depois de clonar_ata_modelo()).
#
# Tela: .../comprasnet-artefatos-web/leitor-artefato (listagem de
# Artefatos Digitais). Cada artefato é uma linha com 4 ícones de ação:
# fa-pencil-square-o (Editar — NUNCA usar no modelo 279), fa-copy
# (Clonar — "Criar um novo artefato do tipo ARP ... a partir deste"),
# fa-eye (Visualizar) e fa-ellipsis-v (mais opções). Confirmado
# clicando manualmente: "Clonar" já abre automaticamente o editor do
# clone recém-criado (.../artefatos/edit/<id>?artefato=<novo>/<ano>&tipo=ARP),
# não precisa de nenhuma navegação extra depois do clique.


def obter_modelo_ata(processo_id: str) -> dict:
    """ETAPA 2 (Checkpoint 3): determina qual Ata-modelo deve ser
    clonada para esse `processo_id`.

    Valida o processo_id (processos_repo.validar_processo — nunca
    recebe e ignora) e deriva o pregão do próprio processo validado,
    nunca de parâmetro solto. Por enquanto SEMPRE retorna o modelo fixo
    (ARTEFATO_MODELO_NUMERO/ANO, a Ata 279) — não existe ainda nenhuma
    regra real de "modelo por pregão" (isso exigiria descobrir
    automaticamente no Compras.gov.br qual é o modelo certo de cada
    pregão, o que não foi investigado; não inventar essa regra).

    A comparação ao vivo entre a Ata 279 e a Ata 283 (Etapa 2,
    investigação) confirmou que esse modelo fixo tem a estrutura
    esperada para o pregão 44/2026 — mas isso NÃO é garantia geral pra
    qualquer outro pregão (a Ata 267 já mostrou um clone com estrutura
    diferente). Esta função existe pra isolar essa decisão num único
    lugar: no futuro, uma regra real de seleção por pregão troca só o
    corpo desta função, sem precisar mexer em quem chama.

    Retorna {"numero", "ano", "numero_pregao"} — `numero_pregao` é só
    contexto (não influencia a escolha ainda)."""
    processo = processos_repo.validar_processo(processo_id)
    return {
        "numero": ARTEFATO_MODELO_NUMERO,
        "ano": ARTEFATO_MODELO_ANO,
        "numero_pregao": processo["pregaoCompleto"],
    }


async def clonar_ata_modelo(
    page, numero_modelo: str = ARTEFATO_MODELO_NUMERO, ano_modelo: str = ARTEFATO_MODELO_ANO,
) -> str:
    """Clica em 'Clonar' (ícone fa-copy) na linha do artefato MODELO
    (por padrão ARTEFATO_MODELO_NUMERO/ARTEFATO_MODELO_ANO, isto é, a
    Ata 279 — mas aceita outro modelo via `numero_modelo`/`ano_modelo`,
    ver obter_modelo_ata) na tela de listagem de Artefatos Digitais.
    NUNCA clica em Editar nessa linha — só em Clonar, pra nunca alterar
    o modelo original.

    Deve ser chamada com `page` já na tela de listagem
    (.../comprasnet-artefatos-web/leitor-artefato).

    Retorna o identificador do clone recém-criado (ex: "295/2026"),
    extraído da URL depois que o sistema navega automaticamente pro
    editor dele.
    """
    identificador_modelo = f"{numero_modelo}/{ano_modelo}"

    # A listagem é paginada, com ordenação que não é puramente
    # cronológica nem numérica — confirmado em teste real que o
    # artefato modelo podia sumir da página 1 conforme mais clones iam
    # sendo criados (a listagem inteira cresce e reordena). Navegar
    # pela paginação se mostrou pouco confiável; o campo de busca por
    # texto é a forma robusta de achar — buscar só pelo NÚMERO (sem
    # "/ano") filtra exatamente pro artefato modelo, confirmado em
    # teste real (buscar "279/2026" com a barra não filtrava; "279"
    # sozinho filtrava certo).
    campo_busca = page.locator("input[placeholder*='Pesquise']").first
    if await campo_busca.count() == 0:
        raise RuntimeError("Não achei o campo de busca da listagem de Artefatos Digitais.")

    await campo_busca.click()
    await campo_busca.fill(numero_modelo)
    await campo_busca.press("Enter")
    await page.wait_for_timeout(4000)

    linha_num = page.get_by_text(identificador_modelo, exact=True).first
    # Espera ativa — mesma classe de bug de timing confirmada no resto
    # do arquivo (ver expandir_item_avulso_e_extrair). Confirmado que a
    # resposta dessa busca pode ser bem mais lenta que o resto do
    # sistema (>10s às vezes) — timeout generoso, isso só roda 1x por
    # clone criado.
    try:
        await linha_num.wait_for(state="visible", timeout=20000)
    except Exception:
        raise RuntimeError(
            f"Não achei a linha do artefato modelo {identificador_modelo} na listagem de Artefatos Digitais "
            f"mesmo após buscar por {numero_modelo!r}."
        )

    linha_container = linha_num.locator("xpath=ancestor::tr[1]")
    botao_clonar = linha_container.locator("button:has(i.fa-copy)").first
    if await botao_clonar.count() == 0:
        raise RuntimeError("Não achei o botão 'Clonar' (ícone fa-copy) na linha do artefato modelo.")

    await botao_clonar.scroll_into_view_if_needed()
    await botao_clonar.click()

    # O clique abre um modal de confirmação ("Deseja copiar o ARP...
    # 279/2026?" com botões Cancelar/Copiar) — confirmado só ao testar
    # de verdade, não documentado antes. É um modal Bootstrap
    # (<modal-container class="modal fade show">), não PrimeNG — o
    # seletor errado (p-dialog) já causou um bug real aqui: o "achou
    # modal?" checava p-dialog, nunca achava, então o modal ficava
    # aberto bloqueando cliques seguintes ("intercepts pointer events").
    modal = page.locator("modal-container, .modal.show").filter(has_text="Deseja copiar").first
    try:
        await modal.wait_for(state="visible", timeout=8000)
    except Exception:
        raise RuntimeError("Cliquei em 'Clonar' mas o modal de confirmação ('Deseja copiar...') não apareceu.")

    botao_copiar = modal.get_by_text("Copiar", exact=True).first
    await botao_copiar.click()

    try:
        await page.wait_for_url(re.compile(r"/artefatos/edit/\d+\?artefato="), timeout=15000)
    except Exception:
        raise RuntimeError(
            f"Cliquei em 'Clonar' mas a navegação para o editor do clone não aconteceu a tempo (url atual: {page.url})."
        )

    m = re.search(r"artefato=(\d+)(?:%2F|/)(\d+)", page.url)
    if not m:
        raise RuntimeError(f"Clone criado, mas não consegui extrair o identificador da URL: {page.url}")

    identificador_clone = f"{m.group(1)}/{m.group(2)}"
    log(f"Ata modelo clonada com sucesso: {identificador_clone}")
    return identificador_clone


# =========================================================
# EDITOR DO CLONE — navegação até a seção e preenchimento
# =========================================================
# A tela do editor (.../artefatos/edit/<id>?artefato=<num>/<ano>&tipo=ARP)
# tem uma barra lateral "SEÇÕES DO DOCUMENTO" organizada como 4
# accordions Bootstrap (#collapse-title-0..3 / #collapse-0..3):
# "Informações Básicas", "ATA DE REGISTRO DE PREÇOS" (contém as 12
# subseções numeradas do texto, incluindo "2. DOS PREÇOS, ESPECIFICAÇÕES
# E QUANTITATIVOS" e "3. ÓRGÃO(S) GERENCIADOR E PARTICIPANTE(S)"),
# "Responsáveis" e "Anexos". Os accordions fecham sozinhos entre
# interações (confirmado: abrir e clicar num subitem em execuções
# separadas falha porque o accordion já tinha fechado de novo) — por
# isso abrir_secao_documento() sempre checa e reabre o accordion pai
# antes de clicar no subitem, na mesma chamada.
#
# Clicar numa subseção abre o corpo dela como um editor rich text
# (provavelmente TinyMCE/CKEditor) dentro de um <iframe> cujo `src` é
# 'about:blank' mesmo depois de carregado — é preciso usar
# `page.frames[1]` (o segundo frame da página, não o `name`/`url` dele)
# pra acessar o conteúdo real.
#
# Tabela de "DOS PREÇOS, ESPECIFICAÇÕES E QUANTITATIVOS" (validado
# manualmente no clone 297/2026): tem 3 <tr> —
#   tr[0]: "Grupo do TR" (rowspan=2), "Item do TR" (rowspan=2),
#          "Fornecedor:/cnpj:/endereço:/contatos:/representante:"
#          (colspan=8) — um bloco de 5 <p> separados, um por rótulo,
#          que fica ACIMA de toda a tabela de itens (cada Ata/clone é
#          de UM fornecedor só; não é uma coluna por linha de item).
#   tr[1]: cabeçalho das 8 colunas de dado: Especificação, Marca,
#          Modelo, Unidade, Quantidade Máxima, Quantidade Mínima,
#          Valor Unitário, Prazo de validade.
#   tr[2]: primeira (e única, no modelo) linha de dados — 10 <td>
#          vazios + "12 Meses" já preenchido no Prazo de validade.
# Preencher uma célula: clicar no <p>/<td>, `press("End")` pra ir pro
# fim do texto existente, depois `type()` o valor. Confirmado
# funcionando: preencher só "Fornecedor:" e "cnpj:" na célula de
# cabeçalho, sem tocar em endereço/contatos/representante.
#
# ETAPA 2 (Checkpoint 1): comparação ao vivo entre a Ata 279 (modelo) e
# a Ata 283 (concluída, pregão 44/2026) confirmou que as duas têm
# EXATAMENTE a mesma estrutura de 10 colunas nessa tabela, na mesma
# ordem. Mas uma investigação anterior (Ata 267) já mostrou um clone
# com 9 colunas — índice fixo funciona hoje, mas é uma aposta. Por
# isso o preenchimento passa a resolver o índice de cada campo pelo
# NOME do cabeçalho (ver mapear_colunas/ler_mapa_colunas_precos
# abaixo), nunca por posição — mesmo princípio de "casar por nome,
# nunca por posição" já usado no ATMOS (sync_service.py).


class EstruturaAtaNaoReconhecida(Exception):
    """Levantada quando a tabela de preços ou a célula do fornecedor de
    um clone não tem a estrutura esperada. Nunca deve ser "contornada"
    deslocando valores para outra coluna — é sempre um sinal para parar
    e revisar manualmente o clone/modelo antes de continuar."""


COLUNAS_OBRIGATORIAS = [
    "grupo do tr", "item do tr", "especificação", "marca", "modelo",
    "unidade", "quantidade máxima", "quantidade mínima", "valor unitário",
    "prazo de validade",
]

# Chave usada nos dicts de item (ver montar_itens_para_preenchimento) ->
# nome normalizado do cabeçalho real da tabela de preços.
CHAVE_PARA_CABECALHO = {
    "grupo_tr": "grupo do tr",
    "item_tr": "item do tr",
    "especificacao": "especificação",
    "marca": "marca",
    "modelo": "modelo",
    "unidade": "unidade",
    "quantidade_maxima": "quantidade máxima",
    "quantidade_minima": "quantidade mínima",
    "valor_unitario": "valor unitário",
    "prazo_validade": "prazo de validade",
}


def _normalizar_cabecalho(texto: str) -> str:
    """Tolera maiúsculas/minúsculas, espaços extras e quebras de linha —
    o cabeçalho real vem de células de rich-text e pode variar em
    formatação sem mudar de significado."""
    return re.sub(r"\s+", " ", (texto or "")).strip().lower()


def mapear_colunas(textos_cabecalho: list[str]) -> dict[str, int]:
    """Monta {nome_normalizado_da_coluna: índice} a partir dos textos das
    células da linha de cabeçalho (tr[1] da tabela 'DOS PREÇOS'). Função
    pura — não toca em Playwright, testável sem navegador."""
    return {
        _normalizar_cabecalho(texto): indice
        for indice, texto in enumerate(textos_cabecalho)
        if _normalizar_cabecalho(texto)
    }


def validar_mapa_colunas(mapa: dict, identificador_ata: str, numero_pregao: str) -> None:
    """Levanta EstruturaAtaNaoReconhecida se qualquer coluna obrigatória
    não foi encontrada no cabeçalho real. Chamada ANTES de preencher
    qualquer item — nunca preenche parcialmente uma estrutura não
    reconhecida."""
    faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in mapa]
    if faltando:
        raise EstruturaAtaNaoReconhecida(
            "Estrutura da Ata não reconhecida\n\n"
            f"Coluna ausente: {faltando[0]!r}\n\n"
            f"Modelo: {identificador_ata}\n"
            f"Pregão: {numero_pregao}"
        )


async def ler_mapa_colunas_precos(frame_editor) -> dict[str, int]:
    """Lê o cabeçalho real (tr[1]) da tabela de preços JÁ ABERTA em
    `frame_editor` (ver abrir_secao_documento) e monta o mapa de
    colunas. IMPORTANTE: só lê dentro desse frame já escopado pra seção
    'DOS PREÇOS' — nunca faz busca no documento inteiro. Isso importa
    porque a seção '12. ANEXO — Cadastro Reserva' tem uma tabela com
    cabeçalho quase idêntico a essa; escopar ao frame certo evita
    qualquer ambiguidade sem precisar de heurística extra."""
    tabela = frame_editor.locator("table").first
    celulas_cabecalho = tabela.locator("tr").nth(1).locator("td, th")
    total = await celulas_cabecalho.count()
    textos = [await celulas_cabecalho.nth(i).inner_text() for i in range(total)]
    return mapear_colunas(textos)


def celula_fornecedor_valida(texto_celula: str) -> bool:
    """Confirma que a célula de cabeçalho da tabela de preços realmente
    contém o bloco Fornecedor/CNPJ/endereço/contato antes de deixar
    preencher_fornecedor_cnpj escrever nela — nunca escreve numa célula
    que não parece ser essa, mesmo que o índice (tr[0]/td[2]) bata."""
    txt = (texto_celula or "").lower()
    return all(marcador in txt for marcador in ["fornecedor", "cnpj", "endereço", "contato"])


async def abrir_secao_documento(page, indice_accordion: int, texto_subitem: str):
    """Garante que o accordion `indice_accordion` (0-3, ver notas acima)
    está aberto e clica no subitem cujo texto contenha `texto_subitem`
    (ex: "DOS PREÇOS" ou "GERENCIADOR"). Retorna o frame do editor rich
    text da subseção (page.frames[1] depois do clique).

    Sempre checa/reabre o accordion na mesma chamada — confirmado que
    ele fecha sozinho entre interações separadas."""
    titulo = page.locator(f"#collapse-title-{indice_accordion}")
    conteudo = page.locator(f"#collapse-{indice_accordion}")

    cls = await conteudo.get_attribute("class") or ""
    if "show" not in cls:
        await titulo.click()
        await page.wait_for_timeout(1000)

    subitem = conteudo.get_by_text(texto_subitem, exact=False).first
    try:
        await subitem.wait_for(state="visible", timeout=8000)
    except Exception:
        raise RuntimeError(
            f"Não achei/não consegui abrir o subitem contendo {texto_subitem!r} no accordion {indice_accordion}."
        )

    await subitem.click()
    await page.wait_for_timeout(2500)

    if len(page.frames) < 2:
        raise RuntimeError(
            f"Cliquei em {texto_subitem!r} mas o iframe do editor rich text não apareceu (frames: {len(page.frames)})."
        )

    return page.frames[1]


async def preencher_fornecedor_cnpj(frame_editor, nome_fornecedor: str, cnpj: str) -> None:
    """Na tabela de 'DOS PREÇOS, ESPECIFICAÇÕES E QUANTITATIVOS' (já
    aberta em `frame_editor`, ver abrir_secao_documento), preenche
    APENAS os rótulos 'Fornecedor:' e 'cnpj:' da célula de cabeçalho
    (tr[0], td[2]) com os valores dados. NUNCA toca em endereço:,
    contatos: ou representante: — regra do projeto: só a Fornecedor e
    o CNPJ são preenchidos automaticamente, o resto fica para
    preenchimento manual humano.

    Antes de escrever, confirma que a célula realmente contém o bloco
    Fornecedor/CNPJ/endereço/contato (ver celula_fornecedor_valida) —
    nunca escreve só confiando no índice tr[0]/td[2], mesmo esse índice
    já tendo sido confirmado nas Atas 279 e 283."""
    tabela = frame_editor.locator("table").first
    celula_fornecedor = tabela.locator("tr").nth(0).locator("td, th").nth(2)

    texto_atual = await celula_fornecedor.inner_text()
    if not celula_fornecedor_valida(texto_atual):
        raise EstruturaAtaNaoReconhecida(
            "Estrutura da Ata não reconhecida\n\n"
            "A célula esperada para Fornecedor/CNPJ (tr[0]/td[2]) não contém "
            "os rótulos esperados (Fornecedor/CNPJ/endereço/contato).\n\n"
            f"Conteúdo encontrado: {texto_atual[:200]!r}"
        )

    paragrafos = celula_fornecedor.locator("p")

    p_fornecedor = paragrafos.nth(0)
    p_cnpj = paragrafos.nth(1)

    await p_fornecedor.click()
    await frame_editor.locator("body").press("End")
    await frame_editor.locator("body").type(f" {nome_fornecedor}")

    await p_cnpj.click()
    await frame_editor.locator("body").press("End")
    await frame_editor.locator("body").type(f" {cnpj}")

    log(f"Fornecedor/CNPJ preenchidos no cabeçalho da tabela de itens: {nome_fornecedor} / {cnpj}")


# =========================================================
# TABELA DE ITENS — inserir linhas + preencher
# =========================================================
# O clique direito nativo do Playwright (.click(button="right")) NÃO
# abre o menu de contexto desse editor — confirmado, é preciso simular
# via CDP puro (Input.dispatchMouseEvent). O menu abre em um frame NOVO
# (não necessariamente o último de page.frames — frames de menus
# anteriores que não foram fechados continuam na lista), por isso
# _achar_item_menu_visivel() procura o texto em TODOS os frames e pega
# o primeiro visível, em vez de assumir um índice fixo.
#
# Cadeia de cliques pra inserir uma linha (validada no clone 298/2026):
# botão direito numa célula da linha de referência -> menu ("Colar",
# "Célula", "Linha", "Coluna", "Apagar Tabela", "Formatar Tabela") ->
# clicar em "Linha" -> submenu ("Inserir linha acima", "Inserir linha
# abaixo", ...) -> clicar na opção desejada.
#
# IMPORTANTE: a linha inserida vem com TODAS as 10 células vazias —
# diferente da linha original do modelo, que já vem com "12 Meses" no
# Prazo de validade. Cada linha inserida precisa desse campo preenchido
# manualmente também (preencher_linha_item cuida disso).


async def _achar_item_menu_visivel(page, texto: str, exato: bool = True):
    """Procura `texto` em TODOS os frames da página (não só o frame do
    editor) e retorna o primeiro elemento visível encontrado, ou None.
    Necessário porque os menus de contexto desse editor abrem em frames
    dinâmicos que não têm índice/nome previsível."""
    for fr in page.frames:
        try:
            candidatos = fr.get_by_text(texto, exact=exato)
            n = await candidatos.count()
            for i in range(n):
                el = candidatos.nth(i)
                if await el.is_visible():
                    return el
        except Exception:
            continue
    return None


async def inserir_linha_tabela(page, celula_referencia, abaixo: bool = True) -> None:
    """Clica com botão direito (via CDP) na `celula_referencia` de uma
    linha da tabela de itens e insere uma nova linha acima ou abaixo
    dela, usando o menu de contexto do editor (Linha > Inserir linha
    acima/abaixo)."""
    box = await celula_referencia.bounding_box()
    if box is None:
        raise RuntimeError("Não consegui obter a posição da célula de referência para o clique direito.")

    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2

    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "right", "clickCount": 1})
    await cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "right", "clickCount": 1})
    await page.wait_for_timeout(1200)

    item_linha = await _achar_item_menu_visivel(page, "Linha")
    if item_linha is None:
        raise RuntimeError("Cliquei com botão direito na célula, mas não achei o item 'Linha' do menu de contexto.")
    await item_linha.click()
    await page.wait_for_timeout(1000)

    texto_opcao = "Inserir linha abaixo" if abaixo else "Inserir linha acima"
    item_opcao = await _achar_item_menu_visivel(page, texto_opcao, exato=False)
    if item_opcao is None:
        raise RuntimeError(f"Abri o submenu 'Linha' mas não achei a opção {texto_opcao!r}.")
    await item_opcao.click()
    await page.wait_for_timeout(1500)


async def preencher_linha_item(frame_editor, linha_tr, item: dict, mapa_colunas: dict) -> None:
    """Preenche as células de UMA linha de dado da tabela de itens (já
    localizada em `linha_tr`, um <tr>) com os campos de `item`:
    grupo_tr, item_tr, especificacao, marca, modelo, unidade,
    quantidade_maxima, quantidade_minima, valor_unitario e (opcional)
    prazo_validade (default "12 Meses" — a linha original do modelo já
    vem com isso preenchido, mas linhas inseridas via
    inserir_linha_tabela() vêm totalmente vazias).

    `mapa_colunas` (ver ler_mapa_colunas_precos/mapear_colunas) resolve
    o índice REAL de cada coluna pelo nome do cabeçalho — nunca por
    posição fixa, pra não escrever num campo errado silenciosamente se
    um modelo tiver colunas em ordem/quantidade diferente (ver
    validar_mapa_colunas, chamada antes disso em criar_ata_fornecedor).

    Nunca inventa dado: se uma chave obrigatória faltar em `item`,
    levanta erro em vez de deixar a célula em branco silenciosamente
    (regra do projeto — quem montar `item` é responsável por garantir
    que os dados vieram de fonte confiável antes de chamar isso)."""
    campos_obrigatorios = [
        "grupo_tr", "item_tr", "especificacao", "marca", "modelo",
        "unidade", "quantidade_maxima", "quantidade_minima", "valor_unitario",
    ]
    faltando = [c for c in campos_obrigatorios if not item.get(c)]
    if faltando:
        raise ValueError(f"Campo(s) obrigatório(s) faltando para preencher a linha do item: {faltando}")

    valores_por_chave = {
        "grupo_tr": item["grupo_tr"],
        "item_tr": item["item_tr"],
        "especificacao": item["especificacao"],
        "marca": item["marca"],
        "modelo": item["modelo"],
        "unidade": item["unidade"],
        "quantidade_maxima": item["quantidade_maxima"],
        "quantidade_minima": item["quantidade_minima"],
        "valor_unitario": item["valor_unitario"],
        "prazo_validade": item.get("prazo_validade", "12 Meses"),
    }

    celulas = linha_tr.locator("td, th")
    for chave, valor in valores_por_chave.items():
        indice = mapa_colunas[CHAVE_PARA_CABECALHO[chave]]
        celula = celulas.nth(indice)
        texto_atual = (await celula.inner_text()).strip()
        if texto_atual:
            # célula já tem conteúdo (ex: "12 Meses" da linha original) —
            # não duplica, só sobrescreve se o valor pedido for diferente.
            if texto_atual == str(valor):
                continue
            # Seleciona só o texto DENTRO da célula (Home + Shift+End),
            # não Ctrl+A — isso evitar risco de selecionar além da
            # célula (linha inteira/tabela/documento) e apagar algo que
            # não devia.
            await celula.click()
            await frame_editor.locator("body").press("Home")
            await frame_editor.locator("body").press("Shift+End")
        else:
            await celula.click()
        await frame_editor.locator("body").type(str(valor))
        await frame_editor.wait_for_timeout(150)


async def preencher_tabela_itens(page, frame_editor, itens: list[dict], mapa_colunas: dict) -> None:
    """Preenche a tabela de 'DOS PREÇOS, ESPECIFICAÇÕES E QUANTITATIVOS'
    com a lista completa de itens de UM fornecedor (a mesma tabela já
    deve ter Fornecedor/CNPJ preenchidos via preencher_fornecedor_cnpj).

    `mapa_colunas` deve vir de ler_mapa_colunas_precos() + já ter
    passado por validar_mapa_colunas() — essa função não valida de
    novo, só usa o mapa pra resolver os índices reais.

    Cada item de `itens` deve ter as chaves exigidas por
    preencher_linha_item (grupo_tr, item_tr, especificacao, marca,
    modelo, unidade, quantidade_maxima, quantidade_minima,
    valor_unitario, prazo_validade opcional). O primeiro item usa a
    linha de dado que já existe no modelo (tr[2]); para cada item
    adicional, insere uma linha nova abaixo da última preenchida antes
    de preencher.

    Se `itens` estiver vazio, levanta erro — nunca deixa uma ata sem
    nenhum item (isso indicaria um bug no chamador, não um caso válido)."""
    if not itens:
        raise ValueError("Lista de itens vazia — não é um cenário válido para preencher a tabela de uma ata.")

    tabela = frame_editor.locator("table").first
    linha_atual = tabela.locator("tr").nth(2)

    await preencher_linha_item(frame_editor, linha_atual, itens[0], mapa_colunas)
    log(f"  Item 1/{len(itens)} preenchido (item TR {itens[0].get('item_tr')}).")

    for i, item in enumerate(itens[1:], start=2):
        primeira_celula = linha_atual.locator("td, th").nth(0)
        await inserir_linha_tabela(page, primeira_celula, abaixo=True)

        # a linha recem-inserida e sempre a proxima apos a ultima
        # preenchida — reconta pra pegar o indice certo (a tabela pode
        # ter crescido).
        total_linhas = await tabela.locator("tr").count()
        linha_atual = tabela.locator("tr").nth(total_linhas - 1)

        await preencher_linha_item(frame_editor, linha_atual, item, mapa_colunas)
        log(f"  Item {i}/{len(itens)} preenchido (item TR {item.get('item_tr')}).")

    log(f"Tabela de itens preenchida: {len(itens)} item(ns) no total.")


# =========================================================
# TABELAS DE QUANTIDADES UGG/UGP (seção "ÓRGÃO(S) GERENCIADOR E
# PARTICIPANTE(S)") — ler coluna A + remover itens de outros fornecedores
# =========================================================
# A seção tem 4 tabelas (validado no clone 299/2026, pregão 44/2026):
#   [0] "QUANTIDADES TOTAIS DAS UGG E UGPs"
#   [1] "QUANTIDADES MÍNIMAS POR REQUISIÇÕES..."
#   [2] "QUANTIDADES MÁXIMAS POR REQUISIÇÕES..."
#   [3] Legenda letra->UGG/UGP (A = a própria UASG do pregão, sempre a
#       primeira/UGG — confirmado A = 160082 = UASG do pregão 44/2026)
# As tabelas [0][1][2] têm as mesmas linhas de dado (GRUPO, ITEM,
# UNIDADE DE MEDIDA, colunas A-G, [TOTAL só na 0]) — já vêm com TODOS
# os grupos/itens do pregão inteiro, não só os do fornecedor alvo.
#
# Coluna GRUPO usa rowspan (só a primeira linha de cada grupo tem
# célula própria; as seguintes do mesmo grupo têm 1 célula a menos).
# Itens avulsos (fora de grupo) mostram "-" na coluna GRUPO.
#
# Confirmado pelo usuário: "Quantidade Mínima"/"Quantidade Máxima" da
# tabela de preços vêm da COLUNA A das tabelas [1]/[2] respectivamente
# (não de nenhum dado coletado do sistema de seleção de fornecedores).
# Depois de preencher a tabela de preços, as linhas das tabelas
# [0][1][2] cujo item não pertence ao fornecedor alvo devem ser
# REMOVIDAS (mantendo intactas as colunas A-G e a estrutura da tabela —
# confirmado que o editor reajusta o rowspan da coluna GRUPO sozinho ao
# remover uma linha do meio de um grupo).


async def extrair_tabela_quantidades(frame_editor, indice_tabela: int) -> dict:
    """Lê a tabela de quantidades (índice 0=Totais, 1=Mínimas,
    2=Máximas) da seção UGG/UGP e retorna
    {numero_item: {"grupo": str, "unidade": str, "col_a": str}}.

    Trata o rowspan da coluna GRUPO: uma linha sem célula própria de
    grupo (9 células em vez de 10, ou 10 em vez de 11 na tabela de
    Totais) herda o grupo da última linha que teve célula própria."""
    tabela = frame_editor.locator("table").nth(indice_tabela)
    linhas = tabela.locator("tr")
    total_linhas = await linhas.count()

    # A tabela de Totais (índice 0) tem 1 coluna a mais (TOTAL no fim)
    # que as outras duas — o número de células que indica "tem célula
    # própria de grupo" muda conforme a tabela. Confirmado em teste
    # real: usar o mesmo critério (10,11) pras 3 tabelas quebrava
    # silenciosamente a leitura da tabela de Totais, porque nela 10
    # células significa SEM grupo próprio (o inverso das outras duas).
    nc_com_grupo, nc_sem_grupo = (11, 10) if indice_tabela == 0 else (10, 9)

    resultado = {}
    grupo_atual = None

    for r in range(2, total_linhas):  # pula as 2 linhas de título
        linha = linhas.nth(r)
        celulas = linha.locator("td, th")
        nc = await celulas.count()

        if nc == nc_com_grupo:
            grupo_atual = (await celulas.nth(0).inner_text()).strip()
            item = (await celulas.nth(1).inner_text()).strip()
            unidade = (await celulas.nth(2).inner_text()).strip()
            col_a = (await celulas.nth(3).inner_text()).strip()
        elif nc == nc_sem_grupo:
            item = (await celulas.nth(0).inner_text()).strip()
            unidade = (await celulas.nth(1).inner_text()).strip()
            col_a = (await celulas.nth(2).inner_text()).strip()
        else:
            raise RuntimeError(
                f"Linha {r} da tabela [{indice_tabela}] tem {nc} célula(s), formato inesperado "
                f"(esperado {nc_com_grupo} ou {nc_sem_grupo}) — abortando em vez de adivinhar a estrutura."
            )

        if not item or not item.isdigit():
            continue

        resultado[item] = {"grupo": grupo_atual, "unidade": unidade, "col_a": col_a}

    return resultado


async def _celula_item_da_linha(linha_tr, indice_tabela: int):
    """Retorna o locator da célula 'ITEM' de uma linha de dado das
    tabelas de quantidade UGG/UGP. Essa célula NUNCA tem rowspan
    (diferente da célula de GRUPO, que pode cobrir várias linhas) —
    importante porque a bounding_box de uma célula com rowspan grande
    cobre TODAS as linhas que ela abrange, e calcular o centro dela
    para posicionar um clique pode cair numa linha errada (bug real
    confirmado em teste: um arraste pretendendo cobrir 4 linhas
    removeu 5, porque o ponto de início foi calculado a partir do
    centro vertical de uma célula de GRUPO com rowspan=5).

    `indice_tabela` importa: a tabela de Totais (0) tem 1 coluna a mais
    (TOTAL) que Mínimas/Máximas (1/2), então a contagem de células que
    indica "linha tem célula própria de grupo" é diferente entre elas
    (bug real confirmado: usar o mesmo critério pras 3 tabelas fazia a
    remoção pegar a célula errada só na tabela de Totais)."""
    nc_com_grupo = 11 if indice_tabela == 0 else 10
    celulas = linha_tr.locator("td, th")
    nc = await celulas.count()
    return celulas.nth(1 if nc == nc_com_grupo else 0)


async def remover_itens_nao_pertencentes_ugg(page, frame_editor, numeros_item_fornecedor: set) -> None:
    """Nas 3 tabelas de quantidades (Totais/Mínimas/Máximas, índices
    0/1/2) da seção UGG/UGP, remove todas as linhas cujo número de item
    NÃO esteja em `numeros_item_fornecedor`. Preserva a estrutura
    (colunas A-G, rowspan de GRUPO ajustado automaticamente pelo
    editor) — só remove linha inteira via 'Remover Linhas' do menu de
    contexto, nunca edita célula a célula aqui.

    Remove uma linha por vez (clique direito simples, sem arraste). Uma
    versão anterior tentava selecionar blocos de várias linhas de uma
    vez via arraste (bem mais rápida), mas se mostrou fundamentalmente
    não confiável nesse editor — em testes reais, a seleção por arraste
    ocasionalmente travava sem cobrir a linha final do bloco, em blocos
    de posições diferentes na tabela (não só casos de borda como a
    última linha), mesmo depois de várias tentativas de correção
    (blocos menores, scroll centralizado, direção invertida, validação
    prévia da seleção com retry). Clique direito único numa linha, sem
    depender de arraste, nunca falhou em nenhum teste — mais lento, mas
    correto é mais importante que rápido aqui.

    Remove sempre de baixo pra cima (maior índice primeiro), porque
    remover uma linha desloca os índices das linhas abaixo dela."""
    for indice_tabela in range(3):
        tabela = frame_editor.locator("table").nth(indice_tabela)

        # 1ª passada (leitura, de cima pra baixo): monta a lista de
        # índices de linha a remover.
        linhas = tabela.locator("tr")
        total_linhas = await linhas.count()
        indices_para_remover = []

        for r in range(2, total_linhas):
            linha = linhas.nth(r)
            celula_item = await _celula_item_da_linha(linha, indice_tabela)
            item = (await celula_item.inner_text()).strip()

            if item.isdigit() and item not in numeros_item_fornecedor:
                indices_para_remover.append(r)

        log(f"  Tabela [{indice_tabela}]: removendo {len(indices_para_remover)} de {total_linhas - 2} linha(s) de item.")

        # 2ª passada (remoção, de baixo pra cima).
        cdp = await page.context.new_cdp_session(page)
        for r in reversed(indices_para_remover):
            linha = tabela.locator("tr").nth(r)
            celula = await _celula_item_da_linha(linha, indice_tabela)

            await celula.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
            await page.wait_for_timeout(200)

            box = await celula.bounding_box()
            if box is None:
                raise RuntimeError(
                    f"Não consegui posicionar a linha {r} da tabela [{indice_tabela}] para remover. "
                    f"Abortando — revise manualmente este clone antes de continuar."
                )
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

            linhas_antes = await tabela.locator("tr").count()

            await cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "right", "clickCount": 1})
            await cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "right", "clickCount": 1})
            await page.wait_for_timeout(1000)

            item_linha = await _achar_item_menu_visivel(page, "Linha")
            if item_linha is None:
                raise RuntimeError(
                    f"Não achei o menu 'Linha' pra remover a linha {r} da tabela [{indice_tabela}]. "
                    f"Abortando — nenhuma linha foi removida nesta tentativa, mas revise manualmente "
                    f"este clone antes de continuar."
                )
            await item_linha.click()
            await page.wait_for_timeout(800)

            item_remover = await _achar_item_menu_visivel(page, "Remover Linhas", exato=False)
            if item_remover is None:
                raise RuntimeError(
                    f"Não achei 'Remover Linhas' pra linha {r} da tabela [{indice_tabela}]. "
                    f"Abortando — revise manualmente este clone antes de continuar."
                )
            await item_remover.click()
            await page.wait_for_timeout(1000)

            # Verificação de segurança: confere que exatamente 1 linha
            # foi removida (nunca 0 nem 2+) antes de seguir pra próxima.
            linhas_depois = await tabela.locator("tr").count()
            if linhas_antes - linhas_depois != 1:
                raise RuntimeError(
                    f"Remoção da linha {r} da tabela [{indice_tabela}] removeu "
                    f"{linhas_antes - linhas_depois} linha(s) em vez de 1. Abortando para não "
                    f"corromper mais dados — revise manualmente este clone antes de continuar."
                )

    log("Remoção de itens não pertencentes ao fornecedor concluída nas 3 tabelas UGG/UGP.")


# =========================================================
# MONTAGEM DOS DADOS + ORQUESTRAÇÃO FINAL (uma ata por fornecedor)
# =========================================================
# Junta 3 fontes, cada uma com um pedaço diferente do item, e NUNCA
# inventa o que faltar — se um item do fornecedor não tiver todos os
# dados, para e avisa (ver montar_itens_para_preenchimento):
#   - itens coletados (coletar_fornecedores_itens): marca, modelo,
#     quantidade ofertada, valor unitário ofertado.
#   - PDF do TR (extrair_itens_tr): grupo, especificação, unidade.
#   - Tabelas UGG/UGP (extrair_tabela_quantidades, índices 1 e 2):
#     Quantidade Mínima e Máxima (coluna A — a UASG do próprio
#     pregão), casando por número de item.
# Validado com dados reais: pregão 44/2026, fornecedor F DE OLIVEIRA,
# 28 itens — todas as 3 fontes bateram sem nenhum dado faltando.


def montar_itens_para_preenchimento(
    itens_coletados: list[dict],
    itens_tr: dict,
    quantidades_minimas: dict,
    quantidades_maximas: dict,
) -> list[dict]:
    """Combina as 3 fontes de dado em uma lista de dicts no formato
    exigido por preencher_linha_item() (grupo_tr, item_tr,
    especificacao, marca, modelo, unidade, quantidade_maxima,
    quantidade_minima, valor_unitario).

    `itens_coletados` é a lista "itens" de UM fornecedor, no formato
    retornado por coletar_fornecedores_itens() (cada item tem
    numero_item, marca, modelo, quantidade_ofertada,
    valor_unitario_ofertado).

    Levanta erro se QUALQUER item não tiver dado em alguma das 3
    fontes — nunca preenche com vazio/inventado. A mensagem lista
    exatamente quais itens e qual fonte faltou, pra revisão humana."""
    resultado = []
    problemas = []

    for item in itens_coletados:
        numero = item.get("numero_item", "")

        dado_tr = itens_tr.get(numero)
        dado_min = quantidades_minimas.get(numero)
        dado_max = quantidades_maximas.get(numero)

        if dado_tr is None:
            problemas.append(f"item {numero}: sem dado no PDF do TR (especificação/unidade)")
            continue
        if dado_min is None:
            problemas.append(f"item {numero}: sem dado na tabela de Quantidades Mínimas")
            continue
        if dado_max is None:
            problemas.append(f"item {numero}: sem dado na tabela de Quantidades Máximas")
            continue
        if not item.get("marca") or not item.get("modelo"):
            problemas.append(f"item {numero}: marca/modelo faltando nos dados coletados")
            continue
        if not item.get("valor_unitario_ofertado"):
            problemas.append(f"item {numero}: valor unitário ofertado faltando nos dados coletados")
            continue

        resultado.append({
            "grupo_tr": dado_tr["grupo"] or "-",
            "item_tr": numero,
            "especificacao": dado_tr["especificacao"],
            "marca": item["marca"],
            "modelo": item["modelo"],
            "unidade": dado_tr["unidade"],
            "quantidade_maxima": dado_max["col_a"],
            "quantidade_minima": dado_min["col_a"],
            "valor_unitario": item["valor_unitario_ofertado"],
        })

    if problemas:
        raise ValueError(
            "Não é seguro preencher a ata — faltam dados para " + str(len(problemas)) +
            " item(ns) (nunca inventar): \n  " + "\n  ".join(problemas)
        )

    return resultado


async def criar_ata_fornecedor(
    page, processo_id: str, cnpj: str, nome_fornecedor: str, itens_coletados: list[dict], itens_tr: dict,
) -> str:
    """Cria a ata completa de UM fornecedor: clona a Ata 279, preenche
    Fornecedor/CNPJ, preenche a tabela de itens com todos os dados
    combinados, e remove das tabelas UGG/UGP os itens que não
    pertencem a esse fornecedor. Deixa o clone como RASCUNHO pronto
    pra revisão humana — NUNCA clica em "Concluir".

    `processo_id` é a âncora obrigatória (Etapa 1): validado logo no
    início (processos_repo.validar_processo) — nunca só recebido e
    ignorado. O número do pregão usado no restante da função (contexto
    da mensagem de erro de estrutura, ver validar_mapa_colunas) vem do
    PRÓPRIO processo validado, nunca de um parâmetro solto.

    Deve ser chamada com `page` já na tela de listagem de Artefatos
    Digitais (.../comprasnet-artefatos-web/leitor-artefato).

    Retorna o identificador do clone criado (ex: "308/2026")."""
    processo = processos_repo.validar_processo(processo_id)
    numero_pregao = processo["pregaoCompleto"]

    # Checkpoint 3: qual modelo clonar passa a ser decidido por
    # obter_modelo_ata (hoje sempre 279 — ver docstring dela), não mais
    # lido direto das constantes do módulo dentro de clonar_ata_modelo.
    modelo = obter_modelo_ata(processo_id)
    identificador = await clonar_ata_modelo(page, modelo["numero"], modelo["ano"])

    # As tabelas de Quantidade Mínima/Máxima ficam na seção
    # "GERENCIADOR" (ÓRGÃO(S) GERENCIADOR E PARTICIPANTE(S)), não na
    # de "DOS PREÇOS" — precisa extrair de lá ANTES de montar os itens
    # que vão ser preenchidos na tabela de preços. Confirmado ao vivo
    # (Ata 283, item 6: Máxima=9/Mínima=2 batem exatamente com a coluna
    # A dessas tabelas) — fonte preservada sem mudança nesta etapa.
    frame_gerenciador = await abrir_secao_documento(page, 1, "GERENCIADOR")
    quantidades_minimas = await extrair_tabela_quantidades(frame_gerenciador, 1)
    quantidades_maximas = await extrair_tabela_quantidades(frame_gerenciador, 2)

    itens_para_preencher = montar_itens_para_preenchimento(
        itens_coletados, itens_tr, quantidades_minimas, quantidades_maximas
    )

    frame_editor = await abrir_secao_documento(page, 1, "DOS PREÇOS")

    # Lê a estrutura real pelo cabeçalho ANTES de preencher qualquer
    # coisa — nunca escreve por posição fixa (ver ETAPA 2, comparação
    # 279x283: estrutura bate hoje, mas a Ata 267 já mostrou um modelo
    # com 9 colunas; isso detecta esse caso e para, em vez de escrever
    # o valor errado na célula errada silenciosamente).
    mapa_colunas = await ler_mapa_colunas_precos(frame_editor)
    validar_mapa_colunas(mapa_colunas, identificador, numero_pregao)

    await preencher_fornecedor_cnpj(frame_editor, nome_fornecedor, cnpj)
    await preencher_tabela_itens(page, frame_editor, itens_para_preencher, mapa_colunas)

    # A remoção UGG é idempotente (relê o estado atual da tabela a cada
    # chamada e só remove o que ainda precisa) — diferente do
    # preenchimento acima, que sempre insere/digita, então nunca é
    # seguro repetir sem duplicar. Por isso só esta etapa tem retry:
    # confirmado em execução real que ela pode falhar por instabilidade
    # pontual (timing, ou o servidor ficar fora do ar no meio), e
    # tentar de novo NO MESMO clone resolve sem precisar recriar nada.
    numeros_item_fornecedor = {item["numero_item"] for item in itens_coletados}
    ultimo_erro = None
    for tentativa in range(3):
        try:
            frame_gerenciador = await abrir_secao_documento(page, 1, "GERENCIADOR")
            await remover_itens_nao_pertencentes_ugg(page, frame_gerenciador, numeros_item_fornecedor)
            ultimo_erro = None
            break
        except Exception as e:
            ultimo_erro = e
            log(f"  ⚠ Remoção UGG falhou (tentativa {tentativa + 1}/3) na ata {identificador}: {e}")
            await page.wait_for_timeout(3000)

    if ultimo_erro is not None:
        raise RuntimeError(
            f"Ata {identificador} ({nome_fornecedor}) ficou com Fornecedor/CNPJ e itens preenchidos, "
            f"mas a remoção das tabelas UGG/UGP falhou 3 vezes seguidas. NÃO crie outra ata para este "
            f"fornecedor — volte a este mesmo clone ({identificador}) e rode remover_itens_nao_pertencentes_ugg() "
            f"de novo depois de confirmar que o sistema está respondendo normalmente. Último erro: {ultimo_erro}"
        )

    log(f"Ata {identificador} criada para {nome_fornecedor} ({cnpj}) com {len(itens_para_preencher)} item(ns). "
        f"RASCUNHO pronto para revisão humana — 'Concluir' NÃO foi clicado.")
    return identificador


async def criar_atas_todos_fornecedores(
    page, processo_id: str, fornecedores: list[dict], itens_tr: dict, arquivo_relatorio: str | None = None
) -> list[dict]:
    """Chama criar_ata_fornecedor() para cada fornecedor de
    `fornecedores` (formato retornado por coletar_fornecedores_itens —
    ignora quem tiver "itens" vazio) e monta a relação final: nome do
    fornecedor + CNPJ + identificador da ata (número/ano) criada para
    ele, ou o motivo da falha se não deu certo.

    `processo_id` é validado uma vez aqui, ANTES de tocar no navegador
    (falha rápido se o processo não existir/não estiver confirmado), e
    de novo dentro de cada criar_ata_fornecedor() — validação em
    profundidade, nunca confiando que já foi checado antes.

    Se um fornecedor falhar (mesmo depois dos retries internos de
    criar_ata_fornecedor), o loop PARA nele — não segue criando atas
    novas pros fornecedores seguintes. Pedido explícito do usuário:
    se der erro, não ficar criando mais artefatos, voltar e corrigir o
    que já existe antes de continuar. O relatório (e o CSV, se
    `arquivo_relatorio` foi passado) mostra o que já deu certo até ali
    e o erro exato do fornecedor que travou o processo.

    Se `arquivo_relatorio` for informado, salva a relação em CSV (nome,
    cnpj, identificador_ata, status) depois de CADA fornecedor
    processado — mesma lógica de progresso incremental usada em
    coletar_fornecedores_itens(), pra não perder tudo se a sessão cair
    no meio de uma varredura longa (cada ata leva minutos pra ser
    criada).

    Deve ser chamada com `page` já na tela de listagem de Artefatos
    Digitais."""
    processos_repo.validar_processo(processo_id)

    relatorio = []

    fornecedores_com_itens = [f for f in fornecedores if f.get("itens")]
    log(f"Criando atas para {len(fornecedores_com_itens)} fornecedor(es) com itens homologados...")

    for i, fornecedor in enumerate(fornecedores_com_itens, start=1):
        cnpj = fornecedor["cnpj"]
        nome = fornecedor["fornecedor"]
        log(f"[{i}/{len(fornecedores_com_itens)}] {nome} ({cnpj}) — {len(fornecedor['itens'])} item(ns)...")

        try:
            identificador = await criar_ata_fornecedor(page, processo_id, cnpj, nome, fornecedor["itens"], itens_tr)
            relatorio.append({"fornecedor": nome, "cnpj": cnpj, "ata": identificador, "status": "OK"})
        except Exception as e:
            log(f"  ⚠ Falha ao criar ata para {nome} ({cnpj}) — PARANDO o processo aqui (não vou criar mais "
                f"atas até isso ser revisado). Erro: {e}")
            relatorio.append({"fornecedor": nome, "cnpj": cnpj, "ata": "", "status": f"ERRO: {e}"})
            if arquivo_relatorio:
                with open(arquivo_relatorio, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=["fornecedor", "cnpj", "ata", "status"])
                    writer.writeheader()
                    writer.writerows(relatorio)
            raise

        if arquivo_relatorio:
            with open(arquivo_relatorio, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["fornecedor", "cnpj", "ata", "status"])
                writer.writeheader()
                writer.writerows(relatorio)

        # de volta pra listagem, pronto pro próximo fornecedor
        try:
            voltar = page.get_by_text("Voltar", exact=True).first
            if await voltar.count() > 0:
                await voltar.click()
                await page.wait_for_timeout(2000)
        except Exception:
            pass

    ok = sum(1 for r in relatorio if r["status"] == "OK")
    log(f"Concluído: {ok}/{len(relatorio)} ata(s) criada(s) com sucesso.")
    if arquivo_relatorio:
        log(f"Relação salva em: {arquivo_relatorio}")

    return relatorio


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
