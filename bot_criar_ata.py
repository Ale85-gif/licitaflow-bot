import asyncio
import base64
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
# ITEM AVULSO (não-grupo): dentro do bloco "melhor classificado" da
# aba Fornecedores, cada item tem um botão-chevron
# (`button:has(i.fa-chevron-down)`) que expande INLINE (sem navegar)
# e revela Descrição detalhada, Quantidade ofertada, Marca/Fabricante,
# Modelo/Versão — ver expandir_item_individual().
#
# ITEM VENCIDO POR GRUPO (linha "GRUPO N | X itens"): não dá pra
# expandir inline, é preciso navegar por uma cadeia de 3 cliques:
#   1. Na linha do grupo (já expandida, dentro do fornecedor), clicar
#      no botão com ícone `fa-plus-square` -> navega pra
#      .../item/<id>?identificador=... (visão geral do grupo, lista
#      TODOS os fornecedores que disputaram aquele grupo, não só o
#      nosso alvo).
#   2. Nessa página, achar a linha do CNPJ do fornecedor alvo e
#      clicar no botão `button[aria-expanded]` (aria-label="Mostrar
#      proposta do grupo") -> expande e revela um bloco "PROPOSTA"
#      terminando num botão/link com texto "Itens do grupo >>"
#      (cuidado: não confundir com o texto explicativo mais longo
#      "Visualize as propostas dos itens do grupo..." que também
#      contém a substring "itens do grupo" — procurar especificamente
#      um elemento <button> ou <a> com esse texto, não texto solto).
#   3. Clicar nesse "Itens do grupo >>" -> navega pra
#      .../item/<id>/itens-grupo/participante/<cnpj-sem-formatacao>,
#      que lista cada item do grupo (mesma estrutura de card com
#      chevron de expansão do item avulso) -> usar
#      expandir_item_individual() em cada um.
#
# Depois de coletar, tem que voltar (botão "Voltar" ou re-navegar)
# antes de continuar pro próximo fornecedor/grupo.


_RE_ITEM_MELHOR_CLASSIFICADO = re.compile(
    r"(\d+)\s+([^\n]+)\n"
    r"(Homologado|Julgado[^\n]*)\s*"
    r"Valor estimado\s*:\s*R\$\s*([\d.,]+)\s*R\$\s*([\d.,]+)"
)


_RE_LINHA_GRUPO = re.compile(r"^\|\s*(\d+)\s*itens?$", re.IGNORECASE)


def _parsear_itens_melhor_classificado(bloco_texto: str) -> list[dict]:
    """Extrai os itens do bloco de texto entre 'Itens em que o
    fornecedor é o melhor classificado' e o próximo marcador (fim do
    fornecedor ou bloco 'não é o melhor classificado').

    Quando o fornecedor venceu um GRUPO inteiro (disputa "menor preço
    por grupo"), a tela mostra uma linha-resumo ("<grupo> | N itens")
    em vez de item por item — essas linhas viram entradas com
    "eh_grupo": True e SEM número de item de verdade (ainda não
    implementamos a expansão de grupo em itens individuais — ver TODO).
    Nunca inventamos números de item pra esses casos.
    """
    itens = []
    for m in _RE_ITEM_MELHOR_CLASSIFICADO.finditer(bloco_texto):
        descricao = _limpar(m.group(2))
        m_grupo = _RE_LINHA_GRUPO.match(descricao)

        if m_grupo:
            itens.append({
                "eh_grupo": True,
                "grupo": m.group(1),
                "qtd_itens_grupo": m_grupo.group(1),
                "status": _limpar(m.group(3)),
                "valor_estimado": m.group(4),
                "valor_ofertado": m.group(5),
            })
            continue

        itens.append({
            "eh_grupo": False,
            "numero_item": m.group(1),
            "descricao_curta": descricao,
            "status": _limpar(m.group(3)),
            "valor_estimado": m.group(4),
            "valor_ofertado": m.group(5),
        })

    return itens


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
    await page.wait_for_timeout(1200)

    return (await botao.get_attribute("aria-expanded")) == "true"


_RE_ITEM_EXPANDIDO = re.compile(
    r"Descrição detalhada\n(?P<descricao_detalhada>[^\n]+)\n"
    r".*?"
    r"Quantidade ofertada\n(?P<quantidade_ofertada>[\d.,]+)\n"
    r"Marca/Fabricante\n(?P<marca>[^\n]*)\n"
    r"Modelo/Vers[ãa]o\n(?P<modelo>[^\n]*)",
    re.DOTALL,
)


def _parsear_item_expandido(texto: str) -> dict:
    """Extrai os dados que só aparecem depois de expandir um item
    (individual ou dentro de um grupo): descrição detalhada,
    quantidade ofertada, marca/fabricante, modelo/versão."""
    m = _RE_ITEM_EXPANDIDO.search(texto)
    if not m:
        return {}
    return {
        "descricao_detalhada": _limpar(m.group("descricao_detalhada")),
        "quantidade_ofertada": _limpar(m.group("quantidade_ofertada")),
        "marca": _limpar(m.group("marca")),
        "modelo": _limpar(m.group("modelo")),
    }


async def expandir_item_individual(item_locator, page) -> dict:
    """Clica no chevron de expansão de UM item (funciona tanto pra
    item avulso na aba Fornecedores quanto pra item dentro da tela
    'Itens do grupo') e retorna o detalhe extraído (ver
    _parsear_item_expandido). Não faz nada se já estiver expandido."""
    chevron = item_locator.locator("button:has(i.fa-chevron-down)").first

    if await chevron.count() > 0:
        await chevron.scroll_into_view_if_needed()
        await chevron.click()
        await page.wait_for_timeout(1200)

    texto = await item_locator.inner_text()
    return _parsear_item_expandido(texto)


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
    `cnpj_fornecedor` na visão geral do grupo falhou — não se sabe se é
    porque a tela em FR mostra menos informação, ou só timing. Se for
    chamar essa função com o pregão numa fase diferente de AH, vale
    testar de novo antes de confiar no resultado.
    """
    botao_mais = linha_grupo_locator.locator("button:has(i.fa-plus-square)").first
    if await botao_mais.count() == 0:
        log("  ⚠ Não achei o botão '+' na linha do grupo.")
        return []

    await botao_mais.scroll_into_view_if_needed()
    await botao_mais.click()
    await page.wait_for_timeout(2000)

    # Visão geral do grupo: lista TODOS os fornecedores que disputaram
    # esse grupo, não só o nosso alvo — precisa achar a linha certa.
    linha_fornecedor = page.get_by_text(cnpj_fornecedor, exact=False).first
    if await linha_fornecedor.count() == 0:
        log(f"  ⚠ Não achei o fornecedor {cnpj_fornecedor} na visão geral do grupo.")
        return []

    linha_fornecedor_container = linha_fornecedor.locator(
        "xpath=ancestor::div[contains(@class,'cp-item-expansivel')]"
    ).first

    if not await _expandir_fornecedor(page, linha_fornecedor_container):
        log(f"  ⚠ Não consegui expandir a proposta do fornecedor {cnpj_fornecedor} no grupo.")
        return []

    link_itens_grupo = page.locator("button, a").filter(has_text="Itens do grupo").first
    if await link_itens_grupo.count() == 0:
        log("  ⚠ Não achei o link 'Itens do grupo >>'.")
        return []

    await link_itens_grupo.scroll_into_view_if_needed()
    await link_itens_grupo.click()
    await page.wait_for_timeout(2500)

    cards_item = page.locator("div.cp-item-expansivel")
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


async def coletar_fornecedores_itens(page) -> list[dict]:
    """Deve ser chamada com `page` já na tela de seleção de
    fornecedores (aba 'Fornecedores' visível — clique nela se
    necessário antes de chamar esta função).

    Expande cada fornecedor com itens habilitados > 0 e retorna uma
    lista de {"cnpj", "fornecedor", "itens": [...]}, onde cada item
    tem numero_item/descricao_curta/status/valor_estimado/valor_ofertado
    (ainda SEM quantidade/marca/modelo — ver TODO no topo do arquivo).
    """
    aba_fornecedores = page.get_by_text("Fornecedores", exact=True).first
    if await aba_fornecedores.count() > 0:
        await aba_fornecedores.click()
        await page.wait_for_timeout(2000)

    linhas = page.locator("div.cp-item-expansivel")
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

        expandiu = await _expandir_fornecedor(page, linha)
        if not expandiu:
            log(f"  ⚠ Não consegui expandir fornecedor {cnpj} — pulando.")
            continue

        texto_expandido = await linha.inner_text()

        # Corta só o trecho "melhor classificado" (ignora o bloco "não é o melhor classificado")
        idx_inicio = texto_expandido.find("melhor classificado")
        idx_fim = texto_expandido.find("não é o melhor classificado")
        bloco = texto_expandido[idx_inicio:idx_fim] if idx_fim > 0 else texto_expandido[idx_inicio:]

        itens = _parsear_itens_melhor_classificado(bloco)

        # Nome do fornecedor: primeira linha de texto antes de "Itens habilitados"
        nome_match = re.search(r"\n([A-ZÀ-Ÿ0-9][^\n]*)\nItens habilitados", texto_linha_fechada)
        nome = _limpar(nome_match.group(1)) if nome_match else ""

        log(f"  {cnpj} {nome}: {len(itens)} item(ns) melhor classificado (esperado: {m_habilitados.group(1)})")

        resultado.append({"cnpj": cnpj, "fornecedor": nome, "itens": itens})

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
