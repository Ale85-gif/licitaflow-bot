"""
verificacao/sicaf.py — SICAF (restrição de contratar com a Administração Pública).

Verificado em 30/08/2026: a única "API pública" do SICAF referenciada em
material antigo (http://api.comprasnet.gov.br/sicaf/...) está fora do ar
(conexão recusada) — não existe hoje uma API oficial documentada para esta
consulta. A consulta de restrição de contratar é feita apenas pela página
autenticada do Comprasnet.

Por regra deste módulo: nunca fazer scraping dessa página autenticada, nunca
tentar contornar login/captcha, e nunca inventar um resultado. Este módulo
sempre devolve "não consultado automaticamente" com o link oficial — se um
dia surgir uma API pública documentada, é só trocar a implementação aqui.
"""

from __future__ import annotations

from datetime import datetime

FONTE = "SICAF"
FONTE_URL = (
    "https://www3.comprasnet.gov.br/sicaf-web/public/pages/consultas/"
    "consultarRestricaoContratarAdministracaoPublica.jsf"
)


async def consultar_sicaf(cnpj: str) -> dict:
    return {
        "fonte": FONTE,
        "status": "nao_consultado",
        "mensagem": "A consulta desta informação do SICAF deve ser realizada diretamente na fonte oficial.",
        "registros": [],
        "consultado_em": datetime.now().isoformat(),
        "fonte_url": FONTE_URL,
        "erro_detalhe": None,
    }
