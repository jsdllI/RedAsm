"""RedVM — motor de execução do RedAsm orientado a ticks.

Modelo temporal
---------------
* Instruções elétricas (POWER, WIRE, COMPARE, ...) são instantâneas dentro
  do tick corrente.
* ``TICKDELAY N`` encerra o tick corrente, avança o relógio N vezes e emite
  um frame ao fim de cada tick; a execução retoma no tick final.
* Cada tick tem um orçamento de 100.000 instruções para detectar laços sem
  ``TICKDELAY`` sem travar o processo.
* O REPEATER agenda escritas para o próximo tick (comportamento de delay).

A VM é determinística para a mesma sequência de entradas externas.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Callable

from errors import VMError
from isa import CALL_STACK_LIMIT, MAX_POWER, MIN_POWER, PORT_COUNT, STACK_LIMIT, Register
from parser import Instruction, MemoryRef, Program

MAX_INSTRUCTIONS_PER_TICK = 100_000

BINARY_OPERATIONS = {
    "ADD": operator.add,
    "MUL": operator.mul,
    "DIV": operator.floordiv,
    "MOD": operator.mod,
    "AND": operator.and_,
    "OR": operator.or_,
    "XOR": operator.xor,
    "SHL": operator.lshift,
    "SHR": operator.rshift,
}
RELATIONS = {
    "JE": operator.eq,
    "JNE": operator.ne,
    "JLT": operator.lt,
    "JLE": operator.le,
    "JGT": operator.gt,
    "JGE": operator.ge,
}


@dataclass(frozen=True)
class Frame:
    """Fotografia do estado ao fim de um tick (ou no encerramento)."""

    tick: int
    pc: int
    registers: dict[Register, int]
    pending: int
    comparator: tuple[int, int, str] | None
    last: str | None
    memory: tuple[int, ...] = ()
    stack: tuple[int, ...] = ()
    call_depth: int = 0
    inputs: tuple[int, ...] = ()
    outputs: tuple[int, ...] = ()


@dataclass(frozen=True)
class OutputEvent:
    tick: int
    value: int


@dataclass(frozen=True)
class RunResult:
    tick: int
    halted: bool
    reason: str
    instructions: int


@dataclass(frozen=True)
class StepResult:
    """Resultado de um único tick lógico.

    ``ticks_advanced`` é 0 quando a VM já terminou (HALT, fim do programa ou
    limite de ticks) e 1 quando o relógio avançou. ``ended`` indica que não há
    mais execução a fazer.
    """

    frame: Frame
    ticks_advanced: int
    ended: bool


TickHook = Callable[[Frame], None]
ExecuteHook = Callable[[int, int, Instruction], None]


class RedVM:
    def __init__(
        self,
        program: Program,
        *,
        max_ticks: int | None = None,
        on_tick: TickHook | None = None,
        on_execute: ExecuteHook | None = None,
        on_output: Callable[[OutputEvent], None] | None = None,
    ) -> None:
        if max_ticks is not None and max_ticks < 0:
            raise ValueError("max_ticks não pode ser negativo")

        self.program = program
        self.registers: dict[Register, int] = {reg: 0 for reg in Register}
        self.memory = list(program.data)
        self.stack: list[int] = []
        self.call_stack: list[int] = []
        self.inputs = [0] * PORT_COUNT
        self.outputs = [0] * PORT_COUNT
        self.tick = 0
        self.pc = 0
        self.halted = False

        self.max_ticks = max_ticks
        self.on_tick = on_tick
        self.on_execute = on_execute
        self.on_output = on_output

        self._pending: list[int] = []          # escritas agendadas no REPEATER
        self._comparator: tuple[int, int, str] | None = None
        self._last: str | None = None
        self._reason: str | None = None
        self._stop = False
        self._running = False
        self._mode: str | None = None
        self._step_delay = 0
        self._step_needs_execution = True
        self._instruction_count = 0

    @property
    def reason(self) -> str | None:
        """Motivo do encerramento, ou ``None`` enquanto a VM está ativa."""
        return self._reason

    # ------------------------------------------------------------------ API
    def run(self) -> RunResult:
        """Executa até HALT, fim do programa ou limite de ticks."""
        if self._running:
            raise VMError(
                "a VM já está em execução",
                filename=self.program.filename,
                hint="crie outra instância de RedVM para uma nova execução",
            )
        if self._mode == "step":
            raise VMError(
                "run() não pode ser usado depois de step_tick()",
                filename=self.program.filename,
                hint="use apenas step_tick() em uma VM controlada pelo engine",
            )

        self._mode = "run"
        self._running = True
        try:
            while not self._stop:
                if self.max_ticks is not None and self.tick >= self.max_ticks:
                    self._stop = True
                    self._reason = f"limite de {self.max_ticks} ticks atingido"
                    break
                self._burst()
        finally:
            self._running = False

        return RunResult(
            tick=self.tick,
            halted=self.halted,
            reason=self._reason or "fim do programa",
            instructions=self._instruction_count,
        )

    def inject(self, register: Register, level: int) -> None:
        """Injeta um sinal externo (entrada) respeitando as regras elétricas.

        A escrita é imediata dentro do tick corrente. O engine deve chamar
        ``inject`` antes de ``step_tick`` para que o programa leia o valor
        durante o próximo tick executado.
        """
        if not isinstance(register, Register):
            raise TypeError("register deve ser um Register")
        if type(level) is not int:
            raise TypeError("level deve ser inteiro")
        self._write(register, level)

    def set_input(self, port: int, level: int) -> None:
        if type(port) is not int or type(level) is not int:
            raise TypeError("porta e nível devem ser inteiros")
        if not 0 <= port < PORT_COUNT:
            raise ValueError(f"porta deve ficar entre 0 e {PORT_COUNT - 1}")
        if not MIN_POWER <= level <= MAX_POWER:
            raise ValueError(f"nível deve ficar entre {MIN_POWER} e {MAX_POWER}")
        self.inputs[port] = level

    def step_tick(self) -> StepResult:
        """Avança a VM em exatamente um tick lógico.

        O frame devolvido descreve o estado **do tick que acaba de terminar**
        (o mesmo instante que ``on_tick`` receberia em ``run``). Quando o
        programa chega a HALT/fim, ``ended`` é ``True`` e ``ticks_advanced`` é
        0; o frame ainda assim reflete o estado final.

        ``on_tick`` não é disparado neste modo: o chamador já recebe o frame.
        ``on_execute`` continua funcionando.
        """
        if self._running:
            raise VMError(
                "a VM já está em execução",
                filename=self.program.filename,
                hint="não chame step_tick() durante run()",
            )

        if self._mode == "run":
            raise VMError(
                "step_tick() não pode ser usado depois de run()",
                filename=self.program.filename,
            )
        self._mode = "step"
        self._running = True
        try:
            if self._stop:
                return StepResult(self.snapshot(), 0, True)

            if self.max_ticks is not None and self.tick >= self.max_ticks:
                self._stop = True
                self._reason = f"limite de {self.max_ticks} ticks atingido"
                return StepResult(self.snapshot(), 0, True)

            if self._step_needs_execution:
                delay = self._execute_burst()
                if self._stop:
                    return StepResult(self.snapshot(), 0, True)
                self._step_delay = delay
                self._step_needs_execution = False

            frame = self.snapshot()
            self.tick += 1
            self._commit_repeater()

            self._step_delay -= 1
            if self._step_delay <= 0:
                self._step_needs_execution = True

            return StepResult(frame, 1, False)
        finally:
            self._running = False

    def snapshot(self) -> Frame:
        """Cópia independente do estado atual para inspeção."""
        return Frame(
            tick=self.tick,
            pc=self.pc,
            registers=dict(self.registers),
            pending=len(self._pending),
            comparator=self._comparator,
            last=self._last,
            memory=tuple(self.memory),
            stack=tuple(self.stack),
            call_depth=len(self.call_stack),
            inputs=tuple(self.inputs),
            outputs=tuple(self.outputs),
        )

    # ------------------------------------------------------------ execução
    def _burst(self) -> None:
        """Executa um tick e, se houver TICKDELAY, avança o relógio."""
        delay = self._execute_burst()
        if delay and not self._stop:
            self._wait(delay)

    def _execute_burst(self) -> int:
        """Executa instruções até TICKDELAY, HALT, fim ou orçamento.

        Devolve o número de ticks pedido por TICKDELAY (0 se a VM parou).
        """
        budget = MAX_INSTRUCTIONS_PER_TICK

        while not self._stop:
            if self.pc >= len(self.program.instructions):
                self._stop = True
                self._reason = "fim do programa"
                return 0

            instruction = self.program.instructions[self.pc]
            if self.on_execute is not None:
                self.on_execute(self.tick, self.pc, instruction)

            self._last = instruction.text
            self._instruction_count += 1

            if instruction.mnemonic == "TICKDELAY":
                self.pc += 1
                return int(instruction.args[0])  # type: ignore[arg-type]

            self.pc += 1
            self._execute(instruction)

            if self._stop:
                return 0

            budget -= 1
            if budget <= 0:
                raise VMError(
                    "orçamento de instruções do tick esgotado "
                    f"({MAX_INSTRUCTIONS_PER_TICK})",
                    filename=self.program.filename,
                    line=instruction.line,
                    hint="há um laço sem TICKDELAY; insira um tick de espera",
                )

        return 0

    def _wait(self, ticks: int) -> None:
        """Avança o relógio, emitindo um frame ao fim de cada tick."""
        for _ in range(ticks):
            if self._stop:
                return

            if self.max_ticks is not None and self.tick >= self.max_ticks:
                self._stop = True
                self._reason = f"limite de {self.max_ticks} ticks atingido"
                return

            if self.on_tick is not None:
                self.on_tick(self.snapshot())

            self.tick += 1
            self._commit_repeater()

    def _commit_repeater(self) -> None:
        """Aplica no REPEATER a última escrita agendada no tick anterior."""
        if self._pending:
            self.registers[Register.REPEATER] = self._pending[-1]
            self._pending.clear()

    def _execute(self, instruction: Instruction) -> None:
        mnemonic = instruction.mnemonic
        args = instruction.args

        if mnemonic == "POWER":
            self._write(args[0], args[1])

        elif mnemonic == "WIRE":
            self._write(args[0], self._value(args[1]))

        elif mnemonic == "INVERT":
            self._write(args[0], 0 if self._value(args[1]) else MAX_POWER)

        elif mnemonic == "NOT":
            self._write(args[0], self._value(args[1]) ^ MAX_POWER)

        elif mnemonic in ("COMPARE", "SUBTRACT"):
            main = self._value(args[1])
            side = self._value(args[2])
            level = (main if main >= side else 0) if mnemonic == "COMPARE" else max(0, main - side)
            self._write(args[0], level)
            if args[0] is Register.COMPARATOR:
                self._comparator = (main, side, mnemonic)

        elif mnemonic in BINARY_OPERATIONS:
            left = self._value(args[1])
            right = self._value(args[2])
            if mnemonic in ("DIV", "MOD") and right == 0:
                self._fail(instruction, f"{mnemonic}: divisão por zero")
            level = BINARY_OPERATIONS[mnemonic](left, right)
            if mnemonic == "SHL":
                level &= MAX_POWER
            self._write(args[0], level)

        elif mnemonic in ("FADE", "INC"):
            level = self._value(args[0]) + (1 if mnemonic == "INC" else -1)
            self._write(args[0], level)

        elif mnemonic == "JUMP":
            self.pc = self._value(args[0])

        elif mnemonic in ("JNZ", "JZ"):
            nonzero = self._value(args[0]) != 0
            if nonzero == (mnemonic == "JNZ"):
                self.pc = self._value(args[1])

        elif mnemonic in RELATIONS:
            if RELATIONS[mnemonic](self._value(args[0]), self._value(args[1])):
                self.pc = self._value(args[2])

        elif mnemonic == "LOAD":
            self._write(args[0], self.memory[self._address(args[1])])

        elif mnemonic == "STORE":
            address = self._address(args[0])
            self.memory[address] = self._value(args[1])

        elif mnemonic == "PUSH":
            if len(self.stack) >= STACK_LIMIT:
                self._fail(instruction, f"pilha de dados excedeu {STACK_LIMIT} valores")
            self.stack.append(self._value(args[0]))

        elif mnemonic == "POP":
            if not self.stack:
                self._fail(instruction, "POP: pilha de dados vazia")
            self._write(args[0], self.stack.pop())

        elif mnemonic == "CALL":
            if len(self.call_stack) >= CALL_STACK_LIMIT:
                self._fail(instruction, f"pilha de chamadas excedeu {CALL_STACK_LIMIT} retornos")
            self.call_stack.append(self.pc)
            self.pc = self._value(args[0])

        elif mnemonic == "RET":
            if not self.call_stack:
                self._fail(instruction, "RET: pilha de chamadas vazia")
            self.pc = self.call_stack.pop()

        elif mnemonic == "IN":
            self._write(args[0], self.inputs[self._value(args[1])])

        elif mnemonic == "OUT":
            port = self._value(args[0])
            self.outputs[port] = self._value(args[1])

        elif mnemonic == "PRINT":
            if self.on_output is not None:
                self.on_output(OutputEvent(self.tick, self._value(args[0])))

        elif mnemonic == "NOP":
            pass

        elif mnemonic == "HALT":
            self.halted = True
            self._stop = True
            self._reason = "HALT"

        else:
            self._fail(instruction, f"instrução desconhecida em tempo de execução: {mnemonic}")

    def _fail(self, instruction: Instruction, message: str) -> None:
        raise VMError(message, filename=self.program.filename, line=instruction.line)

    def _value(self, operand: object) -> int:
        if isinstance(operand, Register):
            return self.registers[operand]
        assert isinstance(operand, int)
        return operand

    def _address(self, operand: object) -> int:
        if isinstance(operand, MemoryRef):
            high = self.registers[operand.high] if operand.high is not None else 0
            return high * 16 + self.registers[operand.low]
        assert isinstance(operand, int)
        return operand

    def _write(self, register: object, value: object) -> None:
        """Escreve respeitando o transform nativo do componente de destino."""
        assert isinstance(register, Register)
        level = int(value)  # type: ignore[arg-type]

        # Restrição estrita do modelo elétrico (0..15).
        if level < MIN_POWER:
            level = MIN_POWER
        elif level > MAX_POWER:
            level = MAX_POWER

        # Repeater: a escrita só vale no próximo tick (last-write-wins).
        if register is Register.REPEATER:
            self._pending.append(level)
            return

        # Tocha: toda entrada é invertida na saída.
        if register is Register.TORCH:
            level = 0 if level > 0 else MAX_POWER
        elif register is Register.COMPARATOR:
            self._comparator = None

        self.registers[register] = level
