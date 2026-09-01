"""Deduplicação entre instâncias (camada 5 do pipeline, PRD §7).

Sob a numeração única do CNJ (Res. 65/2008), o mesmo caso mantém o mesmo número
NNNNNNN-DD.AAAA.J.TR.OOOO ao subir de instância. A chave de deduplicação é o
número normalizado (20 dígitos); as aparições em graus/tribunais diferentes são
mantidas como registros vinculados ao mesmo número, e a análise usa uma única
aparição canônica por caso (grau mais alto disponível).
"""

import re

_ORDEM_GRAU = {"SUP": 3, "TST": 3, "G2": 2, "G1": 1}


def normalizar_numero(numero: str) -> str:
    """Reduz o número CNJ aos seus dígitos (20 quando completo)."""
    return re.sub(r"\D", "", numero or "")


def formatar_numero(numero: str) -> str:
    digitos = normalizar_numero(numero)
    if len(digitos) != 20:
        return numero or ""
    return (
        f"{digitos[0:7]}-{digitos[7:9]}.{digitos[9:13]}"
        f".{digitos[13]}.{digitos[14:16]}.{digitos[16:20]}"
    )


def peso_grau(grau: str) -> int:
    return _ORDEM_GRAU.get((grau or "").upper(), 0)


def escolher_canonico(aparicoes: list[dict]) -> dict:
    """Entre aparições do mesmo número, prefere o grau mais alto."""
    return max(aparicoes, key=lambda a: peso_grau(a.get("grau", "")))
