"""Definição da ISA RedAsm v2.0 (Minecraft Edition).

Este módulo é a fonte única de verdade sobre registradores, limites e
mnemônicos. Lexer, parser e VM importam daqui para evitar divergências.
"""
from __future__ import annotations

from enum import Enum

# ---------------------------------------------------------------------------
# Limites do modelo elétrico
# ---------------------------------------------------------------------------
MIN_POWER = 0
MAX_POWER = 15          # fio de redstone: 4 bits (0x0..0xF)
MAX_TICKDELAY = 10_000  # trava de segurança contra delays absurdos


class Register(Enum):
    """Componentes endereçáveis pela VM.

    DUST0..DUST7 são fios genéricos. TORCH, COMPARATOR e REPEATER têm
    semântica de escrita própria (ver ``vm.py`` e a especificação).
    """

    DUST0 = "DUST0"
    DUST1 = "DUST1"
    DUST2 = "DUST2"
    DUST3 = "DUST3"
    DUST4 = "DUST4"
    DUST5 = "DUST5"
    DUST6 = "DUST6"
    DUST7 = "DUST7"
    TORCH = "TORCH"
    COMPARATOR = "COMPARATOR"
    REPEATER = "REPEATER"


DUST_REGISTERS: tuple[Register, ...] = (
    Register.DUST0,
    Register.DUST1,
    Register.DUST2,
    Register.DUST3,
    Register.DUST4,
    Register.DUST5,
    Register.DUST6,
    Register.DUST7,
)

COMPONENT_REGISTERS: tuple[Register, ...] = (
    Register.TORCH,
    Register.COMPARATOR,
    Register.REPEATER,
)

ALL_REGISTERS: tuple[Register, ...] = DUST_REGISTERS + COMPONENT_REGISTERS

# Nomes canônicos em maiúsculas; o lexer normaliza para caixa alta.
REGISTER_BY_NAME: dict[str, Register] = {reg.value: reg for reg in ALL_REGISTERS}

MEMORY_SIZE = 256
PORT_COUNT = 16
STACK_LIMIT = 64
CALL_STACK_LIMIT = 64
DIRECTIVES = frozenset({".CONST", ".DATA"})

SIGNATURES: dict[str, tuple[str, ...]] = {
    "POWER": ("dust", "imm"),
    "WIRE": ("reg", "value"),
    "INVERT": ("reg", "value"),
    "COMPARE": ("reg", "value", "value"),
    "SUBTRACT": ("reg", "value", "value"),
    "TICKDELAY": ("delay",),
    "FADE": ("dust",),
    "JUMP": ("label",),
    "JNZ": ("reg", "label"),
    "JZ": ("reg", "label"),
    "HALT": (),
    "NOP": (),
    "ADD": ("reg", "value", "value"),
    "MUL": ("reg", "value", "value"),
    "DIV": ("reg", "value", "value"),
    "MOD": ("reg", "value", "value"),
    "INC": ("dust",),
    "AND": ("reg", "value", "value"),
    "OR": ("reg", "value", "value"),
    "XOR": ("reg", "value", "value"),
    "NOT": ("reg", "value"),
    "SHL": ("reg", "value", "value"),
    "SHR": ("reg", "value", "value"),
    "JE": ("value", "value", "label"),
    "JNE": ("value", "value", "label"),
    "JLT": ("value", "value", "label"),
    "JLE": ("value", "value", "label"),
    "JGT": ("value", "value", "label"),
    "JGE": ("value", "value", "label"),
    "LOAD": ("reg", "address"),
    "STORE": ("address", "value"),
    "PUSH": ("value",),
    "POP": ("reg",),
    "CALL": ("label",),
    "RET": (),
    "IN": ("reg", "value"),
    "OUT": ("value", "value"),
    "PRINT": ("value",),
}
MNEMONICS: tuple[str, ...] = tuple(SIGNATURES)
MNEMONIC_SET: frozenset[str] = frozenset(MNEMONICS)
