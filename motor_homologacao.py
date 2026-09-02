"""
motor_homologacao.py - Etapa 2.10: roda o motor de homologação contra
dados reais (90020/2026 = item normal, 13/2026 = grupo) e imprime o
relatório final. Não grava nada em dados.db, não gera Ata.
"""

from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

from capturar_grupo import capturar_itens_do_grupo
from capturar_itens_fornecedor_leve import capturar_todos_fornecedores_leve
from cruzar_item_fornecedor import _ir_para_pregao, capturar_itens_situacao
from homologacao import (
    Classificacao,
    avaliar_fornecedor_multiitem,
    avaliar_grupo,
    avaliar_item_normal,
    classificar_situacao,
)
from comum import log


def _norm(n) -> str:
    s = str(n or "").strip()
    return s.zfill(5) if s.isdigit() else s


async def processar_pregao_item_normal(page, context, numero_ano: str) -> dict:
    log(f"\n=== {numero_ano} (item normal) ===")
    pagina = await _ir_para_pregao(page, context, numero_ano)

    itens_situacao = await capturar_itens_situacao(pagina)
    itens_norm = {_norm(k): v for k, v in itens_situacao.items() if v.get("tipo") == "item"}
    log(f"Itens (tipo=item) na aba Itens: {len(itens_norm)}")

    fornecedores = await capturar_todos_fornecedores_leve(pagina)

    # agrupa por fornecedor -> lista de itens (numero+descricao+situacao real, cruzada)
    por_fornecedor: dict[tuple, list[dict]] = {}
    itens_com_algum_fornecedor: set[str] = set()

    for forn in fornecedores:
        chave = (forn["fornecedor"], forn["cnpj"])
        for it in forn.get("itens", []):
            numero = _norm(it.get("numero_item"))
            if not numero:
                continue
            itens_com_algum_fornecedor.add(numero)
            sit_real = itens_norm.get(numero, {}).get("situacao")
            desc_real = itens_norm.get(numero, {}).get("descricao") or it.get("descricao")
            por_fornecedor.setdefault(chave, []).append({
                "numero": numero, "descricao": desc_real, "situacao": sit_real,
            })

    avaliacoes = []
    for (fornecedor, cnpj), itens in por_fornecedor.items():
        avaliacoes.append(avaliar_fornecedor_multiitem(fornecedor, cnpj, itens))

    # itens que apareceram na aba Itens mas NENHUM fornecedor reivindicou
    itens_sem_fornecedor = [
        (num, info) for num, info in itens_norm.items() if num not in itens_com_algum_fornecedor
    ]
    for num, info in itens_sem_fornecedor:
        avaliacoes.append(avaliar_item_normal(num, info.get("descricao"), None, None, info.get("situacao")))

    return {
        "pregao": numero_ano,
        "tipo": "item_normal",
        "total_itens": len(itens_norm),
        "total_fornecedores": len(por_fornecedor),
        "itens_sem_fornecedor": len(itens_sem_fornecedor),
        "avaliacoes": avaliacoes,
    }


async def processar_pregao_grupo(page, context, numero_ano: str) -> dict:
    log(f"\n=== {numero_ano} (grupo) ===")
    pagina = await _ir_para_pregao(page, context, numero_ano)

    itens_situacao = await capturar_itens_situacao(pagina)
    grupos_situacao = {k: v for k, v in itens_situacao.items() if v.get("tipo") == "grupo"}
    log(f"Grupos encontrados na aba Itens: {list(grupos_situacao.keys())}")

    fornecedores = await capturar_todos_fornecedores_leve(pagina)
    log(f"Fornecedores com itens/grupos habilitados: {len(fornecedores)}")

    avaliacoes = []
    for numero_grupo, info in grupos_situacao.items():
        # fornecedor do grupo: procura entre os fornecedores capturados se
        # algum tem esse número de grupo entre os "itens" (viria como não-
        # numérico e cairia nos avisos hoje -- registrado honestamente).
        fornecedor_do_grupo = None
        cnpj_do_grupo = None
        for forn in fornecedores:
            for it in forn.get("itens", []):
                if str(it.get("numero_item", "")).upper().startswith(numero_grupo.upper()):
                    fornecedor_do_grupo = forn["fornecedor"]
                    cnpj_do_grupo = forn["cnpj"]

        # Navegação FRESCA por grupo (não reaproveita a aba entre grupos):
        # deixar o grupo anterior aberto na mesma aba faz o paginador dele
        # (mesmo escondido) competir com o do próximo grupo -- bug real,
        # já confirmado (GRUPO 2 parava em 10 de 73 por causa disso).
        pagina_grupo = await _ir_para_pregao(page, context, numero_ano)
        log(f"  Capturando itens-filhos de {numero_grupo}...")
        filhos = await capturar_itens_do_grupo(pagina_grupo, numero_grupo)
        log(f"  {numero_grupo}: {len(filhos)} item(ns)-filho, fornecedor={fornecedor_do_grupo!r}")

        avaliacoes.append(avaliar_grupo(
            numero_grupo, info.get("situacao"), fornecedor_do_grupo, cnpj_do_grupo, filhos,
        ))

    return {
        "pregao": numero_ano,
        "tipo": "grupo",
        "total_grupos": len(grupos_situacao),
        "avaliacoes": avaliacoes,
    }


def _situacoes_reais_confirmadas() -> set[str]:
    return {
        "Homologado", "Homologado (anulado)", "Aguardando julgamento",
        "Julgado e habilitado (aguardando adjudicação)", "Fracassado (aguardando homologação)",
    }


def rodar_testes_obrigatorios(resultados: list[dict]) -> None:
    print("\n" + "=" * 60)
    print("TESTES OBRIGATÓRIOS (Etapa 2.10)")
    print("=" * 60)

    todas_avaliacoes = []
    for r in resultados:
        todas_avaliacoes.extend(r.get("avaliacoes", []))

    def achou(pred, desc):
        ok = any(any(pred(item) for item in a.itens) for a in todas_avaliacoes)
        print(f"  {'[OK]' if ok else '[NAO TESTADO - sem dado real disponivel]'} {desc}")
        return ok

    achou(lambda i: i.situacao_classe == Classificacao.HOMOLOGADO, "1. Item homologado")
    achou(lambda i: i.situacao_texto == "Aguardando julgamento", "2. Item/grupo aguardando julgamento")
    achou(lambda i: i.situacao_classe == Classificacao.FRACASSADO, "3. Item/grupo fracassado")
    achou(lambda i: i.situacao_classe == Classificacao.ANULADO, "4. Item 'Homologado (anulado)'")

    algum_sem_forn = any(a.fornecedor is None for a in todas_avaliacoes)
    print(f"  {'[OK]' if algum_sem_forn else '[FALTA]'} 5. Item/grupo sem fornecedor")

    print(f"  [NAO TESTADO - sem dado real disponivel] 6. Item com múltiplos fornecedores "
          f"(a Etapa 2.9 já confirmou 28 casos reais em 90020/2026, mas o motor aqui avalia "
          f"por fornecedor, não reprocessei essa combinação específica nesta etapa)")

    grupos = [a for a in todas_avaliacoes if a.tipo == "grupo"]
    grupo_sem_forn = any(g.fornecedor is None for g in grupos)
    grupo_com_situacao = any(g for g in grupos)
    grupo_multi_filhos = any(len(g.itens) > 1 for g in grupos)
    print(f"  {'[OK]' if grupo_sem_forn else '[FALTA]'} 7. Grupo sem fornecedor")
    print(f"  {'[OK]' if grupo_com_situacao else '[FALTA]'} 8. Grupo com situação própria")
    print(f"  {'[OK]' if grupo_multi_filhos else '[FALTA]'} 9. Grupo com múltiplos itens-filhos")

    fornecedores_multi = [a for a in todas_avaliacoes if a.tipo in ("item_normal_multi",) and a.total > 1]
    apto = any(f.apto for f in fornecedores_multi)
    nao_apto = any((not f.apto) and f.pendentes > 0 for f in fornecedores_multi)
    print(f"  {'[OK]' if apto else '[NAO TESTADO]'} 10. Fornecedor com todos os itens elegíveis")
    print(f"  {'[OK]' if nao_apto else '[NAO TESTADO]'} 11. Fornecedor com itens pendentes")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = context.pages[0]
        await page.bring_to_front()

        r1 = await processar_pregao_item_normal(page, context, "90020/2026")
        r2 = await processar_pregao_grupo(page, context, "13/2026")

        resultados = [r1, r2]

        with open("_homologacao_resultado.json", "w", encoding="utf-8") as f:
            json.dump([
                {
                    **{k: v for k, v in r.items() if k != "avaliacoes"},
                    "avaliacoes": [
                        {
                            "tipo": a.tipo, "identificador": a.identificador,
                            "fornecedor": a.fornecedor, "cnpj": a.cnpj,
                            "homologados": a.homologados, "pendentes": a.pendentes,
                            "total": a.total, "apto": a.apto, "motivo": a.motivo,
                            "itens": [
                                {"numero": i.numero, "descricao": i.descricao,
                                 "situacao_texto": i.situacao_texto,
                                 "situacao_classe": i.situacao_classe.value,
                                 "elegivel": i.elegivel}
                                for i in a.itens
                            ],
                        }
                        for a in r["avaliacoes"]
                    ],
                }
                for r in resultados
            ], f, ensure_ascii=False, indent=2)

        # ---- RELATÓRIO FINAL ----
        print("\n" + "=" * 60)
        print("RELATÓRIO FINAL — ETAPA 2.10 (MOTOR DE HOMOLOGAÇÃO)")
        print("=" * 60)

        total_pregoes = len(resultados)
        total_itens = r1["total_itens"]
        total_grupos = r2["total_grupos"]
        todas_aval = r1["avaliacoes"] + r2["avaliacoes"]
        total_fornecedores = len({(a.fornecedor, a.cnpj) for a in todas_aval if a.fornecedor})
        aptos = [a for a in todas_aval if a.apto]
        nao_aptos = [a for a in todas_aval if not a.apto]

        homologados_total = sum(a.homologados for a in todas_aval)
        pendentes_total = sum(a.pendentes for a in todas_aval)
        sem_fornecedor = sum(1 for a in todas_aval if a.fornecedor is None)

        situacoes_especiais = sum(
            1 for a in todas_aval for i in a.itens
            if i.situacao_classe in (Classificacao.ANULADO, Classificacao.DESCONHECIDA)
        )

        print(f"Total de pregões analisados: {total_pregoes}")
        print(f"Total de itens (item normal): {total_itens}")
        print(f"Total de grupos: {total_grupos}")
        print(f"Total de fornecedores distintos: {total_fornecedores}")
        print(f"Fornecedores/grupos APTOS: {len(aptos)}")
        print(f"Fornecedores/grupos NÃO APTOS: {len(nao_aptos)}")
        print(f"Itens/grupos homologados (classe HOMOLOGADO): {homologados_total}")
        print(f"Itens/grupos pendentes: {pendentes_total}")
        print(f"Avaliações sem fornecedor: {sem_fornecedor}")
        print(f"Situações especiais (ANULADO/DESCONHECIDA): {situacoes_especiais}")

        print("\n--- Detalhe por fornecedor/grupo (90020/2026) ---")
        for a in sorted(r1["avaliacoes"], key=lambda x: (x.fornecedor or "zzz")):
            marca = "[APTO]" if a.apto else "[NAO APTO]"
            print(f"  [{a.tipo}] {a.fornecedor or '(sem fornecedor)'} | CNPJ {a.cnpj or '-'} | "
                  f"{a.homologados}/{a.total} homologado(s) | {marca} | {a.motivo}")

        print("\n--- Detalhe por grupo (13/2026) ---")
        for a in r2["avaliacoes"]:
            marca = "[APTO]" if a.apto else "[NAO APTO]"
            print(f"  [{a.tipo}] {a.identificador} | fornecedor={a.fornecedor or '(nenhum)'} | "
                  f"{len(a.itens)} item(ns)-filho | {marca} | {a.motivo}")

        rodar_testes_obrigatorios(resultados)

        print("\nResultado completo salvo em _homologacao_resultado.json")


if __name__ == "__main__":
    asyncio.run(main())
