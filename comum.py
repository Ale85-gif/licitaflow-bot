import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# =========================================================
# Funções e constantes compartilhadas entre os bots
# (Bot comprasnet .py e bot_participante.py)
# =========================================================

PLANILHA_ID = "1wy8i8nuUkBFezSeySfnnPw06TVLxhwaXrwEGIoxOewU"
CHAVE_JSON = "chaves.json"

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PERFIL = r"C:\chrome-real"

DB_PATH = "dados.db"


def log(msg: str) -> None:
    linha = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(linha, flush=True)
    except UnicodeEncodeError:
        # Console sem suporte a UTF-8 (cp1252 no PowerShell/cmd padrão do
        # Windows) — evita derrubar o bot por causa de um caractere (ex:
        # emoji ⚠) que não tem representação nessa codepage.
        encoding = sys.stdout.encoding or "ascii"
        print(linha.encode(encoding, errors="replace").decode(encoding), flush=True)


def normalizar_espacos(txt) -> str:
    return re.sub(r"\s+", " ", str(txt or "")).strip()


def parse_data_br(valor):
    s = normalizar_espacos(valor)

    if not s or s == "-":
        return None

    m = re.search(r"(\d{2}/\d{2}/\d{4})", s)

    if m:
        s = m.group(1)

    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except Exception:
        return None


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


def update_com_retry(ws, range_name, values, value_input_option="USER_ENTERED", tentativas=6, pausa=2.0):
    for tentativa in range(1, tentativas + 1):
        try:
            ws.update(
                range_name=range_name,
                values=values,
                value_input_option=value_input_option
            )
            time.sleep(pausa)
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


def clear_com_retry(ws, tentativas=6, pausa=2.0):
    for tentativa in range(1, tentativas + 1):
        try:
            ws.clear()
            time.sleep(pausa)
            return

        except Exception as e:
            erro = str(e)

            if "429" in erro or "Quota exceeded" in erro:
                espera = tentativa * 15
                log(f"Quota ao limpar aba. Aguardando {espera}s...")
                time.sleep(espera)
            else:
                raise


# =========================================================
# SQLITE LOCAL (canal rápido para sistemas próprios,
# sem depender da API/cota do Google Sheets)
# =========================================================

def salvar_sqlite(nome_tabela: str, headers: list, linhas: list) -> None:
    """Recria a tabela `nome_tabela` em DB_PATH com as colunas de `headers`
    e insere `linhas` (dump completo, mesmo dado que vai para a aba
    correspondente no Google Sheets)."""
    conn = sqlite3.connect(DB_PATH)

    try:
        cur = conn.cursor()
        colunas = ", ".join(f'"{h}" TEXT' for h in headers)

        cur.execute(f'DROP TABLE IF EXISTS "{nome_tabela}"')
        cur.execute(f'CREATE TABLE "{nome_tabela}" ({colunas})')

        placeholders = ", ".join("?" for _ in headers)
        cur.executemany(
            f'INSERT INTO "{nome_tabela}" VALUES ({placeholders})',
            [[("" if v is None else str(v)) for v in linha] for linha in linhas]
        )

        conn.commit()
        log(f"SQLite: tabela '{nome_tabela}' atualizada com {len(linhas)} linha(s) em {DB_PATH}")

    finally:
        conn.close()


# =========================================================
# CHROME (automação com depuração remota)
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


def fechar_chrome_automacao() -> None:
    """Encerra somente o Chrome aberto com o perfil de automação (PERFIL),
    sem afetar outras janelas/perfis de Chrome que o usuário tenha abertos."""
    log("Encerrando Chrome da automação (perfil de depuração)...")

    subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{PERFIL}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        ],
        capture_output=True,
    )

    time.sleep(2)
