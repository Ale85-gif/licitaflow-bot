"""
abrir_licitaflow.py - sobe a ponte (se não estiver rodando) e abre o Chrome
já com a extensão LicitaFlow carregada, no mesmo perfil persistente que o
bot_comprasnet usa (C:\\chrome-real).

Um único comando para deixar tudo pronto para o login:
    python abrir_licitaflow.py
"""

import os
import subprocess
import time

import requests

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PERFIL = r"C:\chrome-real"
# Fixo (nao sys.executable): o .venv fica fora do OneDrive (C:\venvs\...) para
# nao ser corrompido pela sincronizacao em tempo real durante pip install.
PYTHON_VENV = r"C:\venvs\meu_projeto_python\Scripts\python.exe"
RAIZ_PROJETO = os.path.dirname(os.path.abspath(__file__))
PASTA_EXTENSAO = os.path.join(RAIZ_PROJETO, "licitaflow", "extension")
URL_INICIAL = "https://www.comprasnet.gov.br/seguro/loginPortal.asp"

PONTE_URL = "http://127.0.0.1:8765"
PONTE_LOG_SAIDA = os.path.join(RAIZ_PROJETO, "ponte_stdout.log")
PONTE_LOG_ERRO = os.path.join(RAIZ_PROJETO, "ponte_stderr.log")


def ponte_no_ar() -> bool:
    try:
        return requests.get(f"{PONTE_URL}/health", timeout=2).ok
    except requests.RequestException:
        return False


def subir_ponte() -> None:
    print("Ponte não está no ar. Subindo uvicorn ponte:app --port 8765...")
    with open(PONTE_LOG_SAIDA, "w", encoding="utf-8") as saida, \
         open(PONTE_LOG_ERRO, "w", encoding="utf-8") as erro:
        subprocess.Popen(
            [PYTHON_VENV, "-m", "uvicorn", "ponte:app", "--port", "8765"],
            cwd=RAIZ_PROJETO,
            stdout=saida,
            stderr=erro,
        )

    for _ in range(20):
        time.sleep(0.5)
        if ponte_no_ar():
            print("Ponte no ar.")
            return

    print("AVISO: a ponte não respondeu em 10s. Confira ponte_stderr.log.")


def abrir_chrome() -> None:
    if not os.path.exists(CHROME):
        raise FileNotFoundError(f"Chrome não encontrado em: {CHROME}")
    if not os.path.isdir(PASTA_EXTENSAO):
        raise FileNotFoundError(f"Pasta da extensão não encontrada: {PASTA_EXTENSAO}")

    print("Abrindo Chrome com a extensão LicitaFlow já carregada...")
    subprocess.Popen([
        CHROME,
        f"--user-data-dir={PERFIL}",
        f"--load-extension={PASTA_EXTENSAO}",
        "--remote-debugging-port=9222",
        URL_INICIAL,
    ])


def main() -> None:
    if not ponte_no_ar():
        subir_ponte()
    else:
        print("Ponte já estava no ar.")

    abrir_chrome()

    print()
    print("Pronto. Falta só você:")
    print("  1. Fazer login normalmente no Compras.gov.br (com 2º fator).")
    print("  2. Conferir o popup da extensão: a Ponte já liga sozinha nesse")
    print("     perfil; se aparecer 'Ligar', é porque foi desligada antes.")


if __name__ == "__main__":
    main()
