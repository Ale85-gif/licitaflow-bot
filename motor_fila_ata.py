"""
motor_fila_ata.py - Etapa 2.11: roda a fila de Ata contra dados reais
(90020/2026 = item normal, 13/2026 = grupo), reaproveitando o motor de
homologação (Etapa 2.10) sem duplicar captura nenhuma.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from motor_homologacao import processar_pregao_grupo, processar_pregao_item_normal
from fila_ata import ata_parcial_disponivel, construir_fila, resumo_fila, Disponibilidade
from comum import log


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = context.pages[0]
        await page.bring_to_front()

        r1 = await processar_pregao_item_normal(page, context, "90020/2026")
        r2 = await processar_pregao_grupo(page, context, "13/2026")

        fila1 = construir_fila(r1["pregao"], "160082", r1["avaliacoes"])
        fila2 = construir_fila(r2["pregao"], "160082", r2["avaliacoes"])
        fila_total = fila1 + fila2

        print("\n" + "=" * 60)
        print("RELATÓRIO FINAL — ETAPA 2.11 (FILA DE ATA)")
        print("=" * 60)

        resumo = resumo_fila(fila_total)
        print(f"Total de entradas na fila: {resumo['total']}")
        print(f"  Disponíveis para Ata (ELEGIVEL): {resumo['por_disponibilidade'][Disponibilidade.ELEGIVEL]}")
        print(f"  Pendentes: {resumo['por_disponibilidade'][Disponibilidade.PENDENTE]}")
        print(f"  Não elegíveis: {resumo['por_disponibilidade'][Disponibilidade.NAO_ELEGIVEL]}")
        print(f"  Sem fornecedor: {resumo['por_disponibilidade'][Disponibilidade.SEM_FORNECEDOR]}")
        print(f"  Total de grupos na fila: {resumo['total_grupos']}")

        print("\n--- Por fornecedor ---")
        for chave, d in sorted(resumo["por_fornecedor"].items()):
            print(f"  {d['fornecedor']} ({d['cnpj']}): total={d['total']} "
                  f"eleg={d['elegiveis']} pend={d['pendentes']} nao_eleg={d['nao_elegiveis']}")

        print("\n--- Exemplo de 'Ata parcial disponível' (fornecedores de 90020/2026) ---")
        fornecedores_vistos = {(f.fornecedor, f.cnpj) for f in fila1 if f.fornecedor}
        for fornecedor, cnpj in sorted(fornecedores_vistos)[:5]:
            parcial = ata_parcial_disponivel(fila1, fornecedor, cnpj)
            status = ("ATA COMPLETA DISPONIVEL" if parcial["ata_completa_disponivel"]
                       else "ata parcial disponivel" if parcial["ata_parcial_disponivel"]
                       else "nada disponivel ainda")
            print(f"  {fornecedor}: {len(parcial['elegiveis_para_ata_parcial'])} elegivel(is), "
                  f"{len(parcial['pendentes'])} pendente(s), {len(parcial['nao_elegiveis'])} nao elegivel(is) "
                  f"-> {status}")
            if parcial["pendentes"]:
                print(f"      itens faltantes: {', '.join(parcial['pendentes'])}")

        print("\n--- Grupos (13/2026) ---")
        for f in fila2:
            if f.tipo == "grupo":
                print(f"  {f.identificador}: situação original='{f.situacao_original}' "
                      f"classificação={f.classificacao} disponibilidade={f.disponibilidade} "
                      f"({len(f.itens_filhos)} item(ns)-filho, fornecedor={f.fornecedor or '(nenhum)'})")

        print("\nNenhuma Ata foi gerada. Nada foi salvo em dados.db.")


if __name__ == "__main__":
    asyncio.run(main())
