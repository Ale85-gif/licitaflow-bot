import asyncio
import sys
import traceback

from playwright.async_api import async_playwright

from comum import (
    abrir_chrome,
    log,
)

# =========================================================
# BOT CRIAR ATA - Registra uma nova Ata de Registro de Preços
# no Contratos.gov.br (contratos.sistema.gov.br)
# =========================================================
# STATUS: ESQUELETO / EM CONSTRUÇÃO.
#
# Diferente dos outros bots (que só LEEM dados do portal), este
# bot vai ESCREVER: criar um registro oficial (Ata de Registro de
# Preços) no sistema do governo. Isso tem peso administrativo/legal
# real, então antes de automatizar o envio de verdade é preciso:
#
#   1. Confirmar a URL/tela real de criação (ainda não investigada
#      a fundo - só sabemos que a listagem fica em /arp e o padrão
#      de rotas do sistema, baseado em Backpack/Laravel, sugere que
#      a tela de criação fica em /arp/create, mas isso PRECISA ser
#      confirmado abrindo a tela de verdade antes de codar o
#      preenchimento).
#   2. Mapear TODOS os campos obrigatórios do formulário (fornecedor,
#      itens, quantidades, valores, vigência, unidade gerenciadora,
#      etc.) e o formato esperado de cada um.
#   3. Definir de onde vêm os dados de cada ata nova (planilha? outro
#      sistema? digitação manual mediada pelo bot?). Isso ainda não
#      foi definido.
#   4. Decidir o comportamento em caso de erro no meio do preenchimento
#      (não dá pra simplesmente tentar de novo como nos bots de
#      leitura - pode duplicar um registro oficial).
#
# Recomendo rodar esse bot primeiro em modo de investigação/dry-run
# (abrir a tela, mapear os campos, mas sem clicar em salvar) antes de
# habilitar qualquer envio de verdade.
# =========================================================

URL_ARP = "https://contratos.sistema.gov.br/arp"
URL_ARP_CRIAR = "https://contratos.sistema.gov.br/arp/create"  # TODO: confirmar se é essa mesmo


async def investigar_tela_criacao(page) -> None:
    """Abre a tela de criação (sem preencher/enviar nada) só pra mapear
    os campos do formulário. Rode isso primeiro, antes de implementar
    o preenchimento de verdade."""
    log(f"Abrindo tela de criação para investigação: {URL_ARP_CRIAR}")
    await page.goto(URL_ARP_CRIAR, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    log(f"URL final após navegação: {page.url}")
    log(f"Título da página: {await page.title()}")

    campos = await page.query_selector_all("input, select, textarea")
    log(f"Total de campos de formulário encontrados: {len(campos)}")

    for campo in campos:
        try:
            tag = await campo.evaluate("el => el.tagName")
            nome = await campo.get_attribute("name")
            tipo = await campo.get_attribute("type")
            obrigatorio = await campo.get_attribute("required")
            label = await campo.get_attribute("placeholder")
            log(f"  <{tag}> name={nome!r} type={tipo!r} required={obrigatorio is not None} placeholder={label!r}")
        except Exception as e:
            log(f"  (falha ao inspecionar campo: {e})")


async def criar_ata(page, dados_ata: dict) -> None:
    """AINDA NÃO IMPLEMENTADO. Vai preencher e enviar o formulário de
    criação de ata com base em `dados_ata`, depois que os campos reais
    forem mapeados via investigar_tela_criacao()."""
    raise NotImplementedError(
        "Preenchimento do formulário ainda não implementado - "
        "rode investigar_tela_criacao() primeiro para mapear os campos reais."
    )


async def main():
    try:
        abrir_chrome()

        async with async_playwright() as p:
            log("Conectando ao Chrome via CDP...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")

            if not browser.contexts:
                raise RuntimeError("Nenhum contexto encontrado no Chrome.")

            context = browser.contexts[0]
            page = await context.new_page()

            try:
                await investigar_tela_criacao(page)
            finally:
                await page.close()

        log("Investigação concluída. Nenhum dado foi enviado.")

    except Exception as e:
        log(f"ERRO FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
