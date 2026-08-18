import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from comum import fechar_chrome_automacao

# =========================================================
# ORQUESTRADOR: roda o bot gerenciador (pregões da PMB) e,
# em seguida, o bot de participação (atas onde a PMB participa
# ou gerencia, listadas em contratos.sistema.gov.br/arp).
#
# Entre os dois bots, encerra o Chrome de automação e deixa
# cada bot abrir sua própria instância — evita que o segundo
# bot tente reaproveitar um Chrome que ficou muitas horas aberto
# e não responde mais ao CDP.
#
# Cada execução grava seu próprio log em logs/ (inclusive quando
# rodado pelo Agendador de Tarefas, que por padrão não guarda saída
# nenhuma) e o processo termina com código de erro != 0 se qualquer
# um dos dois bots falhar — sem isso, o Agendador sempre via "sucesso"
# mesmo quando algo dava errado.
# =========================================================

PASTA = Path(__file__).resolve().parent
PYTHON = sys.executable

BOT_GERENCIADOR = PASTA / "Bot comprasnet rapido.py"
BOT_PARTICIPACAO = PASTA / "bot_participante.py"

LOGS_DIR = PASTA / "logs"
RETENCAO_DIAS = 14


def log(msg: str, log_file=None) -> None:
    linha = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(linha, flush=True)

    if log_file:
        log_file.write(linha + "\n")
        log_file.flush()


def limpar_logs_antigos() -> None:
    limite = datetime.now() - timedelta(days=RETENCAO_DIAS)

    for arquivo in LOGS_DIR.glob("execucao_*.log"):
        try:
            if datetime.fromtimestamp(arquivo.stat().st_mtime) < limite:
                arquivo.unlink()
        except Exception:
            pass


def preparar_log() -> Path:
    LOGS_DIR.mkdir(exist_ok=True)
    limpar_logs_antigos()

    nome = datetime.now().strftime("execucao_%Y-%m-%d_%H-%M-%S.log")
    return LOGS_DIR / nome


def rodar_bot(caminho: Path, nome: str, log_file) -> bool:
    log(f"=== Iniciando: {nome} ===", log_file)

    # PYTHONIOENCODING=utf-8: a saida do subprocesso e redirecionada direto
    # pro descritor de arquivo (bypassa o encoding do objeto Python), entao
    # forcamos UTF-8 no filho pra bater com o encoding do arquivo de log.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    processo = subprocess.run(
        [PYTHON, str(caminho)],
        cwd=str(PASTA),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )

    sucesso = processo.returncode == 0

    if sucesso:
        log(f"=== Concluído com sucesso: {nome} ===", log_file)
    else:
        log(f"=== Terminou com erro (código {processo.returncode}): {nome} ===", log_file)

    return sucesso


def main():
    log_path = preparar_log()

    with open(log_path, "w", encoding="utf-8") as log_file:
        log("Iniciando execução completa (gerenciador + participação)...", log_file)

        ok_gerenciador = rodar_bot(BOT_GERENCIADOR, "Bot comprasnet (pregões PMB gerenciadora)", log_file)

        log("Encerrando Chrome da automação antes do próximo bot (evita reaproveitar uma sessão degradada)...", log_file)
        fechar_chrome_automacao()

        ok_participacao = rodar_bot(BOT_PARTICIPACAO, "Bot participação (atas PMB gerenciadora + participante)", log_file)

        log("=== Resumo final ===", log_file)
        log(f"Bot gerenciador: {'OK' if ok_gerenciador else 'FALHOU'}", log_file)
        log(f"Bot participação: {'OK' if ok_participacao else 'FALHOU'}", log_file)

    if not (ok_gerenciador and ok_participacao):
        sys.exit(1)


if __name__ == "__main__":
    main()
