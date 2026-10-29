"""Cobertura das operações, montagem, memória e E/S do RedAsm v2."""
from __future__ import annotations

import io
import operator
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from errors import LexError, ParseError, VMError  # noqa: E402
from isa import MNEMONICS, Register  # noqa: E402
from lexer import TokenType, tokenize  # noqa: E402
from parser import compile_source  # noqa: E402
from vm import OutputEvent, RedVM  # noqa: E402

R = Register
SOURCE_ERRORS = (LexError, ParseError)

# Oráculos independentes das tabelas e dos helpers internos da VM.
BINARY_EXPECTED = {
    "ADD": lambda a, b: min(15, a + b),
    "MUL": lambda a, b: min(15, a * b),
    "SUBTRACT": lambda a, b: max(0, a - b),
    "COMPARE": lambda a, b: a if a >= b else 0,
    "DIV": lambda a, b: a // b,
    "MOD": lambda a, b: a % b,
    "AND": lambda a, b: a & b,
    "OR": lambda a, b: a | b,
    "XOR": lambda a, b: a ^ b,
    "SHL": lambda a, b: (a << b) & 15,
    "SHR": lambda a, b: a >> b,
}
RELATION_EXPECTED = {
    "JE": operator.eq,
    "JNE": operator.ne,
    "JLT": operator.lt,
    "JLE": operator.le,
    "JGT": operator.gt,
    "JGE": operator.ge,
}


def execute(source: str, inputs: dict[int, int] | None = None):
    events = []
    frames = []
    vm = RedVM(
        compile_source(source, "v2.rasm"),
        on_output=events.append,
        on_tick=frames.append,
    )
    for port, value in (inputs or {}).items():
        vm.set_input(port, value)
    result = vm.run()
    return vm, result, frames, events


class LexerV2Tests(unittest.TestCase):
    def test_decimal_hex_binary_preserve_locations_and_comments(self):
        source = "; comentário @ ignorado\r\nPRINT 0b1111 ; fim\r\nPRINT 0xA # fim\nPRINT 007"
        tokens = tokenize(source, "numeros.rasm")
        numbers = [t for t in tokens if t.kind is TokenType.NUMBER]
        self.assertEqual([t.value for t in numbers], [15, 10, 7])
        self.assertEqual([t.lexeme for t in numbers], ["0b1111", "0xA", "007"])
        self.assertEqual([(t.line, t.col) for t in numbers], [(2, 7), (3, 7), (4, 7)])
        self.assertEqual(tokens[-1].kind, TokenType.EOF)

    def test_directives_brackets_and_identifier_case(self):
        tokens = tokenize(".const Mixed, 0B1010\nload dust0, [ToRcH:repeater]\nPRINT Mixed\nPRINT mixed")
        self.assertEqual(tokens[0].value, ".CONST")
        self.assertEqual([t.value for t in tokens if t.kind is TokenType.IDENT],
                         ["Mixed", "Mixed", "mixed"])
        self.assertEqual([t.value for t in tokens if t.kind is TokenType.REGISTER],
                         [R.DUST0, R.TORCH, R.REPEATER])
        self.assertIn(TokenType.LBRACKET, [t.kind for t in tokens])
        self.assertIn(TokenType.RBRACKET, [t.kind for t in tokens])

    def test_invalid_numeric_tokens_report_source_line(self):
        for literal in ("0b", "0b2", "0b102", "0b10x", "0x", "0xG", "12foo", "1.5", "-1"):
            with self.subTest(literal=literal):
                with self.assertRaises(LexError) as ctx:
                    tokenize(f"; cabeçalho\nPRINT {literal}\n", "bad-number.rasm")
                self.assertEqual(ctx.exception.filename, "bad-number.rasm")
                self.assertEqual(ctx.exception.line, 2)
                self.assertIsNotNone(ctx.exception.col)


class ParserV2Tests(unittest.TestCase):
    def assert_invalid(self, source: str):
        with self.assertRaises(SOURCE_ERRORS) as ctx:
            compile_source(source, "invalid-v2.rasm")
        self.assertEqual(ctx.exception.filename, "invalid-v2.rasm")
        self.assertIsNotNone(ctx.exception.line)
        return ctx.exception

    def test_forward_constants_directives_and_inline_labels(self):
        program = compile_source(
            "JUMP begin\n"
            ".DATA ADDRESS, FIRST, 0b1010\n"
            ".CONST FIRST, 0xF\n"
            ".CONST ADDRESS, 254\n"
            "begin: POWER DUST0, FIRST ; sem comentário no texto\n"
            "TICKDELAY WAIT\n"
            ".CONST WAIT, 10000\n"
            "finish: HALT # fim\n",
            "forward.rasm",
        )
        self.assertEqual(program.labels, {"begin": 1, "finish": 3})
        self.assertEqual(program.constants, {"FIRST": 15, "ADDRESS": 254, "WAIT": 10000})
        self.assertIsInstance(program.constants, dict)
        self.assertEqual(len(program.instructions), 4)
        self.assertEqual([i.line for i in program.instructions], [1, 5, 6, 8])
        self.assertEqual(program.instructions[0].args, (1,))
        self.assertEqual(program.instructions[1].args, (R.DUST0, 15))
        self.assertEqual(program.instructions[2].args, (10000,))
        self.assertNotIn(";", program.instructions[1].text)
        self.assertNotIn("#", program.instructions[-1].text)
        self.assertEqual(program.data, (0,) * 254 + (15, 10))

    def test_constant_boundaries_resolve_by_operand_context(self):
        program = compile_source(
            ".CONST ZERO, 0\n.CONST MAX, 15\n.CONST LAST, 255\n.CONST WAIT, 10000\n"
            "POWER DUST0, ZERO\nWIRE DUST1, MAX\nSTORE LAST, MAX\n"
            "LOAD DUST2, LAST\nIN DUST3, MAX\nOUT ZERO, MAX\nTICKDELAY WAIT\n"
        )
        self.assertEqual(program.instructions[2].args, (255, 15))
        self.assertEqual(program.instructions[-1].args, (10000,))
        for source in (
            ".CONST BIG, 16\nPOWER DUST0, BIG",
            ".CONST BIG, 16\nADD DUST0, BIG, 1",
            ".CONST BIG, 256\nLOAD DUST0, BIG",
            ".CONST BIG, 16\nIN DUST0, BIG",
            ".CONST ZERO, 0\nTICKDELAY ZERO",
        ):
            with self.subTest(source=source):
                self.assert_invalid(source)

    def test_labels_and_constants_are_case_sensitive(self):
        vm, _, _, events = execute(
            ".CONST Limit, 3\n.CONST limit, 5\n"
            "PRINT Limit\nPRINT limit\nJUMP Done\n"
            "done: PRINT 0\nDone: HALT\n"
        )
        self.assertEqual([e.value for e in events], [3, 5])
        self.assertNotEqual(vm.program.labels["done"], vm.program.labels["Done"])
        for source in (".CONST Limit, 3\nPRINT LIMIT", "JUMP Done\ndone: HALT"):
            with self.subTest(source=source):
                self.assert_invalid(source)

    def test_duplicate_names_and_label_constant_collisions(self):
        for source in (
            ".CONST name, 0\n.CONST name, 0",
            ".CONST name, 1\n.CONST name, 2",
            "name: NOP\nname: HALT",
            "name: NOP\n.CONST name, 3",
            ".CONST name, 3\nname: HALT",
            ".CONST name, 0\nname: HALT",
        ):
            with self.subTest(source=source):
                self.assert_invalid(source)

    def test_reserved_names_cannot_be_constants_or_labels(self):
        for name in (*MNEMONICS, *(r.value for r in R)):
            for spelling in (name, name.lower()):
                with self.subTest(name=spelling, kind="constant"):
                    self.assert_invalid(f".CONST {spelling}, 1")
                with self.subTest(name=spelling, kind="label"):
                    self.assert_invalid(f"{spelling}: HALT")

    def test_constants_require_single_literal_no_expressions_or_aliases(self):
        for source in (
            ".CONST A, -1", ".CONST A, 10001", ".CONST A, 1.5",
            ".CONST A", ".CONST A 1", ".CONST A, 1, 2", ".CONST 1, 2",
            ".CONST A, B\n.CONST B, 2", ".CONST B, 2\n.CONST A, B",
            ".CONST A, 1+2", ".CONST A, (2)", ".CONST A, DUST0",
            "PRINT 1+2", "LOAD DUST0, [DUST1+1]", ".UNKNOWN A, 1",
        ):
            with self.subTest(source=source):
                self.assert_invalid(source)

    def test_all_signatures_accept_valid_operand_counts_only(self):
        operands = {
            "POWER": ("DUST0", "1"), "WIRE": ("TORCH", "1"),
            "INVERT": ("REPEATER", "0"), "COMPARE": ("COMPARATOR", "2", "1"),
            "SUBTRACT": ("DUST0", "2", "1"), "TICKDELAY": ("1",),
            "FADE": ("DUST0",), "JUMP": ("finish",),
            "JNZ": ("TORCH", "finish"), "JZ": ("REPEATER", "finish"),
            "HALT": (), "NOP": (), "INC": ("DUST7",), "NOT": ("DUST0", "1"),
            "LOAD": ("DUST0", "[DUST1:DUST2]"), "STORE": ("[DUST0]", "2"),
            "PUSH": ("3",), "POP": ("COMPARATOR",), "CALL": ("finish",),
            "RET": (), "IN": ("DUST0", "DUST1"), "OUT": ("DUST0", "15"),
            "PRINT": ("TORCH",),
        }
        operands.update({op: ("DUST0", "2", "1") for op in BINARY_EXPECTED})
        operands.update({op: ("2", "1", "finish") for op in RELATION_EXPECTED})
        self.assertEqual(set(operands), set(MNEMONICS))
        for op, args in operands.items():
            line = op + (" " + ", ".join(args) if args else "")
            with self.subTest(op=op, form="valid"):
                compile_source(line + "\nfinish: HALT")
            invalid = [line + ", 1", line + ","]
            if args:
                invalid.append(op + " " + ", ".join(args[:-1]))
            if len(args) > 1:
                invalid.append(op + " " + " ".join(args))
            for bad in invalid:
                with self.subTest(op=op, form=bad):
                    self.assert_invalid(bad + "\nfinish: HALT")

    def test_value_operands_reject_non_nibbles_and_wrong_kinds(self):
        templates = [
            "POWER DUST0, {bad}", "WIRE DUST0, {bad}", "INVERT DUST0, {bad}",
            "NOT DUST0, {bad}", "PUSH {bad}", "PRINT {bad}",
            "STORE 255, {bad}", "OUT {bad}, 1", "OUT 0, {bad}", "IN DUST0, {bad}",
        ]
        for op in BINARY_EXPECTED:
            templates.extend((f"{op} DUST0, {{bad}}, 1", f"{op} DUST0, 1, {{bad}}"))
        for op in RELATION_EXPECTED:
            templates.extend((f"{op} {{bad}}, 1, finish", f"{op} 1, {{bad}}, finish"))
        for template in templates:
            for value in ("16", "255", "-1", "1.5", "DUST8", "missing", "[DUST1]"):
                source = template.format(bad=value)
                with self.subTest(source=source):
                    self.assert_invalid(source + "\nfinish: HALT")

    def test_destination_dust_label_and_address_validation(self):
        invalid = [
            "POWER DUST0, DUST1", "POWER TORCH, 1", "INC TORCH", "FADE REPEATER",
            "INC COMPARATOR", "WIRE 0, 1", "NOT 0, 1", "ADD 0, 1, 2",
            "LOAD 0, 1", "POP 0", "IN 0, 1", "JZ 0, finish", "JNZ 1, finish",
            "CALL 1", "CALL missing", "JE 1, 1, 1", "JUMP DUST0",
            ".CONST TARGET, 1\nCALL TARGET", "named: NOP\nPRINT named",
            "TICKDELAY DUST0", "TICKDELAY 10001", "LOAD DUST0, 256",
            "STORE 256, 1", "LOAD DUST0, -1", "LOAD DUST0, DUST1",
            "LOAD DUST0, []", "LOAD DUST0, [1]", "LOAD DUST0, [DUST1",
            "LOAD DUST0, [DUST1:]", "LOAD DUST0, [:DUST1]",
            "LOAD DUST0, [DUST1:1]", "LOAD DUST0, [DUST1:DUST2:DUST3]",
            "STORE [DUST0], 1, 2", "LOAD DUST0, [DUST1]]",
            "start: .DATA 0, 1", "start: .CONST VAL, 1", "first: second: HALT",
        ]
        for source in invalid:
            with self.subTest(source=source):
                self.assert_invalid(source + "\nfinish: HALT")

    def test_data_rejects_overlap_overflow_and_invalid_values(self):
        for source in (
            ".DATA 0", ".DATA 0,", ".DATA 0, 16", ".DATA -1, 0",
            ".DATA 256, 0", ".DATA 255, 1, 2", ".DATA 0, -1",
            ".DATA 0, DUST0", ".DATA [DUST0], 1", ".DATA 0, unknown",
            ".DATA 0, 0\n.DATA 0, 0", ".DATA 4, 1, 2\n.DATA 5, 3",
            ".DATA 5, 3\n.DATA 4, 1, 2",
            ".CONST BASE, 255\n.DATA BASE, 1, 2",
        ):
            with self.subTest(source=source):
                self.assert_invalid(source)

    def test_data_can_initialize_exactly_256_cells_without_instructions(self):
        values = tuple(i % 16 for i in range(256))
        source = ".DATA 0, " + ", ".join(map(str, values))
        program = compile_source(source)
        self.assertEqual(program.instructions, ())
        self.assertEqual(program.data, values)
        self.assertIsInstance(program.data, tuple)
        self.assert_invalid(source + ", 0")
        adjacent = compile_source(".DATA 254, 7\n.DATA 255, 8\n.DATA 0, 9")
        self.assertEqual(adjacent.data, (9,) + (0,) * 253 + (7, 8))


class ArithmeticV2Tests(unittest.TestCase):
    def test_all_binary_operations_all_nibble_pairs_and_aliases(self):
        for op, expected in BINARY_EXPECTED.items():
            for a in range(16):
                for b in range(16):
                    if op in ("DIV", "MOD") and b == 0:
                        continue  # Os erros de divisor zero têm teste próprio, abaixo.
                    with self.subTest(op=op, a=a, b=b):
                        lines = []
                        # Dois imediatos, dois registradores, aliases à esquerda/direita,
                        # e formas mistas. Cada operação deve ler antes de escrever.
                        for dest, left, right in (
                            ("DUST2", str(a), str(b)),
                            ("DUST2", "DUST0", "DUST1"),
                            ("DUST0", "DUST0", "DUST1"),
                            ("DUST1", "DUST0", "DUST1"),
                            ("DUST0", "DUST0", str(b)),
                            ("DUST1", str(a), "DUST1"),
                        ):
                            lines.extend((f"POWER DUST0, {a}", f"POWER DUST1, {b}",
                                          f"{op} {dest}, {left}, {right}", f"PRINT {dest}"))
                        vm, _, _, events = execute("\n".join(lines) + "\nHALT")
                        self.assertEqual([e.value for e in events], [expected(a, b)] * 6)
                        self.assertTrue(all(0 <= v <= 15 for v in vm.registers.values()))
                        if a == b:
                            _, _, _, same = execute(
                                f"POWER DUST0, {a}\n{op} DUST0, DUST0, DUST0\nPRINT DUST0"
                            )
                            self.assertEqual([e.value for e in same], [expected(a, a)])

    def test_unary_operations_all_nibbles_and_self_alias(self):
        unary = {"WIRE": lambda a: a, "INVERT": lambda a: 0 if a else 15,
                 "NOT": lambda a: a ^ 15}
        for op, expected in unary.items():
            for value in range(16):
                with self.subTest(op=op, value=value):
                    _, _, _, events = execute(
                        f"{op} DUST0, {value}\nPRINT DUST0\n"
                        f"POWER DUST0, {value}\n{op} DUST0, DUST0\nPRINT DUST0\nHALT"
                    )
                    self.assertEqual([e.value for e in events], [expected(value)] * 2)
        for value in range(16):
            with self.subTest(op="INC/FADE", value=value):
                _, _, _, events = execute(
                    f"POWER DUST0, {value}\nINC DUST0\nPRINT DUST0\n"
                    f"POWER DUST0, {value}\nFADE DUST0\nPRINT DUST0\nHALT"
                )
                self.assertEqual([e.value for e in events], [min(15, value + 1), max(0, value - 1)])

    def test_division_and_modulo_zero_preserve_destination_and_metadata(self):
        for op in ("DIV", "MOD"):
            for value in range(16):
                for dest in ("DUST0", "TORCH", "COMPARATOR", "REPEATER"):
                    for divisor in ("0", "DUST7"):
                        with self.subTest(op=op, value=value, dest=dest, divisor=divisor):
                            source = (
                                f"WIRE {dest}, 7\nCOMPARE COMPARATOR, 12, 4\n"
                                f"TICKDELAY 1\n{op} {dest}, {value}, {divisor}\nHALT"
                            )
                            vm = RedVM(compile_source(source, "zero.rasm"))
                            vm.step_tick()
                            before = vm.snapshot()
                            with self.assertRaises(VMError) as ctx:
                                vm.step_tick()
                            after = vm.snapshot()
                            self.assertEqual(ctx.exception.line, 4)
                            self.assertEqual(ctx.exception.filename, "zero.rasm")
                            self.assertIn(op, str(ctx.exception))
                            self.assertIn("zero", str(ctx.exception).lower())
                            self.assertEqual(after.registers, before.registers)
                            self.assertEqual(after.comparator, before.comparator)
                            self.assertEqual(after.pending, before.pending)

    def test_component_writes_transform_and_repeater_commits(self):
        cases = [
            ("WIRE {dest}, 0", 0), ("WIRE {dest}, 7", 7),
            ("INVERT {dest}, 0", 15), ("INVERT {dest}, 7", 0),
            ("NOT {dest}, 15", 0), ("NOT {dest}, 5", 10),
            ("COMPARE {dest}, 7, 4", 7), ("COMPARE {dest}, 4, 7", 0),
            ("SUBTRACT {dest}, 4, 7", 0), ("SUBTRACT {dest}, 7, 4", 3),
            ("LOAD {dest}, 255", 6), ("POP {dest}", 6), ("IN {dest}, 15", 6),
        ]
        for op, expected in BINARY_EXPECTED.items():
            cases.append((f"{op} {{dest}}, 3, 2", expected(3, 2)))
        for template, value in cases:
            for dest in (R.TORCH, R.REPEATER, R.COMPARATOR):
                with self.subTest(instruction=template, dest=dest):
                    vm, _, frames, events = execute(
                        ".DATA 255, 6\nPUSH 6\n" + template.format(dest=dest.value)
                        + f"\nPRINT {dest.value}\nTICKDELAY 1\nPRINT {dest.value}\nHALT",
                        {15: 6},
                    )
                    transformed = (15 if value == 0 else 0) if dest is R.TORCH else value
                    before = 0 if dest is R.REPEATER else transformed
                    self.assertEqual([e.value for e in events], [before, transformed])
                    self.assertEqual([e.tick for e in events], [0, 1])
                    self.assertEqual(frames[0].registers[dest], before)
                    self.assertEqual(frames[0].pending, 1 if dest is R.REPEATER else 0)
                    self.assertEqual(vm.registers[dest], transformed)
                    self.assertEqual(vm.snapshot().pending, 0)

    def test_repeater_last_write_wins_and_alu_reads_committed_value(self):
        vm, _, frames, events = execute(
            "WIRE REPEATER, 4\nADD REPEATER, REPEATER, 7\nPRINT REPEATER\n"
            "TICKDELAY 2\nPRINT REPEATER\nADD REPEATER, REPEATER, 2\n"
            "PRINT REPEATER\nTICKDELAY 1\nPRINT REPEATER\nHALT"
        )
        self.assertEqual([(e.tick, e.value) for e in events], [(0, 0), (2, 7), (2, 7), (3, 9)])
        self.assertEqual([f.pending for f in frames], [2, 0, 1])
        self.assertEqual(vm.registers[R.REPEATER], 9)

    def test_comparator_metadata_is_replaced_then_cleared_by_alu(self):
        operations = [f"{op} COMPARATOR, 9, 2" for op in BINARY_EXPECTED
                      if op not in ("COMPARE", "SUBTRACT")]
        operations += ["NOT COMPARATOR, 3", "WIRE COMPARATOR, 4", "INVERT COMPARATOR, 4",
                       "LOAD COMPARATOR, 0", "PUSH 7\nPOP COMPARATOR", "IN COMPARATOR, 0"]
        for operation in operations:
            with self.subTest(operation=operation):
                vm, _, frames, _ = execute(
                    "COMPARE COMPARATOR, 12, 4\nTICKDELAY 1\n"
                    "SUBTRACT COMPARATOR, COMPARATOR, 3\nTICKDELAY 1\n"
                    + operation + "\nHALT"
                )
                self.assertEqual(frames[0].comparator, (12, 4, "COMPARE"))
                self.assertEqual(frames[1].comparator, (12, 3, "SUBTRACT"))
                self.assertIsNone(vm.snapshot().comparator)

    def test_components_are_values_not_just_destinations(self):
        _, _, _, events = execute(
            "WIRE TORCH, 0\nWIRE REPEATER, 3\nCOMPARE COMPARATOR, 6, 2\n"
            "TICKDELAY 1\nADD DUST0, TORCH, REPEATER\nPRINT DUST0\n"
            "SUBTRACT DUST1, COMPARATOR, REPEATER\nPRINT DUST1\n"
            "NOT DUST2, TORCH\nPRINT DUST2\nPUSH COMPARATOR\nPOP DUST3\nPRINT DUST3"
        )
        self.assertEqual([e.value for e in events], [15, 3, 0, 6])


class BranchV2Tests(unittest.TestCase):
    def test_relations_all_nibble_pairs_without_flags(self):
        for op, relation in RELATION_EXPECTED.items():
            for a in range(16):
                for b in range(16):
                    with self.subTest(op=op, a=a, b=b):
                        lines = [f"POWER DUST0, {a}", f"POWER DUST1, {b}"]
                        for i, (left, right) in enumerate(((str(a), str(b)), ("DUST0", "DUST1"),
                                                         ("DUST0", str(b)), (str(a), "DUST1"))):
                            lines.extend((
                                "COMPARE COMPARATOR, 0, 15" if i % 2 else "COMPARE COMPARATOR, 15, 0",
                                f"{op} {left}, {right}, taken_{i}", "PRINT 0", f"JUMP end_{i}",
                                f"taken_{i}: PRINT 1", f"end_{i}: NOP",
                            ))
                        lines.append("HALT")
                        vm, _, _, events = execute("\n".join(lines))
                        self.assertEqual([e.value for e in events], [int(relation(a, b))] * 4)
                        self.assertEqual(vm.registers[R.DUST0], a)
                        self.assertEqual(vm.registers[R.DUST1], b)

    def test_jz_jnz_accept_all_components_and_both_outcomes(self):
        for component in (R.TORCH, R.REPEATER, R.COMPARATOR):
            for value in (0, 7):
                level = (0 if value else 15) if component is R.TORCH else value
                for op in ("JZ", "JNZ"):
                    with self.subTest(component=component, value=value, op=op):
                        _, _, _, events = execute(
                            f"WIRE {component.value}, {value}\nTICKDELAY 1\n"
                            f"{op} {component.value}, taken\nPRINT 0\nJUMP finish\n"
                            "taken: PRINT 1\nfinish: HALT"
                        )
                        self.assertEqual([e.value for e in events], [int((level == 0) == (op == "JZ"))])

    def test_relations_accept_components_and_do_not_change_comparator_metadata(self):
        for op, relation in RELATION_EXPECTED.items():
            with self.subTest(op=op):
                vm, _, _, events = execute(
                    "WIRE TORCH, 0\nWIRE REPEATER, 7\nTICKDELAY 1\n"
                    "COMPARE COMPARATOR, 9, 4\n"
                    f"{op} TORCH, REPEATER, taken\nPRINT 0\nJUMP finish\n"
                    "taken: PRINT 1\nfinish: HALT"
                )
                self.assertEqual([e.value for e in events], [int(relation(15, 7))])
                self.assertEqual(vm.snapshot().comparator, (9, 4, "COMPARE"))


class MemoryV2Tests(unittest.TestCase):
    def test_direct_and_two_register_addressing_all_256_cells(self):
        for address in range(256):
            with self.subTest(address=address):
                initial = address % 16
                changed = (initial + 1) % 16
                final = (initial + 2) % 16
                vm, _, _, events = execute(
                    f".DATA {address}, {initial}\nLOAD DUST0, {address}\nPRINT DUST0\n"
                    f"STORE {address}, {changed}\nLOAD DUST0, {address}\nPRINT DUST0\n"
                    f"POWER DUST1, {address // 16}\nPOWER DUST2, {address % 16}\n"
                    "LOAD DUST0, [DUST1:DUST2]\nPRINT DUST0\n"
                    f"POWER DUST3, {final}\nSTORE [DUST1:DUST2], DUST3\n"
                    f"LOAD DUST0, {address}\nPRINT DUST0\nHALT"
                )
                self.assertEqual([e.value for e in events], [initial, changed, changed, final])
                self.assertEqual(vm.memory[address], final)
                self.assertEqual(len(vm.memory), 256)
                self.assertTrue(all(value == 0 for i, value in enumerate(vm.memory) if i != address))

    def test_single_register_address_uses_only_low_nibble(self):
        for address in range(16):
            with self.subTest(address=address):
                vm, _, _, events = execute(
                    f"POWER DUST0, {address}\nPOWER DUST1, 15\n"
                    "STORE [DUST0], 7\nLOAD DUST2, [DUST0]\nPRINT DUST2\nHALT"
                )
                self.assertEqual([e.value for e in events], [7])
                self.assertEqual(vm.memory[address], 7)
                self.assertEqual(vm.memory[240 + address], 0)

    def test_load_destination_may_alias_address_register(self):
        for setup, address, destination, literal in (
            ("POWER DUST0, 15", "[DUST0]", "DUST0", 15),
            ("POWER DUST0, 10\nPOWER DUST1, 11", "[DUST0:DUST1]", "DUST0", 171),
            ("POWER DUST0, 10\nPOWER DUST1, 11", "[DUST0:DUST1]", "DUST1", 171),
            ("POWER DUST0, 15", "[DUST0:DUST0]", "DUST0", 255),
        ):
            with self.subTest(address=address, destination=destination):
                vm, _, _, events = execute(
                    f".DATA {literal}, 6\n{setup}\nLOAD {destination}, {address}\nPRINT {destination}"
                )
                self.assertEqual([e.value for e in events], [6])
                self.assertEqual(vm.memory[literal], 6)

    def test_component_addresses_and_store_source_alias(self):
        vm, _, _, events = execute(
            "WIRE TORCH, 0\nWIRE COMPARATOR, 14\nWIRE REPEATER, 13\nTICKDELAY 1\n"
            "STORE [TORCH:COMPARATOR], COMPARATOR\nLOAD DUST0, 254\nPRINT DUST0\n"
            "STORE [REPEATER], REPEATER\nLOAD DUST0, 13\nPRINT DUST0\nHALT"
        )
        self.assertEqual([e.value for e in events], [14, 13])
        self.assertEqual(vm.memory[254], 14)
        self.assertEqual(vm.memory[13], 13)

    def test_program_data_is_immutable_and_each_vm_gets_own_ram(self):
        program = compile_source(".DATA 0, 3\n.DATA 255, 15\nSTORE 0, 8\nHALT")
        self.assertIsInstance(program.data, tuple)
        self.assertEqual(len(program.data), 256)
        with self.assertRaises(TypeError):
            program.data[0] = 7
        first, second = RedVM(program), RedVM(program)
        first.run()
        self.assertEqual(first.memory[0], 8)
        self.assertEqual(second.memory[0], 3)
        self.assertEqual(program.data[0], 3)
        second.memory[255] = 1
        self.assertEqual(first.memory[255], 15)
        self.assertEqual(program.data[255], 15)
        self.assertEqual(RedVM(program).memory[255], 15)
        empty = compile_source("HALT")
        self.assertEqual(empty.data, (0,) * 256)
        self.assertEqual(empty.constants, {})


class StackV2Tests(unittest.TestCase):
    def test_data_stack_capacity_64_and_lifo(self):
        values = [i % 16 for i in range(64)]
        source = "\n".join(f"PUSH {value}" for value in values)
        source += "\nTICKDELAY 1\n" + "POP DUST0\nPRINT DUST0\n" * 64 + "HALT"
        vm, _, frames, events = execute(source)
        self.assertEqual(frames[0].stack, tuple(values))
        self.assertEqual([e.value for e in events], values[::-1])
        self.assertEqual(vm.snapshot().stack, ())
        self.assertEqual(vm.snapshot().call_depth, 0)

    def test_data_stack_overflow_preserves_64_values_and_reports_line(self):
        vm = RedVM(compile_source("PUSH 7\n" * 64 + "PUSH 9\n", "stack.rasm"))
        with self.assertRaises(VMError) as ctx:
            vm.run()
        self.assertEqual(ctx.exception.filename, "stack.rasm")
        self.assertEqual(ctx.exception.line, 65)
        self.assertIn("64", str(ctx.exception))
        self.assertEqual(vm.snapshot().stack, (7,) * 64)

    def test_underflows_do_not_borrow_from_other_stack_or_modify_dest(self):
        cases = (
            ("POWER DUST0, 12\nPOP DUST0", 2, (), 0),
            ("PUSH 3\nRET", 2, (3,), 0),
            ("POWER DUST0, 12\nCALL sub\nHALT\nsub: POP DUST0\nRET", 4, (), 1),
        )
        for source, line, stack, depth in cases:
            with self.subTest(source=source):
                vm = RedVM(compile_source(source, "underflow.rasm"))
                with self.assertRaises(VMError) as ctx:
                    vm.run()
                self.assertEqual(ctx.exception.filename, "underflow.rasm")
                self.assertEqual(ctx.exception.line, line)
                self.assertEqual(vm.snapshot().stack, stack)
                self.assertEqual(vm.snapshot().call_depth, depth)
                if "POP" in source:
                    self.assertEqual(vm.registers[R.DUST0], 12)

    def test_64_nested_calls_with_full_independent_data_stack(self):
        lines = ["PUSH 7"] * 64 + ["CALL sub_0", "POP DUST0\nPRINT DUST0\n" * 64, "HALT"]
        for i in range(64):
            lines.append(f"sub_{i}: " + (f"CALL sub_{i + 1}\nRET" if i < 63 else "RET"))
        depths = []
        events = []
        vm = RedVM(compile_source("\n".join(lines)), on_output=events.append)
        vm.on_execute = lambda tick, pc, instruction: depths.append(vm.snapshot().call_depth)
        result = vm.run()
        self.assertTrue(result.halted)
        self.assertEqual(max(depths), 64)
        self.assertEqual([e.value for e in events], [7] * 64)
        self.assertEqual(vm.snapshot().stack, ())
        self.assertEqual(vm.snapshot().call_depth, 0)

    def test_recursive_call_65_fails_without_truncating_stack(self):
        vm = RedVM(compile_source("recurse: CALL recurse\n", "recursion.rasm"))
        with self.assertRaises(VMError) as ctx:
            vm.run()
        self.assertEqual(ctx.exception.filename, "recursion.rasm")
        self.assertEqual(ctx.exception.line, 1)
        self.assertIn("64", str(ctx.exception))
        self.assertEqual(vm.snapshot().call_depth, 64)
        self.assertEqual(vm.call_stack, [1] * 64)
        self.assertEqual(vm.snapshot().stack, ())

    def test_return_pc_above_15_is_not_a_nibble(self):
        source = "NOP\n" * 20 + "CALL sub\nPRINT DUST0\nHALT\nsub: ADD DUST0, 3, 3\nRET"
        returns = []
        events = []
        vm = RedVM(compile_source(source), on_output=events.append)

        def observe(tick, pc, instruction):
            if instruction.mnemonic == "RET":
                returns.append(tuple(vm.call_stack))

        vm.on_execute = observe
        vm.run()
        self.assertEqual(returns, [(21,)])
        self.assertEqual([e.value for e in events], [6])
        self.assertEqual(vm.snapshot().call_depth, 0)

    def test_delays_inside_nested_calls_preserve_both_stacks(self):
        vm, result, frames, events = execute(
            "PUSH 1\nCALL outer\nPOP DUST2\nPRINT DUST0\nPRINT DUST1\nPRINT DUST2\nHALT\n"
            "outer: PUSH 3\nCALL inner\nTICKDELAY 1\nPOP DUST1\nRET\n"
            "inner: PUSH 4\nTICKDELAY 2\nPOP DUST0\nRET"
        )
        self.assertEqual(result.tick, 3)
        self.assertEqual([f.call_depth for f in frames], [2, 2, 1])
        self.assertEqual([f.stack for f in frames], [(1, 3, 4), (1, 3, 4), (1, 3)])
        self.assertEqual([e.value for e in events], [4, 3, 1])
        self.assertEqual(vm.snapshot().stack, ())
        self.assertEqual(vm.snapshot().call_depth, 0)


class IOV2Tests(unittest.TestCase):
    def test_default_input_and_output_ports_are_zero_and_independent(self):
        vm, _, _, events = execute("\n".join(f"IN DUST0, {i}\nPRINT DUST0" for i in range(16)))
        self.assertEqual([e.value for e in events], [0] * 16)
        self.assertEqual(vm.snapshot().inputs, (0,) * 16)
        self.assertEqual(vm.snapshot().outputs, (0,) * 16)
        other = RedVM(compile_source("HALT"))
        vm.set_input(0, 7)
        self.assertEqual(other.snapshot().inputs, (0,) * 16)

    def test_all_ports_register_aliases_and_latched_outputs(self):
        for port in range(16):
            with self.subTest(port=port):
                vm, _, _, events = execute(
                    f"POWER DUST0, {port}\nIN DUST0, DUST0\nPRINT DUST0\n"
                    f"POWER DUST1, {port}\nOUT DUST1, DUST0\nPOWER DUST0, 0\n"
                    f"IN DUST2, {port}\nPRINT DUST2\nHALT",
                    {port: 15 - port},
                )
                self.assertEqual([e.value for e in events], [15 - port] * 2)
                self.assertEqual(vm.outputs[port], 15 - port)
                self.assertEqual(vm.inputs[port], 15 - port)
                self.assertTrue(all(v == 0 for i, v in enumerate(vm.outputs) if i != port))

    def test_out_does_not_feed_input_and_print_does_not_write_ports(self):
        vm, _, _, events = execute(
            "OUT 0, 15\nIN DUST0, 0\nPRINT DUST0\nPRINT 7\n"
            "OUT 0, 3\nTICKDELAY 2\nPRINT 9\nHALT"
        )
        self.assertEqual([(e.tick, e.value) for e in events], [(0, 0), (0, 7), (2, 9)])
        self.assertEqual(vm.snapshot().inputs, (0,) * 16)
        self.assertEqual(vm.snapshot().outputs, (3,) + (0,) * 15)

    def test_component_ports_use_their_committed_values(self):
        vm, _, _, events = execute(
            "WIRE TORCH, 0\nWIRE REPEATER, 14\nTICKDELAY 1\n"
            "IN DUST0, TORCH\nIN DUST1, REPEATER\nOUT TORCH, DUST0\n"
            "OUT REPEATER, DUST1\nPRINT DUST0\nPRINT DUST1",
            {15: 7, 14: 6},
        )
        self.assertEqual([e.value for e in events], [7, 6])
        self.assertEqual(vm.outputs[14:], [6, 7])

    def test_set_input_requires_strict_integer_and_nibble_range(self):
        class IntegerSubclass(int):
            pass

        vm = RedVM(compile_source("HALT"))
        for bad in (True, False, 1.0, "1", None, IntegerSubclass(1)):
            for port, value in ((bad, 1), (1, bad)):
                with self.subTest(port=port, value=value, type=type(bad).__name__):
                    with self.assertRaises(TypeError):
                        vm.set_input(port, value)
                    self.assertEqual(vm.snapshot().inputs, (0,) * 16)
        for port, value in ((-1, 0), (16, 0), (0, -1), (0, 16), (10000, 0), (0, 10000)):
            with self.subTest(port=port, value=value):
                with self.assertRaises(ValueError):
                    vm.set_input(port, value)
                self.assertEqual(vm.snapshot().inputs, (0,) * 16)
        vm.set_input(0, 0)
        vm.set_input(15, 15)
        self.assertEqual(vm.snapshot().inputs[-1], 15)

    def test_inject_clamps_but_rejects_non_integer_levels(self):
        vm = RedVM(compile_source("TICKDELAY 1\nHALT"))
        for value, expected in ((-100, 0), (0, 0), (7, 7), (15, 15), (100, 15)):
            with self.subTest(value=value):
                vm.inject(R.DUST0, value)
                self.assertEqual(vm.registers[R.DUST0], expected)
        before = vm.snapshot()
        for value in (True, False, 1.5, "7", None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    vm.inject(R.DUST0, value)
                self.assertEqual(vm.snapshot(), before)
        for register in ("DUST0", 0, None):
            with self.subTest(register=register):
                with self.assertRaises(TypeError):
                    vm.inject(register, 7)
        vm.inject(R.TORCH, -5)
        self.assertEqual(vm.registers[R.TORCH], 15)
        vm.inject(R.REPEATER, 99)
        self.assertEqual(vm.registers[R.REPEATER], 0)
        vm.step_tick()
        self.assertEqual(vm.registers[R.REPEATER], 15)

    def test_external_changes_between_ticks_are_latched(self):
        burst = "IN DUST0, 0\nPRINT DUST0\nOUT 0, DUST0\nWIRE DUST1, DUST2\n"
        events = []
        vm = RedVM(compile_source(burst + "TICKDELAY 1\n" + burst + "TICKDELAY 1\n" + burst + "HALT"),
                   on_output=events.append)
        vm.set_input(0, 3)
        vm.inject(R.DUST2, 4)
        first = vm.step_tick()
        self.assertEqual(first.frame.registers[R.DUST1], 4)
        self.assertEqual(first.frame.outputs[0], 3)
        vm.set_input(0, 9)
        vm.inject(R.DUST2, 8)
        second = vm.step_tick()
        self.assertEqual(second.frame.registers[R.DUST1], 8)
        self.assertEqual(second.frame.outputs[0], 9)
        final = vm.step_tick()
        self.assertTrue(final.ended)
        self.assertEqual([(e.tick, e.value) for e in events], [(0, 3), (1, 9), (2, 9)])
        self.assertEqual(first.frame.inputs[0], 3)
        self.assertEqual(first.frame.outputs[0], 3)
        self.assertEqual(first.frame.registers[R.DUST2], 4)

    def test_print_calls_output_events_without_native_stdout(self):
        for callback in (False, True):
            with self.subTest(callback=callback):
                events = []
                vm = RedVM(compile_source("PRINT 3\nTICKDELAY 2\nPRINT 0b1111\nHALT"),
                           on_output=events.append if callback else None)
                stream = io.StringIO()
                with redirect_stdout(stream):
                    vm.run()
                self.assertEqual(stream.getvalue(), "")
                if callback:
                    self.assertTrue(all(isinstance(e, OutputEvent) for e in events))
                    self.assertEqual([(e.tick, e.value) for e in events], [(0, 3), (2, 15)])
                else:
                    self.assertEqual(events, [])


class ExecutionV2Tests(unittest.TestCase):
    def test_snapshots_are_independent_of_vm_and_each_other(self):
        vm = RedVM(compile_source(
            ".DATA 5, 2\nPOWER DUST0, 5\nPUSH DUST0\nSTORE 5, DUST0\n"
            "IN DUST1, 0\nOUT 1, DUST1\nCALL mutate\nHALT\n"
            "mutate: TICKDELAY 1\nPOP DUST2\nSTORE 5, 9\nPOWER DUST0, 8\n"
            "OUT 1, 12\nTICKDELAY 1\nRET"
        ))
        vm.set_input(0, 3)
        first = vm.step_tick().frame
        duplicate = vm.snapshot()
        vm.set_input(0, 7)
        vm.inject(R.DUST0, 11)
        second = vm.step_tick().frame
        final = vm.step_tick().frame
        self.assertEqual(first.memory[5], 5)
        self.assertEqual(first.stack, (5,))
        self.assertEqual(first.inputs[0], 3)
        self.assertEqual(first.outputs[1], 3)
        self.assertEqual(first.call_depth, 1)
        self.assertEqual(second.memory[5], 9)
        self.assertEqual(second.stack, ())
        self.assertEqual(second.inputs[0], 7)
        self.assertEqual(second.outputs[1], 12)
        self.assertEqual(final.call_depth, 0)
        for name in ("memory", "stack", "inputs", "outputs"):
            values = getattr(first, name)
            self.assertIsInstance(values, tuple)
            with self.assertRaises(TypeError):
                values[0] = 15
        first.registers[R.DUST0] = 0
        self.assertEqual(duplicate.registers[R.DUST0], 5)
        self.assertEqual(second.registers[R.DUST0], 8)
        self.assertEqual(vm.registers[R.DUST0], 8)
        self.assertIsNot(first.registers, second.registers)

    def test_run_and_step_match_without_tick_limit(self):
        sources = (
            "NOP\nHALT", "PRINT 1", "",
            "WIRE REPEATER, 3\nTICKDELAY 3\nADD REPEATER, REPEATER, 4\n"
            "PRINT REPEATER\nTICKDELAY 2\nPRINT REPEATER\nHALT",
            ".DATA 255, 6\nIN DUST0, 0\nCALL sub\nPRINT DUST0\nHALT\n"
            "sub: PUSH DUST0\nLOAD DUST0, 255\nOUT 1, DUST0\nTICKDELAY 2\n"
            "POP DUST1\nADD DUST0, DUST0, DUST1\nSTORE 255, DUST0\nRET",
            "POWER DUST0, 3\nloop: PRINT DUST0\nFADE DUST0\nTICKDELAY 1\n"
            "JGT DUST0, 0, loop\nHALT",
        )
        for source in sources:
            with self.subTest(source=source):
                program = compile_source(source)
                frames, run_events, step_events, run_trace, step_trace, forbidden_ticks = [], [], [], [], [], []
                running = RedVM(program, on_tick=frames.append, on_output=run_events.append,
                                on_execute=lambda t, p, i: run_trace.append((t, p, i)))
                stepping = RedVM(program, on_tick=forbidden_ticks.append, on_output=step_events.append,
                                 on_execute=lambda t, p, i: step_trace.append((t, p, i)))
                for vm in (running, stepping):
                    self.assertIsNone(vm.max_ticks)
                    vm.set_input(0, 5)
                result = running.run()
                step_frames = []
                for _ in range(100):  # Guarda do teste; não configura max_ticks na VM.
                    step = stepping.step_tick()
                    if step.ended:
                        self.assertEqual(step.ticks_advanced, 0)
                        break
                    self.assertEqual(step.ticks_advanced, 1)
                    step_frames.append(step.frame)
                else:
                    self.fail("programa finito não terminou em step_tick()")
                self.assertEqual(step_frames, frames)
                self.assertEqual(stepping.snapshot(), running.snapshot())
                self.assertEqual(step_events, run_events)
                self.assertEqual(step_trace, run_trace)
                self.assertEqual(len(step_trace), result.instructions)
                self.assertEqual(stepping.reason, result.reason)
                self.assertEqual(stepping.halted, result.halted)
                self.assertEqual(forbidden_ticks, [])
                self.assertEqual(stepping.step_tick(), step)

    def test_callback_reentrancy_is_rejected_without_state_changes(self):
        for mode in ("run", "step"):
            for hook in ("on_output", "on_execute", "on_tick"):
                if mode == "step" and hook == "on_tick":
                    continue  # step_tick devolve frames, não dispara on_tick.
                with self.subTest(mode=mode, hook=hook):
                    calls = []
                    events = []
                    vm = RedVM(compile_source("PRINT 3\nTICKDELAY 1\nPRINT 4\nHALT"),
                               on_output=events.append)

                    def callback(*args):
                        before = vm.snapshot()
                        for action in (vm.run, vm.step_tick):
                            with self.assertRaises(VMError):
                                action()
                            self.assertEqual(vm.snapshot(), before)
                        calls.append(args)
                        if hook == "on_output":
                            events.append(args[0])

                    setattr(vm, hook, callback)
                    if mode == "run":
                        vm.run()
                    else:
                        self.assertFalse(vm.step_tick().ended)
                        self.assertTrue(vm.step_tick().ended)
                    self.assertTrue(calls)
                    self.assertEqual([e.value for e in events], [3, 4])
                    self.assertTrue(vm.halted)

    def test_run_after_step_is_rejected(self):
        vm = RedVM(compile_source("TICKDELAY 1\nHALT"))
        vm.step_tick()
        with self.assertRaises(VMError):
            vm.run()
        self.assertTrue(vm.step_tick().ended)

    def test_step_after_run_is_rejected(self):
        vm = RedVM(compile_source("HALT"))
        vm.run()
        with self.assertRaises(VMError):
            vm.step_tick()

    def test_run_and_step_stop_before_instructions_at_tick_limit(self):
        for delay in (1, 2, 4):
            for limit in (0, 1, 2, 3, 4):
                with self.subTest(delay=delay, limit=limit):
                    source = (
                        "loop: PRINT DUST0\nINC DUST0\nSTORE 255, DUST0\n"
                        f"OUT 0, DUST0\nTICKDELAY {delay}\nJUMP loop"
                    )
                    run_events, step_events, run_frames = [], [], []
                    program = compile_source(source)
                    running = RedVM(program, max_ticks=limit, on_output=run_events.append,
                                    on_tick=run_frames.append)
                    stepping = RedVM(program, max_ticks=limit, on_output=step_events.append)
                    result = running.run()
                    step_frames = []
                    for _ in range(limit + 1):
                        step = stepping.step_tick()
                        if step.ended:
                            break
                        step_frames.append(step.frame)
                    self.assertTrue(step.ended)
                    self.assertEqual(result.tick, limit)
                    self.assertFalse(result.halted)
                    self.assertEqual(running.reason, stepping.reason)
                    self.assertEqual(running.snapshot(), stepping.snapshot())
                    self.assertEqual(run_frames, step_frames)
                    self.assertEqual(run_events, step_events)
                    self.assertEqual([e.tick for e in run_events], list(range(0, limit, delay)))
                    if limit == 0:
                        self.assertEqual(result.instructions, 0)
                        self.assertEqual(running.pc, 0)


class ExampleV2Tests(unittest.TestCase):
    def test_v2_examples_have_documented_outputs(self):
        cases = (
            ("arithmetic.rasm", {}, [7, 15, 14, 0]),
            ("memory.rasm", {}, [2, 4, 6, 8]),
            ("subroutines.rasm", {}, [6]),
            ("ports.rasm", {0: 7, 1: 5}, [15]),
        )
        for name, inputs, expected in cases:
            with self.subTest(example=name):
                source = (ROOT / "examples" / name).read_text(encoding="utf-8")
                vm, result, _, events = execute(source, inputs)
                self.assertTrue(result.halted)
                self.assertEqual([e.value for e in events], expected)
                if name == "ports.rasm":
                    self.assertEqual(vm.outputs[0], 15)


class CliV2Tests(unittest.TestCase):
    def run_cli(self, path: Path, *args: str, reject_sleep: bool = False):
        command = [sys.executable, "-X", "utf8"]
        if reject_sleep:
            # Verifica ausência de sleep sem depender de relógio ou tolerância de CI.
            command += ["-c", "from unittest.mock import patch\nfrom cli import main\n"
                        "with patch('cli.time.sleep', side_effect=AssertionError('sleep proibido')):\n"
                        "    raise SystemExit(main())\n"]
        else:
            command.append(str(ROOT / "redvm.py"))
        return subprocess.run(
            [*command, str(path), *args], capture_output=True, text=True,
            encoding="utf-8", cwd=str(ROOT), timeout=15, check=False,
        )

    def run_source(self, source: str, *args: str, reject_sleep: bool = False):
        with tempfile.TemporaryDirectory(prefix="redasm-v2-") as directory:
            path = Path(directory) / "programa.rasm"
            path.write_text(source, encoding="utf-8")
            return self.run_cli(path, *args, reject_sleep=reject_sleep)

    def test_output_only_golden_and_no_sleeps_even_with_tick_rate(self):
        proc = self.run_source(
            "PRINT 0\nTICKDELAY 2\nPRINT 0b1111\nOUT 0, 7\n"
            "ADD DUST0, 2, 3\nPRINT DUST0\nHALT",
            "--output-only", "--tick-rate", "9999", reject_sleep=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "0\n15\n5\n")
        self.assertEqual(proc.stderr, "")

    def test_output_only_without_print_is_empty(self):
        proc = self.run_source("OUT 0, 15\nTICKDELAY 1\nHALT", "--output-only", reject_sleep=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def test_check_compiles_but_does_not_execute_or_sleep(self):
        proc = self.run_source(
            ".CONST VALUE, 13\nPRINT VALUE\nTICKDELAY 2\nDIV DUST0, 1, 0\nHALT",
            "--check", reject_sleep=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("FINAL", proc.stdout)
        self.assertNotIn("DUST0", proc.stdout)
        self.assertNotIn("13", proc.stdout.splitlines())
        self.assertNotIn("value=13", proc.stdout)

    def test_repeated_inputs_accept_decimal_hex_binary_last_wins(self):
        proc = self.run_source(
            "IN DUST0, 0\nPRINT DUST0\nIN DUST0, 1\nPRINT DUST0\n"
            "IN DUST0, 15\nPRINT DUST0\nHALT",
            "--output-only", "--input", "0=1", "--input", "0x0=0b111",
            "--input", "0b1=0xA", "--input", "15=0b1111",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "7\n10\n15\n")
        self.assertEqual(proc.stderr, "")

    def test_invalid_input_flags_exit_2(self):
        for value in ("0", "=1", "0=", "0=1=2", "x=1", "0=x", "16=0", "-1=0",
                      "0=16", "0=-1", "0=1.5", "0=0b2", "0x10=1"):
            with self.subTest(value=value):
                proc = self.run_source("HALT", "--output-only", f"--input={value}")
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("--input", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_output_only_incompatible_flags_exit_2(self):
        for flag in ("--trace", "--check", "--dump-memory"):
            with self.subTest(flag=flag):
                proc = self.run_source("PRINT 3\nHALT", "--output-only", flag)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("--output-only", proc.stderr)
                self.assertIn(flag, proc.stderr)

    def test_other_invalid_flags_exit_2(self):
        for flags in (("--ticks", "-1"), ("--tick-rate", "-1"), ("--unknown-v2",), ("--input",)):
            with self.subTest(flags=flags):
                proc = self.run_source("HALT", *flags)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn("Traceback", proc.stderr)

    def test_source_errors_exit_1_for_check_and_execution(self):
        for source in ("PRINT 0b2", "ADD DUST0, 16, 1", "CALL missing",
                       ".DATA 255, 1, 2", ".CONST A, 1\n.CONST A, 2"):
            for flag in ("--check", "--output-only"):
                with self.subTest(source=source, flag=flag):
                    proc = self.run_source(source, flag)
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertIn("programa.rasm:", proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)

    def test_runtime_errors_exit_1_with_source_line_and_no_summary(self):
        for instruction in ("DIV DUST0, 1, 0", "MOD DUST0, 1, 0", "POP DUST0", "RET"):
            with self.subTest(instruction=instruction):
                proc = self.run_source("PRINT 7\n" + instruction + "\nHALT", "--output-only")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(proc.stdout, "7\n")
                self.assertIn("programa.rasm:2", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_missing_file_exit_1_in_check_mode(self):
        with tempfile.TemporaryDirectory(prefix="redasm-v2-") as directory:
            proc = self.run_cli(Path(directory) / "missing.rasm", "--check")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("missing.rasm", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_dump_memory_lists_all_final_ram_cells(self):
        proc = self.run_source(
            ".DATA 0, 1, 2\n.DATA 255, 15\nSTORE 0, 9\nSTORE 254, 7\nHALT",
            "--dump-memory", "--max-speed", "--no-color", "--ascii", reject_sleep=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertIn("RAM", proc.stdout)
        rows = [line for line in proc.stdout.splitlines()
                if re.fullmatch(r"[0-9A-F]{2}(?: [0-9A-F]{2}){15}", line)]
        self.assertEqual(len(rows), 16)
        values = [int(value, 16) for row in rows for value in row.split()]
        self.assertEqual(values, [9, 2] + [0] * 252 + [7, 15])

    def test_example_output_only_golden(self):
        for name, expected, flags in (
            ("arithmetic.rasm", "7\n15\n14\n0\n", ()),
            ("memory.rasm", "2\n4\n6\n8\n", ()),
            ("subroutines.rasm", "6\n", ()),
            ("ports.rasm", "15\n", ("--input", "0=7", "--input", "1=5")),
        ):
            with self.subTest(example=name):
                proc = self.run_cli(ROOT / "examples" / name, "--output-only", *flags)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, expected)
                self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
