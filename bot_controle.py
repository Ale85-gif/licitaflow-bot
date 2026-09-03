"""
bot_controle.py - camada de orquestração da execução do bot dentro do SARP.

Não duplica a lógica de coleta: só decide QUANDO chamar `executar_bots.py`
(como subprocesso), acompanha o estado da execução e lê os números pós-
execução direto do dados.db — nunca usa contagem fixa.

Fluxo (sem mover a coleta pra cá):
    SARP "iniciar"  →  subprocess executar_bots.py  →  bots gravam no
    dados.db  →  este módulo relê o dados.db  →  SARP mostra o resultado.

Risco conhecido (documentado, não resolvido aqui): o Agendador de Tarefas
do Windows já roda executar_bots.py sozinho, de hora em hora (10h-17h),
independente do SARP. Este módulo evita que o PRÓPRIO SARP dispare duas
execuções ao mesmo tempo (trava em memória), mas não enxerga uma execução
disparada pelo Agendador de Tarefas fora daqui — as duas rodando juntas
disputariam o mesmo Chrome/perfil (C:\\chrome-real).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

RAIZ = Path(__file__).resolve().parent
SCRIPT_ORQUESTRADOR = RAIZ / "executar_bots.py"
DB_PATH = RAIZ / "dados.db"
ARQUIVO_CONTROLE = RAIZ / "licitaflow" / "bot_controle.json"

FREQUENCIAS_VALIDAS = {5, 10, 15, 30, 60}
INTERVALO_VERIFICACAO_SEGUNDOS = 15

# Estado em memória (não persiste — reinicia com o processo do api.py, o que
# é esperado: se o api.py caiu, qualquer execução em andamento também caiu).
_estado: dict = {
    "executando": False,
    "origem": None,       # "manual" | "automatico"
    "iniciadoEm": None,
    "processo": None,     # asyncio.subprocess.Process
}


def _config_padrao() -> dict:
    # Padrão inicial exigido: manual, automático desligado, 15 minutos.
    return {"automaticoAtivo": False, "frequenciaMinutos": 15, "pausado": False}


def _carregar() -> dict:
    if not ARQUIVO_CONTROLE.exists():
        return {"config": _config_padrao(), "ultimaExecucao": None, "proximaExecucao": None, "ultimaVerificacao": None}
    try:
        dados = json.loads(ARQUIVO_CONTROLE.read_text(encoding="utf-8"))
    except Exception:
        dados = {}
    dados.setdefault("config", _config_padrao())
    dados.setdefault("ultimaExecucao", None)
    dados.setdefault("proximaExecucao", None)
    dados.setdefault("ultimaVerificacao", None)
    return dados


def _salvar(dados: dict) -> None:
    ARQUIVO_CONTROLE.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO_CONTROLE.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


def _numeros_reais() -> dict:
    """Nunca usa número fixo: lê o dados.db depois de cada execução."""
    if not DB_PATH.exists():
        return {"pregoesAnalisados": 0, "itensEncontrados": 0}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        pregoes = conn.execute('SELECT COUNT(*) FROM "pregoes_indice"').fetchone()[0]
        itens = conn.execute('SELECT COUNT(*) FROM "pregoes_itens"').fetchone()[0]
    except sqlite3.OperationalError:
        pregoes, itens = 0, 0
    finally:
        conn.close()
    return {"pregoesAnalisados": pregoes, "itensEncontrados": itens}


def status_publico() -> dict:
    dados = _carregar()
    cfg = dados["config"]

    if _estado["executando"]:
        situacao = "executando"
    elif cfg["automaticoAtivo"]:
        situacao = "pausado" if cfg["pausado"] else "aguardando"
    elif dados["ultimaExecucao"] and dados["ultimaExecucao"].get("erro"):
        situacao = "erro"
    else:
        situacao = "pronto"

    return {
        "status": situacao,
        "executando": _estado["executando"],
        "origemExecucaoAtual": _estado["origem"],
        "config": cfg,
        "ultimaExecucao": dados["ultimaExecucao"],
        "proximaExecucao": dados["proximaExecucao"],
        "ultimaVerificacao": dados["ultimaVerificacao"],
    }


async def iniciar(origem: str = "manual") -> dict:
    # Regra 7: nunca duas execuções ao mesmo tempo.
    if _estado["executando"]:
        return {"ok": False, "erro": "ja_executando"}

    if not SCRIPT_ORQUESTRADOR.exists():
        return {"ok": False, "erro": "script_nao_encontrado"}

    _estado["executando"] = True
    _estado["origem"] = origem
    _estado["iniciadoEm"] = datetime.now()

    asyncio.create_task(_executar_e_registrar())
    return {"ok": True, "status": "executando"}


async def _executar_e_registrar() -> None:
    inicio = _estado["iniciadoEm"]
    erro_msg = None
    codigo = None

    try:
        processo = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT_ORQUESTRADOR),
            cwd=str(RAIZ),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _estado["processo"] = processo
        codigo = await processo.wait()
        if codigo != 0:
            erro_msg = (
                f"executar_bots.py terminou com código {codigo}. "
                "Não foi possível concluir a análise — veja logs/ para detalhes."
            )
    except Exception as e:
        erro_msg = f"Não foi possível concluir a análise. Falha ao iniciar o processo do bot: {e}"

    fim = datetime.now()
    numeros = _numeros_reais()

    dados = _carregar()
    dados["ultimaExecucao"] = {
        "origem": _estado["origem"],
        "iniciadoEm": inicio.isoformat(),
        "finalizadoEm": fim.isoformat(),
        "duracaoSegundos": round((fim - inicio).total_seconds()),
        "codigoSaida": codigo,
        "erro": erro_msg,
        **numeros,
    }

    if dados["config"]["automaticoAtivo"] and not dados["config"]["pausado"]:
        dados["proximaExecucao"] = (fim + timedelta(minutes=dados["config"]["frequenciaMinutos"])).isoformat()

    _salvar(dados)

    _estado["executando"] = False
    _estado["origem"] = None
    _estado["processo"] = None


async def parar() -> dict:
    """Mata a ÁRVORE inteira de processos, não só o `executar_bots.py`.

    Achado em teste real: executar_bots.py chama Bot comprasnet rapido.py /
    bot_participante.py via subprocess.run() (processo filho). Um simples
    processo.terminate() no processo pai deixa o filho órfão e rodando
    sozinho — exatamente o que aconteceu e teve que ser encerrado na mão.
    `taskkill /T` mata pai e descendentes de uma vez.
    """
    processo = _estado.get("processo")
    if not processo or not _estado["executando"]:
        return {"ok": False, "erro": "nao_esta_executando"}

    try:
        matador = await asyncio.create_subprocess_exec(
            "taskkill", "/F", "/T", "/PID", str(processo.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await matador.wait()
    except Exception:
        processo.terminate()  # melhor que nada, se o taskkill falhar

    return {"ok": True}


def atualizar_config(automatico_ativo: Optional[bool] = None, frequencia_minutos: Optional[int] = None) -> dict:
    dados = _carregar()
    cfg = dados["config"]

    if frequencia_minutos is not None:
        if frequencia_minutos not in FREQUENCIAS_VALIDAS:
            return {"ok": False, "erro": "frequencia_invalida"}
        cfg["frequenciaMinutos"] = frequencia_minutos
        if cfg["automaticoAtivo"] and not cfg["pausado"]:
            dados["proximaExecucao"] = (datetime.now() + timedelta(minutes=frequencia_minutos)).isoformat()

    if automatico_ativo is not None:
        ligou_agora = automatico_ativo and not cfg["automaticoAtivo"]
        cfg["automaticoAtivo"] = automatico_ativo
        cfg["pausado"] = False
        if ligou_agora:
            dados["proximaExecucao"] = (datetime.now() + timedelta(minutes=cfg["frequenciaMinutos"])).isoformat()
        if not automatico_ativo:
            dados["proximaExecucao"] = None

    _salvar(dados)
    return {"ok": True, "config": cfg}


def pausar() -> dict:
    dados = _carregar()
    if not dados["config"]["automaticoAtivo"]:
        return {"ok": False, "erro": "automatico_desativado"}
    dados["config"]["pausado"] = True
    dados["proximaExecucao"] = None
    _salvar(dados)
    return {"ok": True}


def retomar() -> dict:
    dados = _carregar()
    if not dados["config"]["automaticoAtivo"]:
        return {"ok": False, "erro": "automatico_desativado"}
    dados["config"]["pausado"] = False
    dados["proximaExecucao"] = (datetime.now() + timedelta(minutes=dados["config"]["frequenciaMinutos"])).isoformat()
    _salvar(dados)
    return {"ok": True}


async def loop_automatico() -> None:
    """Roda para sempre no fundo do api.py: a cada 15s verifica se está na
    hora de rodar. Nunca soma outra execução em cima de uma já em andamento
    (mesma trava do modo manual, via iniciar())."""
    while True:
        try:
            dados = _carregar()
            cfg = dados["config"]
            agora = datetime.now()

            dados = _carregar()
            dados["ultimaVerificacao"] = agora.isoformat()
            _salvar(dados)

            if cfg["automaticoAtivo"] and not cfg["pausado"] and not _estado["executando"]:
                proxima_txt = dados.get("proximaExecucao")
                proxima = datetime.fromisoformat(proxima_txt) if proxima_txt else agora
                if agora >= proxima:
                    await iniciar(origem="automatico")
        except Exception:
            pass

        await asyncio.sleep(INTERVALO_VERIFICACAO_SEGUNDOS)
