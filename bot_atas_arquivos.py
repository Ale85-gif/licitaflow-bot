import asyncio
import os
import time
import subprocess
import traceback
from datetime import datetime
from urllib.parse import urljoin

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from playwright.async_api import async_playwright

# =========================================================
# BOT ATAS - ARQUIVO POR ARQUIVO - V35 PROFISSIONAL
# =========================================================
# - Extração profunda: Capa, Pregão, Unidades e Itens
# - Filtro EXCLUSIVO: Apenas Atas onde a PMB (160082) é PARTICIPANTE
# - Destino: Aba "PARTICIPAÇÃO"
# - Proteção anti-quota 429 (Retry Automático)
# - Formatação padronizada de cabeçalho
# =========================================================

# --- CONFIGURAÇÕES GERAIS ---
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PERFIL = r"C:\chrome-real"
PLANILHA_ID = "1wy8i8nuUkBFezSeySfnnPw06TVLxhwaXrwEGIoxOewU"
CHAVE_JSON = "chaves.json"

# Filtro alterado exclusivamente para Participante (uasg=["P"])
URL_ATAS_GERAL = "https://contratos.sistema.gov.br/arp?situacao=%5B%22Ata+de+Registro+de+Pre%C3%A7os%22%5D&uasg=%5B%22P%22%5D"

PAUSA_SHEETS = 2.0

BOT_HEADERS = [
    "Ata", "Situação", "Tipo UASG", "Vigência Inicial", "Vigência Final", 
    "Compra (Pregão)", "Modalidade", "Processo", "Valor Total Ata", "Papel PMB (160082)",
    "CNPJ", "Fornecedor", "Item", "Descrição", 
    "Qtd Registrada", "Valor Unitário", "Valor Total Item", "Data Coleta"
]

# --- CORES DO SISTEMA ---
COR_CAB = {"red": 0.19, "green": 0.28, "blue": 0.50}
COR_BRANCO = {"red": 1, "green": 1, "blue": 1}

# =========================================================
# LOG & UTIL
# =========================================================
def log(msg: str) -> None: 
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# =========================================================
# GOOGLE SHEETS COM RETRY
# =========================================================
def conectar_google():
    log("Conectando ao Google Sheets...")
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CHAVE_JSON, scope)
    gc = gspread.authorize(creds)
    planilha = gc.open_by_key(PLANILHA_ID)
    log(f"Planilha conectada: {planilha.title}")
    return planilha

def get_or_create_worksheet(planilha, nome: str, rows=2000, cols=20):
    try:
        return planilha.worksheet(nome)
    except Exception:
        return planilha.add_worksheet(title=nome, rows=rows, cols=cols)

def update_com_retry(ws, range_name, values, value_input_option="USER_ENTERED", tentativas=6):
    for tentativa in range(1, tentativas + 1):
        try:
            ws.update(
                range_name=range_name,
                values=values,
                value_input_option=value_input_option
            )
            time.sleep(PAUSA_SHEETS)
            return
        except Exception as e:
            erro = str(e)
            if "429" in erro or "Quota exceeded" in erro:
                espera = tentativa * 15
                log(f"Quota Sheets atingida. Aguardando {espera}s e tentando novamente...")
                time.sleep(espera)
            else:
                raise
    raise Exception("Falhou após várias tentativas por limite de quota do Google Sheets.")

def clear_com_retry(ws, tentativas=6):
    for tentativa in range(1, tentativas + 1):
        try:
            ws.clear()
            time.sleep(PAUSA_SHEETS)
            return
        except Exception as e:
            erro = str(e)
            if "429" in erro or "Quota exceeded" in erro:
                espera = tentativa * 15
                log(f"Quota ao limpar aba. Aguardando {espera}s...")
                time.sleep(espera)
            else:
                raise

def formatar_aba_atas(ws):
    try:
        ws.format("A1:R1", {
            "backgroundColor": COR_CAB, 
            "textFormat": {"bold": True, "foregroundColor": COR_BRANCO}, 
            "horizontalAlignment": "CENTER",
            "wrapStrategy": "WRAP"
        })
    except Exception as e: 
        log(f"Falha na formatação da aba: {e}")

# =========================================================
# CHROME
# =========================================================
def abrir_chrome() -> None:
    if not os.path.exists(CHROME):
        raise FileNotFoundError(f"Chrome não encontrado em: {CHROME}")
    log("Iniciando navegador...")
    subprocess.Popen([CHROME, "--remote-debugging-port=9222", f"--user-data-dir={PERFIL}"])
    time.sleep(5)

# =========================================================
# ORQUESTRAÇÃO PRINCIPAL
# =========================================================
async def rodar_bot_atas():
    try:
        abrir_chrome()
        planilha = conectar_google()
        
        # Configuração da Aba (Nome alterado para PARTICIPAÇÃO)
        ws = get_or_create_worksheet(planilha, "PARTICIPAÇÃO", rows=2000, cols=18)
        clear_com_retry(ws)
        update_com_retry(ws, "A1:R1", [BOT_HEADERS])
        formatar_aba_atas(ws)
        
        linhas_banco_dados = []

        async with async_playwright() as p:
            log("Conectando ao Chrome via CDP...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            page = browser.contexts[0].pages[0] if browser.contexts[0].pages else await browser.contexts[0].new_page()
            
            log(f"Acessando portal de Atas...")
            await page.goto(URL_ATAS_GERAL, wait_until="domcontentloaded")
            
            try:
                await page.wait_for_selector("a[href*='/show']", timeout=15000)
            except:
                log("Aviso: Demora no carregamento da tabela inicial.")

            # 1. Configura a Paginação
            try:
                await page.select_option("select[name*='length']", value="-1")
                await page.wait_for_timeout(4000)
                await page.wait_for_selector("a[href*='/show']") 
            except Exception: 
                log("Aviso: Lendo paginação padrão do sistema...")

            # 2. Mapear Links (Lógica de Paginação mantida)
            log("Mapeando arquivos de Atas...")
            links_arquivos = []
            pagina_atual = 1
            
            while True:
                log(f"Coletando links da página {pagina_atual}...")
                linhas = await page.query_selector_all("table tbody tr")
                
                for linha in linhas:
                    btn_visualizar = await linha.query_selector("a[href*='/show']")
                    if btn_visualizar: 
                        link_relativo = await btn_visualizar.get_attribute("href")
                        if link_relativo:
                            links_arquivos.append(urljoin("https://contratos.sistema.gov.br", link_relativo))
                
                btn_proximo = await page.query_selector(".next:not(.disabled) a, a.next:not(.disabled)")
                if btn_proximo:
                    await btn_proximo.click()
                    await page.wait_for_timeout(3000)
                    pagina_atual += 1
                else:
                    break
                    
            log(f"Total de {len(links_arquivos)} atas mapeadas. Iniciando extração profunda...")

            # 3. Fluxo Arquivo por Arquivo
            for idx, url_full in enumerate(links_arquivos, start=1):
                log(f"Processando arquivo {idx}/{len(links_arquivos)}...")
                
                await page.goto(url_full, wait_until="domcontentloaded")
                await page.wait_for_timeout(3500)
                
                # Script nativo focado em validar se PMB é apenas PARTICIPANTE
                dados_ata = await page.evaluate('''() => {
                    const capa = {};
                    document.querySelectorAll("div").forEach(div => {
                        const txt = div.innerText.trim();
                        if (["Número:", "Situação:", "Tipo UASG:", "Vigência inicial:", "Vigência final:", "Compra:", "Modalidade da compra:", "Número do processo:", "Valor total:", "Unidade gerenciadora da ata:"].includes(txt)) {
                            if (div.nextElementSibling) capa[txt] = div.nextElementSibling.innerText.trim();
                        }
                    });

                    // Filtro 1: Se a PMB for a Gerenciadora, descarta o arquivo
                    if (capa["Unidade gerenciadora da ata:"] && capa["Unidade gerenciadora da ata:"].includes("160082")) {
                        return { pular: true, motivo: "PMB é a Gerenciadora. Ignorando." };
                    }

                    let papel_pmb = "Não localizada";
                    let encontrou_pmb = false;
                    const itens = [];
                    
                    document.querySelectorAll("table").forEach(table => {
                        const txt = table.innerText;
                        
                        // Filtro 2: Busca a PMB na tabela de Participantes
                        if (txt.includes("Código UASG") && txt.includes("Unidade participante")) {
                            table.querySelectorAll("tbody tr").forEach(tr => {
                                if (tr.innerText.includes("160082")) {
                                    const tds = tr.querySelectorAll("td");
                                    if (tds.length >= 3) {
                                        papel_pmb = tds[2].innerText.trim();
                                        encontrou_pmb = true;
                                    }
                                }
                            });
                        }
                        
                        // Extrai Itens
                        if (txt.includes("Fornecedor") && txt.includes("Valor unitário")) {
                            table.querySelectorAll("tbody tr").forEach(tr => {
                                const tds = tr.querySelectorAll("td");
                                if (tds.length >= 7) {
                                    itens.push({
                                        cnpj: tds[0].innerText.trim(),
                                        fornecedor: tds[1].innerText.trim(),
                                        numero: tds[2].innerText.trim(),
                                        descricao: tds[3].innerText.trim(),
                                        qtd: tds[4].innerText.trim(),
                                        valor_unit: tds[5].innerText.trim(),
                                        valor_total: tds[6].innerText.trim()
                                    });
                                }
                            });
                        }
                    });

                    if (!encontrou_pmb) {
                        return { pular: true, motivo: "PMB não localizada na tabela de Participantes. Ignorando." };
                    }

                    return { pular: false, capa, papel_pmb, itens };
                }''')

                # Verifica se o arquivo foi rejeitado pelos filtros JS
                if dados_ata.get("pular"):
                    log(f"-> Arquivo ignorado: {dados_ata.get('motivo')}")
                    continue

                capa = dados_ata.get("capa", {})
                papel_pmb = dados_ata.get("papel_pmb", "N/A")
                itens = dados_ata.get("itens", [])
                
                numero_ata = capa.get("Número:", "N/A")
                
                for item in itens:
                    linhas_banco_dados.append([
                        numero_ata,
                        capa.get("Situação:", "N/A"),
                        capa.get("Tipo UASG:", "N/A"),
                        capa.get("Vigência inicial:", "N/A"),
                        capa.get("Vigência final:", "N/A"),
                        capa.get("Compra:", "N/A"),
                        capa.get("Modalidade da compra:", "N/A"),
                        capa.get("Número do processo:", "N/A"),
                        capa.get("Valor total:", "N/A"),
                        papel_pmb, 
                        item.get("cnpj", ""),
                        item.get("fornecedor", "").replace("\n", " "),
                        item.get("numero", ""),
                        item.get("descricao", "")[:150],
                        item.get("qtd", ""),
                        item.get("valor_unit", ""),
                        item.get("valor_total", ""),
                        datetime.now().strftime("%d/%m/%Y")
                    ])
                
                log(f"-> Arquivo Ata {numero_ata} validado. {len(itens)} itens extraídos. Papel: {papel_pmb}")

            # 4. Descarregamento Seguro com Retry no Final
            if linhas_banco_dados:
                log(f"Salvando {len(linhas_banco_dados)} linhas na aba PARTICIPAÇÃO...")
                ultima_coluna = chr(64 + len(BOT_HEADERS)) # Transforma 18 em 'R'
                update_com_retry(ws, f"A2:{ultima_coluna}{len(linhas_banco_dados)+1}", linhas_banco_dados)
            else:
                log("Nenhum dado válido encontrado para salvar.")

            log("Processo 100% concluído. Tabela atualizada com sucesso.")

    except Exception as e:
        log(f"ERRO FATAL: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(rodar_bot_atas())