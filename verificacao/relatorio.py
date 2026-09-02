"""
verificacao/relatorio.py — monta o texto do relatório de verificação
(regras 15 e 16 do módulo: conclusão nunca afirma "regular", e o aviso
obrigatório sempre acompanha o relatório).
"""

from __future__ import annotations

from datetime import datetime

AVISO_OBRIGATORIO = (
    "Este relatório representa o resultado das consultas realizadas nas fontes "
    "oficiais disponíveis na data e horário indicados. A ausência de registro não "
    "deve ser interpretada isoladamente como comprovação definitiva da inexistência "
    "de sanções, considerando possíveis prazos de processamento, atualização ou "
    "integração das bases oficiais. Registros identificados deverão ser analisados "
    "conforme sua natureza, vigência, abrangência e legislação aplicável."
)


def gerar_relatorio(empresa: dict, consolidado: dict) -> dict:
    tem_registro = any(f["status"] == "registro_encontrado" for f in consolidado["fontes"])

    conclusao = (
        "Foi localizado registro que requer análise quanto à natureza, abrangência, "
        "vigência e efeitos da sanção/restrição."
        if tem_registro else
        "Nas consultas realizadas nas fontes oficiais indicadas, não foram localizados "
        "registros nas bases que puderam ser consultadas automaticamente na data e "
        "horário informados."
    )

    return {
        "empresa": empresa,
        "consolidado": consolidado,
        "conclusao": conclusao,
        "aviso": AVISO_OBRIGATORIO,
        "gerado_em": datetime.now().isoformat(),
    }
