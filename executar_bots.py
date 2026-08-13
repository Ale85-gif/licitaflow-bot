import subprocess
import sys
from datetime import datetime
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
# =========================================================

PASTA = Path(__file__).resolve().parent
PYTHON = sys.executable

BOT_GERENCIADOR = PASTA / "Bot comprasnet rapido.py"
BOT_PARTICIPACAO = PASTA / "bot_participante.py"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def rodar_bot(caminho: Path, nome: str) -> bool:
    log(f"=== Iniciando: {nome} ===")

    processo = subprocess.run(
        [PYTHON, str(caminho)],
        cwd=str(PASTA),
    )

    sucesso = processo.returncode == 0

    if sucesso:
        log(f"=== Concluído com sucesso: {nome} ===")
    else:
        log(f"=== Terminou com erro (código {processo.returncode}): {nome} ===")

    return sucesso


def main():
    log("Iniciando execução completa (gerenciador + participação)...")

    ok_gerenciador = rodar_bot(BOT_GERENCIADOR, "Bot comprasnet (pregões PMB gerenciadora)")

    log("Encerrando Chrome da automação antes do próximo bot (evita reaproveitar uma sessão degradada)...")
    fechar_chrome_automacao()

    ok_participacao = rodar_bot(BOT_PARTICIPACAO, "Bot participação (atas PMB gerenciadora + participante)")

    log("=== Resumo final ===")
    log(f"Bot gerenciador: {'OK' if ok_gerenciador else 'FALHOU'}")
    log(f"Bot participação: {'OK' if ok_participacao else 'FALHOU'}")


if __name__ == "__main__":
    main()
