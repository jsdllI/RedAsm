"""Erros do RedAsm com localização de origem.

Todos os erros do compilador/VM derivam de :class:`RedAsmError`, carregando
arquivo, linha, coluna e uma dica opcional. A CLI formata esses dados no
padrão ``arquivo:linha:coluna: erro [fase]: mensagem``.
"""
from __future__ import annotations


class RedAsmError(Exception):
    """Erro base do RedAsm."""

    phase = "geral"

    def __init__(
        self,
        message: str,
        *,
        filename: str | None = None,
        line: int | None = None,
        col: int | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.filename = filename
        self.line = line
        self.col = col
        self.hint = hint

    def __str__(self) -> str:
        location = self.filename or "<fonte>"
        if self.line is not None:
            location += f":{self.line}"
            if self.col is not None:
                location += f":{self.col}"
        text = f"{location}: erro [{self.phase}]: {self.message}"
        if self.hint:
            text += f"\n  dica: {self.hint}"
        return text


class LexError(RedAsmError):
    """Caractere/literal inválido no fluxo de entrada."""

    phase = "léxico"


class ParseError(RedAsmError):
    """Instrução, operando ou rótulo inválido."""

    phase = "sintaxe"


class VMError(RedAsmError):
    """Falha em tempo de execução (limites, orçamento, estado interno)."""

    phase = "execução"
