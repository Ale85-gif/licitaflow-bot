"""
api.py - serve o painel LicitaFlow e alimenta com os dados reais que o
Bot comprasnet .py já grava na planilha (aba BD_CONSOLIDADO).

Rodar:
    uvicorn api:app --port 8000

Depois abra http://localhost:8000 — o painel detecta a API sozinho
(detectarBackend() em licitaflow.html) e troca os dados de demonstração
pelos dados reais.

Limite conhecido: este bot só grava itens que já têm ata e fornecedor
(item_valido() em "Bot comprasnet .py" exige isso), ou seja, já passaram
da homologação. Por isso todo item aqui aparece "ok" — a parte de itens
pendentes/em diligência (fase pré-homologação) depende do outro bot.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import gspread
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from oauth2client.service_account import ServiceAccountCredentials

from verificacao.ceis import consultar_ceis
from verificacao.cnep import consultar_cnep
from verificacao.cnpj import formatar_cnpj, limpar_cnpj, validar_cnpj
from verificacao.consolidacao import consolidar
from verificacao import confirmacoes as confirmacoes_manuais
from verificacao import historico as historico_empresas
from verificacao.pncp import consultar_pncp
from verificacao.relatorio import gerar_relatorio
from verificacao.sicaf import consultar_sicaf

import processos_repo

load_dotenv()

RAIZ = Path(__file__).resolve().parent
PLANILHA_ID = "1wy8i8nuUkBFezSeySfnnPw06TVLxhwaXrwEGIoxOewU"
CHAVE_JSON = RAIZ / "chaves.json"
ARQUIVO_HTML = RAIZ / "licitaflow" / "licitaflow.html"
ANEXOS_DIR = RAIZ / "licitaflow" / "anexos"
ANEXOS_INDEX = ANEXOS_DIR / "index.json"
ATAS_GERADAS_ARQUIVO = RAIZ / "licitaflow" / "atas_geradas.json"
COMPROVANTES_DIR = RAIZ / "licitaflow" / "comprovantes_manuais"
COLETAS_HISTORICO_ARQUIVO = RAIZ / "logs" / "coletas_historico.json"
# Leitura/gravação de processos.json e a validação de processo_id vivem em
# processos_repo.py (compartilhado com bot_criar_ata.py - ver ETAPA 2
# Checkpoint 2) para não duplicar a mesma lógica em dois lugares.
UASG_FIXA = processos_repo.UASG_FIXA

TTL_CACHE_SEGUNDOS = 120

# Fixo (nao sys.executable): o .venv fica fora do OneDrive (C:\venvs\...) para
# nao ser corrompido pela sincronizacao em tempo real durante pip install.
PYTHON_VENV = r"C:\venvs\meu_projeto_python\Scripts\python.exe"
BOT_COLETA = RAIZ / "Bot comprasnet rapido.py"
ABRIR_LICITAFLOW = RAIZ / "abrir_licitaflow.py"
CAPTURAR_FASE = RAIZ / "capturar_fase.py"
COLETA_LOG = RAIZ / "logs" / "coleta_painel.log"
CONECTAR_LOG = RAIZ / "logs" / "conectar_painel.log"
FASE_LOG = RAIZ / "logs" / "fase_painel.log"
CDP_URL = "http://127.0.0.1:9222/json/version"
DB_PATH = RAIZ / "dados.db"

app = FastAPI(title="LicitaFlow")

_cache: dict = {"em": None, "pregoes": []}
_coleta_processo: Optional[subprocess.Popen] = None
_conectar_processo: Optional[subprocess.Popen] = None


def _chrome_debug_ativo() -> bool:
    """True se o Chrome da automação (perfil C:\\chrome-real, porta de
    depuração 9222) está aberto e respondendo -- é o que de fato os bots
    (rapido.py, bot_criar_ata.py etc.) precisam para funcionar."""
    try:
        return requests.get(CDP_URL, timeout=1.5).ok
    except requests.RequestException:
        return False


def _conectar_planilha():
    escopo = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(str(CHAVE_JSON), escopo)
    return gspread.authorize(creds).open_by_key(PLANILHA_ID)


def _numero(txt) -> float:
    s = str(txt or "").replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else 0.0


# Cobre CNPJ completo ("12.345.678/0001-90 - "), CPF ("123.456.789-00 - ") e
# variações truncadas que o Google Sheets produz ao tratar o documento como
# número (ex.: "53.665.245 " sem os dígitos finais). Qualquer prefixo de
# 6+ caracteres feito só de dígitos/pontos/barras/hífens, seguido de um
# hífen opcional, é considerado "número de documento" e descartado.
RX_ID_PREFIXO = re.compile(r"^[\d./-]{6,20}\s*-?\s*")


def _separar_documento_nome(bruto: str) -> tuple[str, str]:
    """A planilha guarda 'documento - Razão Social'. Separa os dois: o painel
    mostra o nome, e o CNPJ passa a ficar disponível para a Verificação de
    Empresas (antes ele era descartado aqui).

    Alguns cadastros do portal repetem o número (CNPJ na frente e de novo
    dentro do próprio nome), então repete a limpeza do nome até estabilizar.
    """
    bruto = bruto.strip()
    m = RX_ID_PREFIXO.match(bruto)
    documento = re.sub(r"\D", "", m.group(0)) if m else ""

    nome = bruto
    anterior = None
    while nome != anterior:
        anterior = nome
        nome = RX_ID_PREFIXO.sub("", nome).strip()

    return documento, (nome or bruto)


def _data_iso(txt: str) -> Optional[str]:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(txt or ""))
    if not m:
        return None
    d, mes, ano = m.groups()
    try:
        return date(int(ano), int(mes), int(d)).isoformat()
    except ValueError:
        return None


def _carregar_pregoes() -> list[dict]:
    planilha = _conectar_planilha()
    ws = planilha.worksheet("BD_CONSOLIDADO")
    linhas = ws.get_all_records()

    grupos: dict[str, list[dict]] = {}
    for linha in linhas:
        pregao = str(linha.get("Pregão Origem", "")).strip()
        if pregao:
            grupos.setdefault(pregao, []).append(linha)

    pregoes = []
    for numero, itens_brutos in grupos.items():
        itens = []
        fornecedores: dict[str, str] = {}  # nome -> cnpj (dígitos, pode ser "")
        valor_total = 0.0
        vig_fins = []

        for idx, item in enumerate(itens_brutos, start=1):
            n_txt = re.sub(r"\D", "", str(item.get("Número Item", "")))
            n = int(n_txt) if n_txt else idx

            documento, fornecedor = _separar_documento_nome(str(item.get("Fornecedor", "")))
            if fornecedor:
                fornecedores.setdefault(fornecedor, documento)

            valor_total += _numero(item.get("Valor Unitário")) * _numero(item.get("Total"))

            vig_fim_iso = _data_iso(item.get("Vig Fim", ""))
            if vig_fim_iso:
                vig_fins.append(vig_fim_iso)

            itens.append({
                "n": n,
                "desc": str(item.get("Descrição", "")).strip() or f"Item {n}",
                "status": "ok",
            })

        itens.sort(key=lambda i: i["n"])

        pregoes.append({
            "numero": numero,
            "objeto": str(itens_brutos[0].get("Descrição", "")).strip() if itens_brutos else "",
            "fornecedores": [
                {
                    "nome": nome,
                    "cnpj": documento,
                    "cnpjFormatado": formatar_cnpj(documento) if validar_cnpj(documento) else documento,
                }
                for nome, documento in sorted(fornecedores.items())
            ],
            # "fase" NÃO entra aqui mais (era um valor fixo/inventado). Quem
            # preenche é _status(), a partir da tabela pregoes_fase real
            # (ver capturar_fase.py — Etapa 2.5).
            "valor": round(valor_total, 2),
            "ataVigencia": min(vig_fins) if vig_fins else None,
            "ataAssinada": True,
            "atualizado": date.today().isoformat(),
            "itens": itens,
        })

    pregoes.sort(key=lambda p: p["numero"])
    return pregoes


def _slug_pregao(numero: str) -> str:
    return re.sub(r"[^\w.-]", "_", numero)


def _carregar_anexos() -> dict:
    if not ANEXOS_INDEX.exists():
        return {}
    try:
        return json.loads(ANEXOS_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _salvar_anexos(indice: dict) -> None:
    ANEXOS_DIR.mkdir(parents=True, exist_ok=True)
    ANEXOS_INDEX.write_text(json.dumps(indice, ensure_ascii=False, indent=2), encoding="utf-8")


def _carregar_atas_geradas() -> dict:
    if not ATAS_GERADAS_ARQUIVO.exists():
        return {}
    try:
        return json.loads(ATAS_GERADAS_ARQUIVO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _salvar_atas_geradas(indice: dict) -> None:
    ATAS_GERADAS_ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    ATAS_GERADAS_ARQUIVO.write_text(json.dumps(indice, ensure_ascii=False, indent=2), encoding="utf-8")


def _carregar_coletas() -> list[dict]:
    if not COLETAS_HISTORICO_ARQUIVO.exists():
        return []
    try:
        return json.loads(COLETAS_HISTORICO_ARQUIVO.read_text(encoding="utf-8"))
    except Exception:
        return []


def _registrar_coleta(ok: bool, msg: str) -> None:
    """Grava o resultado de uma coleta em disco -- sem isso, 'Últimas coletas'
    esvaziava a cada reload da página porque só existia em memória no JS."""
    historico = _carregar_coletas()
    historico.insert(0, {
        "em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "ok": ok,
        "msg": msg,
    })
    historico = historico[:8]
    COLETAS_HISTORICO_ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    COLETAS_HISTORICO_ARQUIVO.write_text(
        json.dumps(historico, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _carregar_fases() -> dict[str, dict]:
    """Lê a tabela `pregoes_fase` (gravada por capturar_fase.py — Etapa 2.5).
    Só leitura, tabela própria, não mexe em nada que os outros bots usam."""
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT pregao, fase, encontrado, motivo, atualizado_em FROM pregoes_fase"
        )
        return {r["pregao"]: dict(r) for r in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}  # tabela ainda não existe (nenhuma captura rodou ainda)
    finally:
        conn.close()


def _status() -> dict:
    agora = datetime.now()
    cache_velho = not _cache["em"] or (agora - _cache["em"]).total_seconds() > TTL_CACHE_SEGUNDOS
    if cache_velho:
        _cache["pregoes"] = _carregar_pregoes()
        _cache["em"] = agora

    anexos = _carregar_anexos()
    atas_geradas = _carregar_atas_geradas()
    fases = _carregar_fases()
    for p in _cache["pregoes"]:
        p["anexo"] = anexos.get(p["numero"])
        p["atasGeradas"] = atas_geradas.get(p["numero"], [])

        info_fase = fases.get(p["numero"])
        if info_fase:
            p["fase"] = info_fase["fase"]  # None se a última leitura não encontrou
            p["faseCapturadaEm"] = info_fase["atualizado_em"]
            p["faseMotivo"] = info_fase["motivo"]
        else:
            p["fase"] = None
            p["faseCapturadaEm"] = None
            p["faseMotivo"] = None

    return {
        "sessao": {
            "valida": _chrome_debug_ativo(),
            "expiraEm": None,
            "usuario": "Chrome da automação",
        },
        "coletas": _carregar_coletas(),
        "pregoes": _cache["pregoes"],
    }


@app.get("/")
def painel():
    return FileResponse(ARQUIVO_HTML)


@app.get("/api/status")
def status():
    try:
        return _status()
    except Exception as e:
        return JSONResponse({"erro": str(e)}, status_code=500)


@app.post("/api/pregoes/{numero:path}/anexo")
async def anexar_tr(numero: str, arquivo: UploadFile = File(...)):
    pasta = ANEXOS_DIR / _slug_pregao(numero)
    pasta.mkdir(parents=True, exist_ok=True)

    # Só um TR por pregão: remove o anexo anterior antes de salvar o novo.
    for antigo in pasta.glob("*"):
        antigo.unlink(missing_ok=True)

    destino = pasta / arquivo.filename
    with destino.open("wb") as f:
        shutil.copyfileobj(arquivo.file, f)

    indice = _carregar_anexos()
    indice[numero] = {"arquivo": arquivo.filename, "enviado_em": datetime.now().isoformat()}
    _salvar_anexos(indice)

    return {"ok": True, "arquivo": arquivo.filename}


@app.get("/api/pregoes/{numero:path}/anexo")
async def baixar_tr(numero: str):
    info = _carregar_anexos().get(numero)
    if not info:
        return JSONResponse({"erro": "sem_anexo"}, status_code=404)

    caminho = ANEXOS_DIR / _slug_pregao(numero) / info["arquivo"]
    if not caminho.exists():
        return JSONResponse({"erro": "arquivo_nao_encontrado"}, status_code=404)

    return FileResponse(caminho, filename=info["arquivo"])


@app.delete("/api/pregoes/{numero:path}/anexo")
async def remover_tr(numero: str):
    indice = _carregar_anexos()
    info = indice.pop(numero, None)
    if info:
        caminho = ANEXOS_DIR / _slug_pregao(numero) / info["arquivo"]
        caminho.unlink(missing_ok=True)
        _salvar_anexos(indice)
    return {"ok": True}


@app.post("/api/pregoes/{numero:path}/atas")
async def gerar_ata_parcial(numero: str, corpo: dict):
    itens = corpo.get("itens") or []
    if not itens:
        return JSONResponse({"ok": False, "erro": "sem_itens"}, status_code=400)

    processo_id = corpo.get("processoId")
    processos = processos_repo.carregar_processos()
    processo = processos.get(processo_id) if processo_id else None
    if not processo or processo.get("pregaoCompleto") != numero:
        return JSONResponse({"ok": False, "erro": "processo_nao_confirmado"}, status_code=409)

    indice = _carregar_atas_geradas()
    existentes = indice.setdefault(numero, [])

    ja_cobertos = {n for ata in existentes for n in ata["itens"]}
    novos = sorted({int(n) for n in itens if int(n) not in ja_cobertos})
    if not novos:
        return JSONResponse({"ok": False, "erro": "itens_ja_cobertos"}, status_code=400)

    existentes.append({
        "id": int(datetime.now().timestamp() * 1000),
        "itens": novos,
        "criadaEm": datetime.now().isoformat(),
        "processoId": processo_id,
    })
    _salvar_atas_geradas(indice)

    return {"ok": True}


# ── Identificação do Processo (Pregão + TR) antes de gerar Ata ──────────────

@app.post("/api/processos/localizar")
def localizar_processo(corpo: dict):
    uasg = str(corpo.get("uasg", "")).strip()
    pregao = str(corpo.get("pregao", "")).strip()
    tr = str(corpo.get("tr", "")).strip()
    numero_processo = str(corpo.get("numeroProcesso", "")).strip()

    if uasg != UASG_FIXA:
        return {"ok": False, "motivo": "uasg_incompativel"}

    pregoes = {p["numero"]: p for p in _carregar_pregoes()}
    pregao_dado = pregoes.get(pregao)
    if not pregao_dado:
        return {"ok": False, "motivo": "pregao_nao_encontrado"}

    anexo = _carregar_anexos().get(pregao)
    if not anexo:
        return {
            "ok": False,
            "motivo": "tr_nao_encontrado",
            "detalhe": "Nenhum Termo de Referência anexado para este pregão. Envie o PDF do TR na aba do pregão primeiro.",
        }

    num_pregao, ano_pregao = processos_repo.partir_composto(pregao)
    num_tr, ano_tr = processos_repo.partir_composto(tr)

    resposta = {
        "ok": True,
        "processo": {
            "uasg": uasg,
            "pregao": pregao,
            "numeroPregao": num_pregao,
            "anoPregao": ano_pregao,
            "tr": tr,
            "numeroTR": num_tr,
            "anoTR": ano_tr,
            "numeroProcesso": numero_processo,
            "objeto": pregao_dado.get("objeto", ""),
        },
    }

    anterior = processos_repo.processo_confirmado_para(pregao)
    if anterior and (
        (tr and anterior.get("tr") and anterior["tr"] != tr)
        or (numero_processo and anterior.get("numeroProcesso") and anterior["numeroProcesso"] != numero_processo)
    ):
        resposta["aviso"] = "conflito_com_processo_anterior"
        resposta["processoAnterior"] = {
            "tr": anterior.get("tr"),
            "numeroProcesso": anterior.get("numeroProcesso"),
            "confirmadoEm": anterior.get("confirmadoEm"),
        }

    return resposta


@app.post("/api/processos/confirmar")
def confirmar_processo(corpo: dict):
    uasg = str(corpo.get("uasg", "")).strip()
    pregao = str(corpo.get("pregao", "")).strip()
    tr = str(corpo.get("tr", "")).strip()
    numero_processo = str(corpo.get("numeroProcesso", "")).strip()

    if uasg != UASG_FIXA or not pregao or not tr:
        return JSONResponse({"ok": False, "erro": "dados_incompletos"}, status_code=400)

    num_pregao, ano_pregao = processos_repo.partir_composto(pregao)
    num_tr, ano_tr = processos_repo.partir_composto(tr)
    processo_id = processos_repo.montar_processo_id(uasg, num_pregao, ano_pregao, num_tr, ano_tr)

    pregoes = {p["numero"]: p for p in _carregar_pregoes()}
    pregao_dado = pregoes.get(pregao, {})

    processos = processos_repo.carregar_processos()
    processos[processo_id] = {
        "uasg": uasg,
        "numeroPregao": num_pregao,
        "anoPregao": ano_pregao,
        "tr": tr,
        "numeroTR": num_tr,
        "anoTR": ano_tr,
        "numeroProcesso": numero_processo,
        "pregaoCompleto": pregao,
        "objeto": pregao_dado.get("objeto", ""),
        "confirmadoEm": datetime.now().isoformat(),
    }
    processos_repo.salvar_processos(processos)

    return {"ok": True, "processoId": processo_id}


@app.get("/api/processos/por-pregao/{numero:path}")
def processo_por_pregao(numero: str):
    processo = processos_repo.processo_confirmado_para(numero)
    return {"processo": processo}


def _aplicar_confirmacoes_manuais(cnpj: str, resultados: list[dict]) -> None:
    """Nunca transforma indisponibilidade em resultado positivo: só troca o
    rótulo de 'não consultado' para 'confirmado manualmente' quando existe
    um registro de que um humano de fato conferiu a fonte oficial — a
    fonte automática (consultar_sicaf) continua sempre dizendo a verdade
    ('não consultado'), essa função só decora o resultado por cima."""
    for r in resultados:
        if r["status"] != "nao_consultado":
            continue
        confirmacao = confirmacoes_manuais.obter(cnpj, r["fonte"])
        if not confirmacao:
            continue
        r["status"] = "confirmado_manualmente"
        r["mensagem"] = "Consulta manual confirmada pelo usuário na fonte oficial."
        r["confirmacaoManual"] = confirmacao


async def _rodar_consultas(cnpj: str, corpo: dict) -> list[dict]:
    resultados = list(await asyncio.gather(
        consultar_ceis(cnpj),
        consultar_cnep(cnpj),
        consultar_sicaf(cnpj),
        consultar_pncp(
            cnpj,
            corpo.get("cnpjOrgao"),
            corpo.get("dataInicial"),
            corpo.get("dataFinal"),
        ),
    ))
    _aplicar_confirmacoes_manuais(cnpj, resultados)
    return resultados


@app.post("/api/empresas/verificar")
async def verificar_empresa(corpo: dict):
    cnpj = limpar_cnpj(corpo.get("cnpj", ""))
    if not validar_cnpj(cnpj):
        return JSONResponse({"erro": "cnpj_invalido"}, status_code=400)

    resultados = await _rodar_consultas(cnpj, corpo)
    consolidado = consolidar(resultados)
    registro = historico_empresas.registrar(
        cnpj, consolidado,
        pregao=corpo.get("pregao"),
        razao_social=corpo.get("razaoSocial", ""),
    )

    return {
        "cnpj": formatar_cnpj(cnpj),
        "consolidado": consolidado,
        "novidades": registro["mudancas"],
    }


@app.post("/api/empresas/{cnpj}/confirmar-manual")
async def confirmar_manual(cnpj: str, fonte: str = Form("SICAF"), arquivo: Optional[UploadFile] = File(None)):
    limpo = limpar_cnpj(cnpj)
    nome_arquivo = None

    if arquivo is not None and arquivo.filename:
        pasta = COMPROVANTES_DIR / limpo
        pasta.mkdir(parents=True, exist_ok=True)
        nome_arquivo = arquivo.filename
        with (pasta / nome_arquivo).open("wb") as f:
            shutil.copyfileobj(arquivo.file, f)

    entrada = confirmacoes_manuais.registrar(limpo, fonte, nome_arquivo)
    return {"ok": True, "confirmacao": entrada}


@app.delete("/api/empresas/{cnpj}/confirmar-manual")
async def remover_confirmacao_manual(cnpj: str, fonte: str = "SICAF"):
    confirmacoes_manuais.remover(limpar_cnpj(cnpj), fonte)
    return {"ok": True}


@app.get("/api/empresas/{cnpj}/comprovante/{fonte}")
async def baixar_comprovante_manual(cnpj: str, fonte: str):
    limpo = limpar_cnpj(cnpj)
    confirmacao = confirmacoes_manuais.obter(limpo, fonte)
    if not confirmacao or not confirmacao.get("arquivo"):
        return JSONResponse({"erro": "sem_comprovante"}, status_code=404)

    caminho = COMPROVANTES_DIR / limpo / confirmacao["arquivo"]
    if not caminho.exists():
        return JSONResponse({"erro": "arquivo_nao_encontrado"}, status_code=404)

    return FileResponse(caminho, filename=confirmacao["arquivo"])


@app.get("/api/empresas/{cnpj}/historico")
async def historico_empresa(cnpj: str):
    limpo = limpar_cnpj(cnpj)
    return {"cnpj": formatar_cnpj(limpo), "historico": historico_empresas.historico(limpo)}


@app.post("/api/empresas/relatorio")
async def relatorio_empresa(corpo: dict):
    cnpj = limpar_cnpj(corpo.get("cnpj", ""))
    if not validar_cnpj(cnpj):
        return JSONResponse({"erro": "cnpj_invalido"}, status_code=400)

    resultados = await _rodar_consultas(cnpj, corpo)
    consolidado = consolidar(resultados)

    empresa = {"cnpj": formatar_cnpj(cnpj), "razaoSocial": corpo.get("razaoSocial", "")}
    return gerar_relatorio(empresa, consolidado)


@app.post("/api/sessao/conectar")
def conectar():
    global _conectar_processo

    if _chrome_debug_ativo():
        return {"ok": True, "detalhe": "Chrome da automação já está aberto. Faça login se ainda não tiver feito."}

    if _conectar_processo is not None and _conectar_processo.poll() is None:
        return {"ok": True, "detalhe": "Chrome já está sendo aberto, aguarde..."}

    CONECTAR_LOG.parent.mkdir(exist_ok=True)
    log_arquivo = open(CONECTAR_LOG, "w", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    _conectar_processo = subprocess.Popen(
        [PYTHON_VENV, str(ABRIR_LICITAFLOW)],
        cwd=str(RAIZ),
        stdout=log_arquivo,
        stderr=subprocess.STDOUT,
        env=env,
    )

    return {"ok": True, "detalhe": "Abrindo Chrome... Faça login no Compras.gov.br quando a janela aparecer."}


def _acompanhar_coleta(processo: subprocess.Popen, log_arquivo) -> None:
    """Roda numa thread separada: espera o bot terminar e grava o
    resultado real (código de saída) no histórico persistido em disco."""
    codigo = processo.wait()
    log_arquivo.close()
    if codigo == 0:
        _registrar_coleta(True, "Coleta concluída — dados atualizados.")
    else:
        _registrar_coleta(False, f"Coleta terminou com erro (código {codigo}).")


@app.post("/api/coletar")
def coletar():
    global _coleta_processo

    if _coleta_processo is not None and _coleta_processo.poll() is None:
        return {
            "ok": False,
            "detalhe": "Já tem uma coleta rodando em background. Aguarde terminar antes de disparar outra.",
        }

    if not _chrome_debug_ativo():
        return {
            "ok": False,
            "detalhe": "Chrome da automação não está aberto. Clique em 'Conectar' e faça login antes de coletar.",
        }

    COLETA_LOG.parent.mkdir(exist_ok=True)
    log_arquivo = open(COLETA_LOG, "w", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    _coleta_processo = subprocess.Popen(
        [PYTHON_VENV, str(BOT_COLETA)],
        cwd=str(RAIZ),
        stdout=log_arquivo,
        stderr=subprocess.STDOUT,
        env=env,
    )
    threading.Thread(
        target=_acompanhar_coleta, args=(_coleta_processo, log_arquivo), daemon=True
    ).start()

    return {
        "ok": True,
        "detalhe": (
            "Coleta iniciada em background (Bot comprasnet rapido.py). "
            f"Acompanhe em {COLETA_LOG.name} e atualize a página quando terminar."
        ),
    }


@app.get("/api/coletar/status")
def coletar_status():
    if _coleta_processo is None:
        return {"rodando": False, "iniciado": False}

    rodando = _coleta_processo.poll() is None
    return {
        "rodando": rodando,
        "iniciado": True,
        "codigo_saida": None if rodando else _coleta_processo.returncode,
    }


# ── Etapa 2.5: captura da FASE real (capturar_fase.py) ──────────────────────

_fase_processo: Optional[subprocess.Popen] = None


@app.post("/api/fase/atualizar")
def atualizar_fase(pregao: str):
    """Dispara capturar_fase.py para UM pregão específico ('Analisar Pregão').
    Não mexe em fornecedores/itens/homologação — só a fase."""
    global _fase_processo

    if _fase_processo is not None and _fase_processo.poll() is None:
        return {"ok": False, "detalhe": "Já tem uma verificação de fase rodando. Aguarde terminar."}

    if not _chrome_debug_ativo():
        return {"ok": False, "detalhe": "Chrome da automação não está aberto. Clique em 'Conectar' primeiro."}

    FASE_LOG.parent.mkdir(exist_ok=True)
    log_arquivo = open(FASE_LOG, "w", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    _fase_processo = subprocess.Popen(
        [PYTHON_VENV, str(CAPTURAR_FASE), pregao],
        cwd=str(RAIZ),
        stdout=log_arquivo,
        stderr=subprocess.STDOUT,
        env=env,
    )

    return {"ok": True, "detalhe": f"Verificando fase do pregão {pregao}..."}


@app.get("/api/fase/status")
def fase_status():
    if _fase_processo is None:
        return {"rodando": False, "iniciado": False}

    rodando = _fase_processo.poll() is None
    return {
        "rodando": rodando,
        "iniciado": True,
        "codigo_saida": None if rodando else _fase_processo.returncode,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=False)
