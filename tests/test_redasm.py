"""Testes automatizados do RedAsm (sem dependências externas).

Execute a partir da raiz do projeto:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli import render_frame  # noqa: E402
from errors import LexError, ParseError, VMError  # noqa: E402
from isa import Register  # noqa: E402
from lexer import TokenType, tokenize  # noqa: E402
from parser import compile_source  # noqa: E402
from vm import Frame, RedVM  # noqa: E402

R = Register


def run_source(source: str, max_ticks: int | None = None):
    """Compila e executa, devolvendo (vm, result, frames)."""
    program = compile_source(source, "<teste>")
    frames: list[Frame] = []
    vm = RedVM(program, max_ticks=max_ticks, on_tick=frames.append)
    result = vm.run()
    return vm, result, frames


def run_example(name: str, max_ticks: int | None = None):
    source = (ROOT / "examples" / name).read_text(encoding="utf-8")
    program = compile_source(source, name)
    frames: list[Frame] = []
    vm = RedVM(program, max_ticks=max_ticks, on_tick=frames.append)
    result = vm.run()
    return vm, result, frames


class LexerTests(unittest.TestCase):
    def test_tokens_basicos(self):
        tokens = tokenize("POWER DUST0, 15 # liga\n")
        kinds = [t.kind for t in tokens]
        self.assertEqual(
            kinds,
            [
                TokenType.INSTRUCTION,
                TokenType.REGISTER,
                TokenType.COMMA,
                TokenType.NUMBER,
                TokenType.NEWLINE,
                TokenType.EOF,
            ],
        )
        self.assertEqual(tokens[0].value, "POWER")
        self.assertEqual(tokens[1].value, R.DUST0)
        self.assertEqual(tokens[3].value, 15)

    def test_case_insensitive(self):
        tokens = tokenize("power dust3, 0xf\n")
        self.assertEqual(tokens[0].value, "POWER")
        self.assertEqual(tokens[1].value, R.DUST3)
        self.assertEqual(tokens[3].value, 15)

    def test_comentarios_e_rotulos(self):
        tokens = tokenize("# topo\nloop: JUMP loop\n")
        values = [t.value for t in tokens]
        self.assertIn("loop", values)
        self.assertIn(TokenType.COLON, [t.kind for t in tokens])

    def test_caractere_invalido(self):
        with self.assertRaises(LexError) as ctx:
            tokenize("POWER DUST0, @\n")
        self.assertIn("caractere inesperado", str(ctx.exception))
        self.assertEqual(ctx.exception.line, 1)

    def test_hex_incompleto(self):
        with self.assertRaises(LexError):
            tokenize("POWER DUST0, 0x\n")


class ParserTests(unittest.TestCase):
    def test_rotulos_resolvidos(self):
        program = compile_source("inicio:\n  POWER DUST0, 15\n  JUMP inicio\n")
        self.assertEqual(program.labels["inicio"], 0)
        self.assertEqual(program.instructions[1].args, (0,))

    def test_hex_aceito(self):
        program = compile_source("POWER DUST0, 0xF\nHALT\n")
        self.assertEqual(program.instructions[0].args[1], 15)

    def test_texto_sem_comentario(self):
        program = compile_source("POWER DUST0, 15 # liga\nHALT\n")
        self.assertEqual(program.instructions[0].text, "POWER DUST0, 15")

    def test_potencia_fora_do_intervalo(self):
        with self.assertRaises(ParseError) as ctx:
            compile_source("POWER DUST0, 16\n")
        self.assertIn("fora do intervalo", str(ctx.exception))

    def test_power_exige_dust(self):
        with self.assertRaises(ParseError) as ctx:
            compile_source("POWER TORCH, 15\n")
        self.assertIn("DUST0..DUST7", str(ctx.exception))

    def test_fade_exige_dust(self):
        with self.assertRaises(ParseError):
            compile_source("FADE COMPARATOR\n")

    def test_virgula_ausente(self):
        with self.assertRaises(ParseError) as ctx:
            compile_source("POWER DUST0 15\n")
        self.assertIn("esperava ','", str(ctx.exception))

    def test_operando_extra(self):
        with self.assertRaises(ParseError):
            compile_source("HALT DUST0\n")

    def test_rotulo_indefinido(self):
        with self.assertRaises(ParseError) as ctx:
            compile_source("JUMP destino\n")
        self.assertIn("não definido", str(ctx.exception))

    def test_rotulo_duplicado(self):
        with self.assertRaises(ParseError) as ctx:
            compile_source("loop:\nloop:\nHALT\n")
        self.assertIn("mais de uma vez", str(ctx.exception))

    def test_rotulo_reservado(self):
        with self.assertRaises(ParseError) as ctx:
            compile_source("fade:\nHALT\n")
        self.assertIn("reservada", str(ctx.exception))

    def test_tickdelay_zero_rejeitado(self):
        with self.assertRaises(ParseError):
            compile_source("TICKDELAY 0\n")

    def test_instrucao_desconhecida(self):
        with self.assertRaises(ParseError):
            compile_source("UNKNOWN DUST0, DUST1\n")


class VMTests(unittest.TestCase):
    def test_wire_invert_e_fade(self):
        vm, result, _ = run_source(
            "POWER DUST0, 15\n"
            "WIRE DUST1, DUST0\n"
            "INVERT DUST2, DUST1\n"
            "FADE DUST0\n"
            "HALT\n"
        )
        self.assertTrue(result.halted)
        self.assertEqual(vm.registers[R.DUST0], 14)
        self.assertEqual(vm.registers[R.DUST1], 15)
        self.assertEqual(vm.registers[R.DUST2], 0)

    def test_compare_e_subtract(self):
        vm, _, _ = run_source(
            "POWER DUST1, 9\n"
            "POWER DUST2, 4\n"
            "COMPARE DUST0, DUST1, DUST2\n"
            "COMPARE DUST3, DUST2, DUST1\n"
            "SUBTRACT DUST4, DUST1, DUST2\n"
            "SUBTRACT DUST5, DUST2, DUST1\n"
            "HALT\n"
        )
        self.assertEqual(vm.registers[R.DUST0], 9)
        self.assertEqual(vm.registers[R.DUST3], 0)
        self.assertEqual(vm.registers[R.DUST4], 5)
        self.assertEqual(vm.registers[R.DUST5], 0)

    def test_compare_igualdade_mantem_main(self):
        vm, _, _ = run_source(
            "POWER DUST1, 7\nPOWER DUST2, 7\nCOMPARE DUST0, DUST1, DUST2\nHALT\n"
        )
        self.assertEqual(vm.registers[R.DUST0], 7)

    def test_fade_nao_fica_negativo(self):
        vm, _, _ = run_source("POWER DUST0, 2\nFADE DUST0\nFADE DUST0\nFADE DUST0\nHALT\n")
        self.assertEqual(vm.registers[R.DUST0], 0)

    def test_saltos_condicionais(self):
        vm, _, _ = run_source(
            "POWER DUST0, 1\n"
            "JZ DUST0, errado\n"
            "JNZ DUST0, certo\n"
            "POWER DUST1, 15\n"
            "errado:\n"
            "POWER DUST1, 1\n"
            "certo:\n"
            "JUMP fim\n"
            "POWER DUST2, 15\n"
            "fim:\n"
            "HALT\n"
        )
        self.assertEqual(vm.registers[R.DUST0], 1)
        self.assertEqual(vm.registers[R.DUST1], 0)
        self.assertEqual(vm.registers[R.DUST2], 0)

    def test_jz_tomado(self):
        vm, _, _ = run_source(
            "POWER DUST0, 0\nJZ DUST0, zero\nPOWER DUST1, 15\nzero:\nHALT\n"
        )
        self.assertEqual(vm.registers[R.DUST1], 0)

    def test_torch_inverte_na_escrita(self):
        vm, _, _ = run_source(
            "POWER DUST0, 7\n"
            "WIRE TORCH, DUST0\n"
            "WIRE DUST1, TORCH\n"
            "POWER DUST0, 0\n"
            "WIRE TORCH, DUST0\n"
            "WIRE DUST2, TORCH\n"
            "HALT\n"
        )
        self.assertEqual(vm.registers[R.DUST1], 0)
        self.assertEqual(vm.registers[R.DUST2], 15)

    def test_invert_em_torch_inverte_duas_vezes(self):
        vm, _, _ = run_source("POWER DUST0, 7\nINVERT TORCH, DUST0\nHALT\n")
        # INVERT converte 7 em 0; a escrita na tocha inverte 0 para 15.
        self.assertEqual(vm.registers[R.TORCH], 15)

    def test_repeater_atrasa_um_tick(self):
        vm, _, frames = run_source(
            "POWER DUST0, 4\n"
            "WIRE REPEATER, DUST0\n"
            "TICKDELAY 1\n"
            "WIRE DUST1, REPEATER\n"
            "TICKDELAY 1\n"
            "HALT\n"
        )
        self.assertEqual(frames[0].registers[R.REPEATER], 0)
        self.assertEqual(frames[1].registers[R.REPEATER], 4)
        self.assertEqual(frames[1].registers[R.DUST1], 4)
        self.assertEqual(vm.registers[R.DUST1], 4)

    def test_limite_de_ticks(self):
        _, result, frames = run_source(
            "loop:\nINVERT DUST0, DUST0\nTICKDELAY 1\nJUMP loop\n", max_ticks=3
        )
        self.assertEqual(len(frames), 3)
        self.assertEqual(result.tick, 3)
        self.assertFalse(result.halted)
        self.assertIn("limite de 3 ticks", result.reason)
        self.assertEqual([f.registers[R.DUST0] for f in frames], [15, 0, 15])

    def test_loop_sem_delay_estoura_orcamento(self):
        with self.assertRaises(VMError) as ctx:
            run_source("loop:\nJUMP loop\n")
        self.assertIn("orçamento", str(ctx.exception))

    def test_meta_do_comparador(self):
        vm, _, _ = run_source(
            "POWER DUST1, 12\nPOWER DUST2, 5\n"
            "COMPARE COMPARATOR, DUST1, DUST2\nHALT\n"
        )
        self.assertEqual(vm.snapshot().comparator, (12, 5, "COMPARE"))

    def test_meta_do_comparador_e_limpa_por_wire(self):
        vm, _, _ = run_source(
            "POWER DUST1, 12\nPOWER DUST2, 5\n"
            "COMPARE COMPARATOR, DUST1, DUST2\n"
            "WIRE COMPARATOR, DUST1\nHALT\n"
        )
        self.assertIsNone(vm.snapshot().comparator)

    def test_fim_implicito_sem_halt(self):
        _, result, _ = run_source("POWER DUST0, 3\n")
        self.assertFalse(result.halted)
        self.assertEqual(result.reason, "fim do programa")


class RenderTests(unittest.TestCase):
    def test_render_ascii(self):
        frame = Frame(
            tick=2,
            pc=1,
            registers={reg: 0 for reg in Register},
            pending=0,
            comparator=None,
            last="POWER DUST0, 15",
        )
        frame.registers[R.DUST0] = 15
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            render_frame(frame, source_name="teste.rasm", color=False, ascii_mode=True)
        output = buffer.getvalue()
        self.assertIn("TICK  0002", output)
        self.assertIn("DUST0      [15]: ###############", output)
        self.assertIn("último: POWER DUST0, 15", output)


class ExampleTests(unittest.TestCase):
    def test_clock_alterna_15_e_0(self):
        _, result, frames = run_example("clock.rasm", max_ticks=8)
        self.assertEqual(result.tick, 8)
        self.assertEqual([f.registers[R.DUST0] for f in frames], [15, 15, 0, 0, 15, 15, 0, 0])
        self.assertEqual([f.registers[R.TORCH] for f in frames], [0, 0, 15, 15, 0, 0, 15, 15])

    def test_rslatch_retem_estado_e_termina_em_reset(self):
        vm, result, frames = run_example("rslatch.rasm")
        self.assertTrue(result.halted)
        self.assertEqual(vm.registers[R.DUST0], 0)
        self.assertEqual(vm.registers[R.DUST1], 0)
        self.assertEqual(vm.registers[R.DUST2], 0)
        # Q=15 deve sobreviver com as entradas em 0 após o pulso de SET.
        self.assertTrue(
            any(f.registers[R.DUST0] == 15 and f.registers[R.DUST1] == 0
                for f in frames)
        )

    def test_fader_varre_15_ate_0(self):
        vm, _, frames = run_example("fader.rasm", max_ticks=40)
        values = [f.registers[R.DUST0] for f in frames]
        self.assertEqual(max(values), 15)
        self.assertEqual(min(values), 0)
        self.assertIn(7, values)
        for frame in frames:
            if frame.registers[R.DUST0] < 15:
                self.assertEqual(frame.registers[R.DUST1], frame.registers[R.DUST0])

    def test_comparator_extra(self):
        vm, result, _ = run_example("comparator.rasm")
        self.assertTrue(result.halted)
        self.assertEqual(vm.registers[R.DUST0], 0)
        self.assertEqual(vm.registers[R.DUST3], 0)
        self.assertEqual(vm.registers[R.DUST4], 5)
        self.assertEqual(vm.snapshot().comparator, (4, 9, "COMPARE"))


class CliTests(unittest.TestCase):
    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "redvm.py"), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            check=False,
        )

    def test_executa_clock_em_max_speed(self):
        proc = self._run_cli(
            "examples/clock.rasm", "--ticks", "3", "--max-speed", "--no-color"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("FINAL", proc.stdout)
        self.assertIn("limite de 3 ticks", proc.stdout)
        self.assertIn("DUST0", proc.stdout)

    def test_erro_de_sintaxe_retorna_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalido.rasm"
            path.write_text("POWER DUST0, 99\n", encoding="utf-8")
            proc = self._run_cli(str(path), "--max-speed")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("fora do intervalo", proc.stderr)

    def test_arquivo_inexistente_retorna_1(self):
        proc = self._run_cli("examples/nao_existe.rasm", "--max-speed")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("não foi possível ler", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
