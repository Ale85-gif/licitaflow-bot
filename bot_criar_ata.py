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
# TODO: ainda não implementado como função testada via script.
# Mapeado manualmente (ver conversa): tela
# .../comprasnet-web/seguro/governo/selecao-fornecedores
# ?identificador=<UASG><modalidade 2 dig><numero 5 dig><ano>&etapa=AH
# aba "Fornecedores" lista CNPJ + razão social + "Itens habilitados:
# X de Y". Expandir cada fornecedor mostra 2 tabelas: "Itens em que
# o fornecedor é o melhor classificado" (USAR) e "...não é o melhor
# classificado" (IGNORAR, mesmo que apareça "Homologado"). Cada
# item tem um "+" que expande e mostra Quantidade ofertada,
# Marca/Fabricante, Modelo/Versão, Valor ofertado (unitário/total).


async def coletar_fornecedores_itens(page, identificador: str) -> list[dict]:
    """AINDA NÃO IMPLEMENTADO / NÃO TESTADO.
    Deve navegar até a tela de seleção de fornecedores do pregão
    (usando `identificador`) e retornar, por fornecedor, a lista de
    itens homologados com quantidade/marca/modelo/valor unitário.
    """
    raise NotImplementedError(
        "Ainda não implementado — precisa testar ao vivo os seletores da tela "
        "de seleção de fornecedores (expandir fornecedor, expandir item)."
    )


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
