"""Interface de linha de comando e renderizador de terminal do RedAsm.

Uso:
    python redvm.py examples/clock.rasm
    python redvm.py examples/fader.rasm --ticks 40 --tick-rate 60
    python redvm.py examples/clock.rasm --max-speed --ticks 16

O renderizador desenha os 11 componentes como barras de 15 células. Quando a
saída é um terminal interativo, o frame é redesenhado no lugar e as barras
recebem cor por intensidade; em pipes/CI a saída é texto puro.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from errors import RedAsmError
from isa import ALL_REGISTERS, DUST_REGISTERS, MAX_POWER, MEMORY_SIZE, MIN_POWER, PORT_COUNT, Register
from lexer import tokenize
from parser import Instruction, parse
from vm import Frame, RedVM

if TYPE_CHECKING:
    from vm import OutputEvent

VERSION = "RedAsm v2.0"
CLEAR_SCREEN = "\x1b[2J\x1b[H"
RESET = "\x1b[0m"
DIM = "\x1b[2m"
ANSI_GREY = "\x1b[90m"
ANSI_GREEN = "\x1b[32m"
ANSI_YELLOW = "\x1b[33m"
ANSI_RED = "\x1b[91m"

UNICODE_GLYPHS = {
    "fill": "█",
    "empty": "░",
    "bar": "─",
    "sep": "│",
    "dot": "·",
}
ASCII_GLYPHS = {
    "fill": "#",
    "empty": "-",
    "bar": "-",
    "sep": "|",
    "dot": ".",
}


# ---------------------------------------------------------------------------
# Renderização
# ---------------------------------------------------------------------------
def _power_color(level: int) -> str:
    if level == 0:
        return ANSI_GREY
    if level <= 5:
        return ANSI_GREEN
    if level <= 10:
        return ANSI_YELLOW
    return ANSI_RED


def _bar(level: int, glyphs: dict[str, str]) -> str:
    return glyphs["fill"] * level + glyphs["empty"] * (MAX_POWER - level)


def _cell(register: Register, frame: Frame, *, color: bool, glyphs: dict[str, str]) -> str:
    level = frame.registers[register]
    bar = _bar(level, glyphs)
    if color:
        bar = _power_color(level) + bar + RESET
    return f"{register.value:<10} [{level:02d}]: {bar}"


def render_frame(
    frame: Frame,
    *,
    source_name: str,
    color: bool = False,
    ascii_mode: bool = False,
    final: bool = False,
) -> None:
    """Desenha um frame completo no stdout."""
    glyphs = ASCII_GLYPHS if ascii_mode else UNICODE_GLYPHS
    label = "FINAL" if final else "TICK "

    print(f"{label} {frame.tick:04d} {glyphs['sep']} PC {frame.pc:04d} {glyphs['sep']} {source_name}")
    print(glyphs["bar"] * 69)

    for left, right in (
        (DUST_REGISTERS[0], DUST_REGISTERS[1]),
        (DUST_REGISTERS[2], DUST_REGISTERS[3]),
        (DUST_REGISTERS[4], DUST_REGISTERS[5]),
        (DUST_REGISTERS[6], DUST_REGISTERS[7]),
    ):
        print("  " + _cell(left, frame, color=color, glyphs=glyphs)
              + "   " + _cell(right, frame, color=color, glyphs=glyphs))

    repeater = _cell(Register.REPEATER, frame, color=color, glyphs=glyphs)
    if frame.pending:
        repeater += f" (+{frame.pending})"
    print("  " + _cell(Register.TORCH, frame, color=color, glyphs=glyphs)
          + "   " + repeater)

    comparator = _cell(Register.COMPARATOR, frame, color=color, glyphs=glyphs)
    if frame.comparator is not None:
        main, side, mode = frame.comparator
        comparator += f"   A={main:02d} B={side:02d} {mode}"
    print("  " + comparator)

    last = frame.last if frame.last is not None else "(nenhuma instrução executada)"
    print(f"  {glyphs['dot']} último: {last}")


def _signature(frame: Frame) -> tuple[object, ...]:
    return (
        tuple(frame.registers[reg] for reg in ALL_REGISTERS),
        frame.pending,
        frame.comparator,
        frame.pc,
        frame.last,
        frame.memory,
        frame.stack,
        frame.call_depth,
        frame.inputs,
        frame.outputs,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_input(value: str) -> tuple[int, int]:
    """Lê PORT=VALUE em decimal, hexadecimal ou binário."""
    try:
        port_text, level_text = value.split("=")
        numbers = []
        for text in (port_text, level_text):
            text = text.strip()
            base = 0 if text.lstrip("+-").lower().startswith(("0x", "0b")) else 10
            numbers.append(int(text, base))
        port, level = numbers
    except ValueError:
        raise argparse.ArgumentTypeError(
            "use PORT=VALUE com inteiros decimais, hexadecimais (0x) ou binários (0b)"
        ) from None

    if not 0 <= port < PORT_COUNT:
        raise argparse.ArgumentTypeError(f"a porta deve estar entre 0 e {PORT_COUNT - 1}")
    if not MIN_POWER <= level <= MAX_POWER:
        raise argparse.ArgumentTypeError(f"o valor deve estar entre {MIN_POWER} e {MAX_POWER}")
    return port, level


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redvm",
        description="RedVM — interpretador da linguagem RedAsm (Redstone Assembly).",
        epilog=(
            "exemplos:\n"
            "  python redvm.py examples/clock.rasm\n"
            "  python redvm.py examples/fader.rasm --ticks 40 --tick-rate 60\n"
            "  python redvm.py examples/clock.rasm --max-speed --ticks 16\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("arquivo", help="arquivo .rasm a executar")
    parser.add_argument(
        "--tick-rate",
        type=int,
        default=100,
        metavar="MS",
        help="duração de cada tick em milissegundos (padrão: 100; 0 = máxima velocidade)",
    )
    parser.add_argument(
        "--max-speed",
        action="store_true",
        help="executa o mais rápido possível e mostra apenas o frame final",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=64,
        metavar="N",
        help="número máximo de ticks a simular (padrão: 64; 0 = sem limite)",
    )
    parser.add_argument("--trace", action="store_true", help="mostra cada instrução executada")
    parser.add_argument("--check", action="store_true", help="compila e valida sem executar")
    parser.add_argument(
        "--input",
        type=_parse_input,
        action="append",
        default=[],
        metavar="PORT=VALUE",
        help="define uma entrada de 0 a 15 (decimal, 0x ou 0b); repetível, a última vence",
    )
    parser.add_argument(
        "--output-only",
        action="store_true",
        help="imprime apenas os valores decimais de PRINT, sem pausas",
    )
    parser.add_argument(
        "--dump-memory",
        action="store_true",
        help=f"mostra as {MEMORY_SIZE} células da RAM em hexadecimal ao final",
    )
    parser.add_argument("--no-color", action="store_true", help="desativa cores ANSI")
    parser.add_argument("--no-clear", action="store_true", help="não limpa a tela entre frames")
    parser.add_argument("--ascii", action="store_true", help="barras ASCII (#/-) em vez de blocos")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def _reconfigure_stdio() -> None:
    """Garante UTF-8 para os glifos de barra mesmo com stdout redirecionado."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.tick_rate < 0:
        parser.error("--tick-rate não pode ser negativo")
    if args.ticks < 0:
        parser.error("--ticks não pode ser negativo")
    if args.output_only and (args.trace or args.check or args.dump_memory):
        parser.error("--output-only não pode ser usado com --trace, --check ou --dump-memory")

    path = Path(args.arquivo)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        detail = getattr(exc, "strerror", None) or str(exc)
        print(f"redvm: não foi possível ler '{path}': {detail}", file=sys.stderr)
        return 1

    try:
        tokens = tokenize(source, str(path))
        program = parse(tokens, source, str(path))
    except RedAsmError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.check:
        print(f"Compilação válida: {len(program.instructions)} instruções, "
              f"{len(program.constants)} constantes.")
        return 0

    if not program.instructions:
        print(f"redvm: '{path}' não contém instruções", file=sys.stderr)
        return 1

    max_speed = args.output_only or args.max_speed or args.tick_rate == 0
    tick_rate = 0 if max_speed else args.tick_rate
    max_ticks = args.ticks if args.ticks > 0 else None
    color = sys.stdout.isatty() and not args.no_color
    has_print = any(instruction.mnemonic == "PRINT" for instruction in program.instructions)
    clear = (sys.stdout.isatty() and not args.no_clear and not max_speed
             and not args.trace and not has_print)
    source_name = path.name
    glyphs = ASCII_GLYPHS if args.ascii else UNICODE_GLYPHS

    last_signature: tuple[object, ...] | None = None

    def on_tick(frame: Frame) -> None:
        nonlocal last_signature
        last_signature = _signature(frame)
        if max_speed:
            return
        if clear:
            sys.stdout.write(CLEAR_SCREEN)
        render_frame(
            frame,
            source_name=source_name,
            color=color,
            ascii_mode=args.ascii,
            final=False,
        )
        if not clear:
            print()
        sys.stdout.flush()
        if tick_rate:
            time.sleep(tick_rate / 1000.0)

    def on_execute(tick: int, pc: int, instruction: Instruction) -> None:
        if args.trace:
            prefix = DIM if color else ""
            suffix = RESET if color else ""
            print(f"{prefix}  {glyphs['dot']} t={tick:04d} pc={pc:04d}  {instruction}{suffix}")

    def on_output(event: OutputEvent) -> None:
        if args.output_only:
            print(f"{event.value:d}", flush=True)
        else:
            print(f"OUT t={event.tick:04d} value={event.value:d}", flush=True)

    vm = RedVM(
        program,
        max_ticks=max_ticks,
        on_tick=None if args.output_only else on_tick,
        on_execute=on_execute if args.trace else None,
        on_output=on_output,
    )

    try:
        for port, level in dict(args.input).items():
            vm.set_input(port, level)
        result = vm.run()
    except RedAsmError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrompido pelo usuário", file=sys.stderr)
        return 130

    if args.output_only:
        return 0

    final_frame = vm.snapshot()
    if max_speed or _signature(final_frame) != last_signature:
        if clear:
            sys.stdout.write(CLEAR_SCREEN)
        render_frame(
            final_frame,
            source_name=source_name,
            color=color,
            ascii_mode=args.ascii,
            final=True,
        )
        print()

    noun = "instrução executada" if result.instructions == 1 else "instruções executadas"
    print(f"{glyphs['bar'] * 3} {result.reason} {glyphs['dot']} "
          f"tick {result.tick} {glyphs['dot']} {result.instructions} {noun}")
    if args.dump_memory:
        print(f"RAM ({MEMORY_SIZE} células, hexadecimal):")
        for start in range(0, MEMORY_SIZE, 16):
            print(" ".join(f"{value:02X}" for value in final_frame.memory[start:start + 16]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
