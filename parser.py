"""Parser RedAsm: coleta constantes, compila linhas e resolve rótulos."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import NoReturn

from errors import ParseError
from isa import (
    DUST_REGISTERS,
    MAX_POWER,
    MAX_TICKDELAY,
    MEMORY_SIZE,
    MIN_POWER,
    MNEMONICS,
    SIGNATURES,
    Register,
)
from lexer import Token, TokenType, tokenize


@dataclass(frozen=True)
class LabelRef:
    name: str
    line: int
    col: int


@dataclass(frozen=True)
class MemoryRef:
    high: Register | None
    low: Register


@dataclass(frozen=True)
class Instruction:
    mnemonic: str
    args: tuple[object, ...]
    line: int
    text: str

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class Program:
    instructions: tuple[Instruction, ...]
    labels: dict[str, int]
    filename: str
    data: tuple[int, ...] = (0,) * MEMORY_SIZE
    constants: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.instructions)


class Parser:
    def __init__(self, tokens: list[Token], source: str = "", filename: str = "<fonte>") -> None:
        self.tokens = tokens
        self.source = source
        self.filename = filename
        self.lines = source.split("\n")
        self.instructions: list[Instruction] = []
        self.labels: dict[str, int] = {}
        self.label_lines: dict[str, int] = {}
        self.constants: dict[str, int] = {}
        self.constant_lines: dict[str, int] = {}
        self.data = [0] * MEMORY_SIZE
        self.data_lines: dict[int, int] = {}

    def _error(self, message: str, token: Token | None = None, *, hint: str | None = None,
               line: int | None = None, col: int | None = None) -> NoReturn:
        raise ParseError(
            message,
            filename=self.filename,
            line=token.line if token is not None else line,
            col=token.col if token is not None else col,
            hint=hint,
        )

    def parse(self) -> Program:
        lines = self._collect_lines()
        for tokens in lines:
            if tokens[0].kind is TokenType.DIRECTIVE and tokens[0].value == ".CONST":
                self._parse_constant(tokens)
        for tokens in lines:
            self._parse_line(tokens)
        self._resolve_labels()
        return Program(
            tuple(self.instructions),
            dict(self.labels),
            self.filename,
            tuple(self.data),
            dict(self.constants),
        )

    def _collect_lines(self) -> list[list[Token]]:
        lines: list[list[Token]] = []
        line_tokens: list[Token] = []
        for token in self.tokens:
            if token.kind in (TokenType.NEWLINE, TokenType.EOF):
                if line_tokens:
                    lines.append(line_tokens)
                    line_tokens = []
                if token.kind is TokenType.EOF:
                    break
            else:
                line_tokens.append(token)
        if line_tokens:
            lines.append(line_tokens)
        return lines

    def _parse_constant(self, tokens: list[Token]) -> None:
        first = tokens[0]
        if len(tokens) < 2:
            self._error(".CONST esperava um nome de constante", first)
        name_token = tokens[1]
        if name_token.kind is not TokenType.IDENT:
            self._error(
                f"nome inválido de constante: '{name_token.lexeme}'",
                name_token,
                hint="use um identificador que não seja uma palavra reservada",
            )
        name = str(name_token.value)
        if name in self.constants:
            self._error(
                f"constante '{name}' definida mais de uma vez",
                name_token,
                hint=f"primeira definição na linha {self.constant_lines[name]}",
            )
        if len(tokens) < 3 or tokens[2].kind is not TokenType.COMMA:
            self._error("esperava ',' após o nome da constante", tokens[2] if len(tokens) > 2 else name_token)
        if len(tokens) < 4:
            self._error(".CONST esperava um literal numérico após ','", tokens[2])
        literal = tokens[3]
        if literal.kind is not TokenType.NUMBER:
            self._error(
                ".CONST exige um literal numérico, não uma expressão ou outra constante",
                literal,
            )
        if len(tokens) > 4:
            self._error("operando extra em .CONST", tokens[4])
        value = literal.value
        assert isinstance(value, int)
        if not 0 <= value <= MAX_TICKDELAY:
            self._error(f"constante fora do intervalo 0..{MAX_TICKDELAY}", literal)
        self.constants[name] = value
        self.constant_lines[name] = name_token.line

    def _parse_data(self, tokens: list[Token]) -> None:
        first = tokens[0]
        if len(tokens) < 2:
            self._error(".DATA esperava um endereço e pelo menos um valor", first)
        address = self._parse_operand("address", tokens[1], ".DATA")
        assert isinstance(address, int)
        if len(tokens) < 3:
            self._error(".DATA exige pelo menos um valor após o endereço", tokens[1])

        pos = 2
        while pos < len(tokens):
            if tokens[pos].kind is not TokenType.COMMA:
                self._error("esperava ',' antes do valor de .DATA", tokens[pos])
            comma = tokens[pos]
            pos += 1
            if pos >= len(tokens):
                self._error("vírgula sem operando em .DATA", comma)
            token = tokens[pos]
            value = self._parse_operand("imm", token, ".DATA")
            assert isinstance(value, int)
            if address >= MEMORY_SIZE:
                self._error(
                    f".DATA ultrapassa o limite da memória (endereços 0..{MEMORY_SIZE - 1})",
                    token,
                )
            if address in self.data_lines:
                self._error(
                    f"sobreposição de .DATA no endereço {address}",
                    token,
                    hint=f"primeira inicialização na linha {self.data_lines[address]}",
                )
            self.data[address] = value
            self.data_lines[address] = first.line
            address += 1
            pos += 1

    def _parse_line(self, tokens: list[Token]) -> None:
        first = tokens[0]
        if len(tokens) >= 2 and tokens[1].kind is TokenType.COLON:
            if first.kind is TokenType.INSTRUCTION:
                self._error(
                    f"'{first.lexeme}' é uma instrução reservada e não pode ser rótulo",
                    first,
                    hint="escolha outro nome, como 'loop:' ou 'inicio:'",
                )
            if first.kind is TokenType.REGISTER:
                self._error(f"'{first.lexeme}' é um registrador e não pode ser rótulo", first)
            if first.kind is TokenType.DIRECTIVE:
                self._error(f"'{first.lexeme}' é uma diretiva reservada e não pode ser rótulo", first)
            if first.kind is not TokenType.IDENT:
                self._error(f"nome inválido de rótulo: '{first.lexeme}'", first)
            name = str(first.value)
            if name in self.constants:
                self._error(
                    f"nome '{name}' já definido como constante; não pode ser rótulo",
                    first,
                    hint=f"constante definida na linha {self.constant_lines[name]}",
                )
            if name in self.labels:
                self._error(
                    f"rótulo '{name}' definido mais de uma vez",
                    first,
                    hint=f"primeira definição na linha {self.label_lines[name]}",
                )
            self.labels[name] = len(self.instructions)
            self.label_lines[name] = first.line
            tokens = tokens[2:]
            if not tokens:
                return
            first = tokens[0]
            if first.kind is TokenType.DIRECTIVE:
                self._error(
                    "diretivas .CONST e .DATA não podem aparecer após um rótulo na mesma linha",
                    first,
                    hint="rótulos só podem identificar código; coloque a diretiva em outra linha",
                )
            if len(tokens) >= 2 and tokens[1].kind is TokenType.COLON:
                self._error("apenas um rótulo por linha é permitido", first)

        if first.kind is TokenType.DIRECTIVE:
            if first.value == ".DATA":
                self._parse_data(tokens)
            elif first.value != ".CONST":
                self._error(f"diretiva desconhecida '{first.lexeme}'", first)
            return

        if first.kind is TokenType.IDENT:
            if len(tokens) == 1:
                self._error(
                    f"rótulo '{first.value}' sem ':'",
                    first,
                    hint="escreva 'nome:' antes de uma instrução ou em uma linha isolada",
                )
            self._error(
                f"instrução desconhecida '{first.lexeme}'",
                first,
                hint="mnemônicos válidos: " + ", ".join(MNEMONICS)
                     + ". Rótulos devem terminar com ':'",
            )
        if first.kind is not TokenType.INSTRUCTION:
            self._error(
                f"esperava uma instrução, diretiva ou rótulo, encontrei '{first.lexeme}'",
                first,
            )

        mnemonic = str(first.value)
        args = self._parse_operands(tokens, mnemonic, SIGNATURES[mnemonic])
        text = self._line_text(first.line, first.col)
        self.instructions.append(Instruction(mnemonic, tuple(args), first.line, text))

    def _line_text(self, line: int, col: int = 1) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1][col - 1:].split("#", 1)[0].split(";", 1)[0].strip()
        return ""

    def _parse_operands(self, tokens: list[Token], mnemonic: str, spec: tuple[str, ...]) -> list[object]:
        first = tokens[0]
        pos = 1
        args: list[object] = []
        if not spec:
            if pos < len(tokens):
                self._error(f"{mnemonic} não aceita operandos", tokens[pos])
            return args

        for index, kind in enumerate(spec):
            if index > 0:
                if pos >= len(tokens) or tokens[pos].kind is not TokenType.COMMA:
                    token = tokens[pos] if pos < len(tokens) else first
                    self._error(f"esperava ',' antes do operando {index + 1} de {mnemonic}", token)
                pos += 1
            if pos >= len(tokens):
                self._error(
                    f"{mnemonic} esperava {len(spec)} operando(s), mas recebeu {index}",
                    first,
                )
            token = tokens[pos]
            if kind == "address" and token.kind is TokenType.LBRACKET:
                address, pos = self._parse_memory_ref(tokens, pos, mnemonic)
                args.append(address)
            else:
                args.append(self._parse_operand(kind, token, mnemonic))
                pos += 1

        if pos < len(tokens):
            extra = tokens[pos]
            if extra.kind is TokenType.COMMA:
                self._error("vírgula sem operando", extra)
            self._error(f"operando extra em {mnemonic}: '{extra.lexeme}'", extra)
        return args

    def _parse_memory_ref(self, tokens: list[Token], pos: int, mnemonic: str) -> tuple[MemoryRef, int]:
        opening = tokens[pos]
        pos += 1
        if pos >= len(tokens):
            self._error(f"{mnemonic} esperava um registrador após '['", opening)
        low = self._parse_operand("reg", tokens[pos], mnemonic)
        assert isinstance(low, Register)
        high = None
        pos += 1
        if pos < len(tokens) and tokens[pos].kind is TokenType.COLON:
            high = low
            colon = tokens[pos]
            pos += 1
            if pos >= len(tokens):
                self._error(f"{mnemonic} esperava um registrador após ':'", colon)
            low = self._parse_operand("reg", tokens[pos], mnemonic)
            assert isinstance(low, Register)
            pos += 1
        if pos >= len(tokens) or tokens[pos].kind is not TokenType.RBRACKET:
            self._error(
                f"esperava ']' no endereço de {mnemonic}",
                tokens[pos] if pos < len(tokens) else opening,
            )
        return MemoryRef(high, low), pos + 1

    def _parse_operand(self, kind: str, token: Token, mnemonic: str) -> object:
        if kind in ("reg", "dust") or (kind == "value" and token.kind is TokenType.REGISTER):
            if token.kind is not TokenType.REGISTER:
                self._error(
                    f"{mnemonic} esperava um registrador, encontrei '{token.lexeme}'",
                    token,
                    hint="use DUST0..DUST7, TORCH, COMPARATOR ou REPEATER",
                )
            register = token.value
            assert isinstance(register, Register)
            if kind == "dust" and register not in DUST_REGISTERS:
                self._error(f"{mnemonic} exige um fio DUST0..DUST7, não {register.value}", token)
            return register

        if kind in ("imm", "value", "delay", "address"):
            if token.kind is TokenType.IDENT:
                name = str(token.value)
                if name not in self.constants:
                    self._error(f"constante '{name}' não definida", token)
                value = self.constants[name]
            elif token.kind is TokenType.NUMBER:
                value = token.value
                assert isinstance(value, int)
            else:
                expected = {
                    "imm": "um literal numérico ou constante 0..15",
                    "value": "um registrador, literal numérico ou constante 0..15",
                    "delay": "um número de ticks ou constante",
                    "address": f"um endereço literal ou constante 0..{MEMORY_SIZE - 1}",
                }[kind]
                self._error(f"{mnemonic} esperava {expected}, encontrei '{token.lexeme}'", token)

            if kind in ("imm", "value") and not MIN_POWER <= value <= MAX_POWER:
                self._error(
                    f"potência {token.lexeme} fora do intervalo {MIN_POWER}..{MAX_POWER}",
                    token,
                )
            if kind == "delay" and not 1 <= value <= MAX_TICKDELAY:
                self._error(f"TICKDELAY deve ficar entre 1 e {MAX_TICKDELAY} ticks", token)
            if kind == "address" and not 0 <= value < MEMORY_SIZE:
                self._error(f"endereço {token.lexeme} fora do intervalo 0..{MEMORY_SIZE - 1}", token)
            return value

        if kind == "label":
            if token.kind is not TokenType.IDENT:
                self._error(
                    f"{mnemonic} esperava um rótulo, encontrei '{token.lexeme}'",
                    token,
                    hint="rótulos não podem coincidir com palavras reservadas",
                )
            name = str(token.value)
            if name in self.constants:
                self._error(f"'{name}' é uma constante, não um rótulo", token)
            return LabelRef(name, token.line, token.col)

        raise AssertionError(f"tipo de operando desconhecido: {kind}")  # pragma: no cover

    def _resolve_labels(self) -> None:
        for index, instruction in enumerate(self.instructions):
            args = list(instruction.args)
            for pos, ref in enumerate(args):
                if not isinstance(ref, LabelRef):
                    continue
                if ref.name not in self.labels:
                    self._error(
                        f"rótulo '{ref.name}' não definido",
                        line=ref.line,
                        col=ref.col,
                        hint="defina o rótulo como 'nome:' antes de uma instrução",
                    )
                args[pos] = self.labels[ref.name]
            self.instructions[index] = replace(instruction, args=tuple(args))


def parse(tokens: list[Token], source: str = "", filename: str = "<fonte>") -> Program:
    """Analisa tokens já produzidos pelo lexer."""
    return Parser(tokens, source, filename).parse()


def compile_source(source: str, filename: str = "<fonte>") -> Program:
    """Atalho léxico + sintático para código em memória."""
    return parse(tokenize(source, filename), source, filename)
