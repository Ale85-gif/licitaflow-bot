"""
utils/cnpj.py — validação e formatação de CNPJ.

Algoritmo público padrão (dígitos verificadores módulo 11), não depende de
nenhuma fonte externa.
"""

import re

_PESOS_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_PESOS_2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]


def limpar_cnpj(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _digito_verificador(base: str, pesos: list) -> str:
    soma = sum(int(d) * p for d, p in zip(base, pesos))
    resto = soma % 11
    return "0" if resto < 2 else str(11 - resto)


def validar_cnpj(cnpj: str) -> bool:
    digitos = limpar_cnpj(cnpj)

    if len(digitos) != 14 or digitos == digitos[0] * 14:
        return False

    dv1 = _digito_verificador(digitos[:12], _PESOS_1)
    dv2 = _digito_verificador(digitos[:12] + dv1, _PESOS_2)

    return digitos[-2:] == dv1 + dv2


def formatar_cnpj(cnpj: str) -> str:
    d = limpar_cnpj(cnpj)
    if len(d) != 14:
        return cnpj
    return f"{d[0:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:14]}"
