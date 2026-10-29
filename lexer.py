"""Lexer RedAsm: preserva linhas e nomes, normaliza palavras reservadas."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

from errors import LexError
from isa import DIRECTIVES, MNEMONIC_SET, REGISTER_BY_NAME, Register


class TokenType(Enum):
    INSTRUCTION = "INSTRUÇÃO"
    DIRECTIVE = "DIRETIVA"
    REGISTER = "REGISTRADOR"
    NUMBER = "NÚMERO"
    IDENT = "IDENTIFICADOR"
    COMMA = "VÍRGULA"
    COLON = "DOIS_PONTOS"
    LBRACKET = "ABRE_COLCHETE"
    RBRACKET = "FECHA_COLCHETE"
    NEWLINE = "FIM_DE_LINHA"
    EOF = "EOF"


@dataclass(frozen=True)
class Token:
    kind: TokenType
    value: object
    line: int
    col: int
    lexeme: str

    def __repr__(self) -> str:  # pragma: no cover - depuração apenas
        return f"Token({self.kind.name}, {self.value!r}, {self.line}:{self.col})"


class Lexer:
    def __init__(self, source: str, filename: str = "<fonte>") -> None:
        self.source = source
        self.filename = filename
        self.index = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []

    def _emit(self, kind: TokenType, value: object, line: int, col: int, lexeme: str) -> None:
        self.tokens.append(Token(kind, value, line, col, lexeme))

    def _error(self, message: str, line: int, col: int, hint: str | None = None) -> NoReturn:
        raise LexError(message, filename=self.filename, line=line, col=col, hint=hint)

    def tokenize(self) -> list[Token]:
        src = self.source
        n = len(src)

        while self.index < n:
            ch = src[self.index]

            if ch in " \t\r":
                self.index += 1
                self.col += 1
                continue

            if ch == "\n":
                self._emit(TokenType.NEWLINE, "\n", self.line, self.col, "\n")
                self.index += 1
                self.line += 1
                self.col = 1
                continue

            if ch in "#;":
                while self.index < n and src[self.index] != "\n":
                    self.index += 1
                    self.col += 1
                continue

            if ch in ",:[]":
                kind = {
                    ",": TokenType.COMMA,
                    ":": TokenType.COLON,
                    "[": TokenType.LBRACKET,
                    "]": TokenType.RBRACKET,
                }[ch]
                self._emit(kind, ch, self.line, self.col, ch)
                self.index += 1
                self.col += 1
                continue

            if ch.isdigit():
                self._read_number()
                continue

            if ch == ".":
                self._read_directive()
                continue

            if ch.isalpha() or ch == "_":
                self._read_ident()
                continue

            self._error(
                f"caractere inesperado {ch!r}",
                self.line,
                self.col,
                "remova o caractere ou use '#' ou ';' para iniciar um comentário",
            )

        self._emit(TokenType.EOF, None, self.line, self.col, "")
        return self.tokens

    def _read_number(self) -> None:
        src = self.source
        start = self.index
        start_col = self.col

        while self.index < len(src) and (src[self.index].isalnum() or src[self.index] == "_"):
            self.index += 1
            self.col += 1

        lexeme = src[start:self.index]
        if lexeme.lower().startswith("0x"):
            base, digits, allowed, name = 16, lexeme[2:], "0123456789abcdefABCDEF", "hexadecimal"
        elif lexeme.lower().startswith("0b"):
            base, digits, allowed, name = 2, lexeme[2:], "01", "binário"
        else:
            base, digits, allowed, name = 10, lexeme, "0123456789", "decimal"

        if not digits:
            self._error(f"literal {name} incompleto", self.line, start_col)
        if any(ch not in allowed for ch in digits):
            self._error(
                f"literal {name} inválido: '{lexeme}'",
                self.line,
                start_col,
                "use números decimais, hexadecimais (0xF) ou binários (0b1111)",
            )
        try:
            value = int(lexeme, base)
        except ValueError:
            self._error(
                "literal numérico inválido ou grande demais para conversão",
                self.line,
                start_col,
            )

        self._emit(TokenType.NUMBER, value, self.line, start_col, lexeme)

    def _read_directive(self) -> None:
        start = self.index
        start_col = self.col
        self.index += 1
        self.col += 1
        while self.index < len(self.source) and (
            self.source[self.index].isalnum() or self.source[self.index] == "_"
        ):
            self.index += 1
            self.col += 1

        lexeme = self.source[start:self.index]
        upper = lexeme.upper()
        if upper not in DIRECTIVES:
            self._error(
                f"diretiva desconhecida '{lexeme}'",
                self.line,
                start_col,
                "diretivas válidas: " + ", ".join(sorted(DIRECTIVES)),
            )
        self._emit(TokenType.DIRECTIVE, upper, self.line, start_col, lexeme)

    def _read_ident(self) -> None:
        src = self.source
        start = self.index
        start_col = self.col

        while self.index < len(src) and (src[self.index].isalnum() or src[self.index] == "_"):
            self.index += 1
            self.col += 1

        lexeme = src[start:self.index]
        upper = lexeme.upper()

        if upper in MNEMONIC_SET:
            self._emit(TokenType.INSTRUCTION, upper, self.line, start_col, lexeme)
        elif upper in REGISTER_BY_NAME:
            self._emit(TokenType.REGISTER, REGISTER_BY_NAME[upper], self.line, start_col, lexeme)
        else:
            self._emit(TokenType.IDENT, lexeme, self.line, start_col, lexeme)


def tokenize(source: str, filename: str = "<fonte>") -> list[Token]:
    """Atalho funcional para ``Lexer(source, filename).tokenize()``."""
    return Lexer(source, filename).tokenize()


__all__ = ["Token", "TokenType", "Lexer", "tokenize", "Register"]
