# RedAsm — Redstone Assembly

Linguagem assembly de 4 bits inspirada em redstone, agora com **38 instruções**,
RAM, pilhas, sub-rotinas, operações aritméticas/bit a bit e portas de E/S.
Python 3.11+; nenhuma dependência externa.

Os sinais continuam entre **0 e 15**. Tochas invertem, repetidores atrasam
um tick e comparadores comparam/subtraem. Os circuitos existentes e o
[RedEngine 2D](../redengine/README.md) continuam funcionando.

[Especificação completa](REDASM_SPEC.md)

## Executar

Na raiz do workspace, sem instalar pacotes:

```bash
python redasm/redvm.py redasm/examples/arithmetic.rasm --output-only
python redasm/redvm.py redasm/examples/memory.rasm --max-speed --dump-memory
python redasm/redvm.py redasm/examples/subroutines.rasm --output-only
python redasm/redvm.py redasm/examples/ports.rasm --input 0=7 --input 1=5 --output-only
```

Resultados: aritmética imprime `7, 15, 14, 0` (um por linha); memória imprime
`2, 4, 6, 8`; recursão imprime `6`; portas imprime `15` para as entradas 7 e 5.

Dentro da pasta `redasm`, continua válido:

```bash
python redvm.py examples/clock.rasm --ticks 16
python redvm.py examples/fader.rasm --ticks 40 --tick-rate 60
python redvm.py examples/subroutines.rasm --check
python redvm.py --help
```

## Novidades

| Área | Recursos |
|---|---|
| Aritmética | `ADD`, `MUL`, `DIV`, `MOD`, `INC`, além de `SUBTRACT`/`FADE` |
| Bits | `AND`, `OR`, `XOR`, `NOT`, `SHL`, `SHR` |
| Comparações | `JE`, `JNE`, `JLT`, `JLE`, `JGT`, `JGE` sem flags |
| Memória | 256 células de 4 bits, `LOAD`/`STORE`, endereços diretos/indiretos |
| Sub-rotinas | `CALL`/`RET`, recursão e pilha de retorno separada |
| Pilha de dados | `PUSH`/`POP`, 64 nibbles |
| Entrada/saída | 16 portas de entrada e 16 de saída, `IN`/`OUT`, `PRINT` |
| Montagem | `.CONST`, `.DATA`, binários, rótulos inline, comentários `;` ou `#` |
| CLI | `--check`, `--input`, `--output-only`, `--dump-memory` |

Também há `NOP`. `WIRE`, `INVERT`, `COMPARE` e `SUBTRACT` aceitam imediatos
como origem; `JZ`/`JNZ` aceitam os registradores de componentes.

## Um programa completo

```asm
.CONST COUNT, 4
.DATA 0xA0, 2, 4, 6, 8

POWER DUST1, 0xA
POWER DUST2, 0
loop:
    LOAD DUST0, [DUST1:DUST2]
    CALL emit
    INC DUST2
    JLT DUST2, COUNT, loop
HALT

emit:
    OUT 0, DUST0
    PRINT DUST0
    TICKDELAY 1
    RET
```

A tabela ocupa RAM `0xA0..0xA3`. `[DUST1:DUST2]` calcula o endereço como
`DUST1*16 + DUST2`. Cada chamada escreve a porta 0, imprime um valor e espera
um tick. `.CONST` e `.DATA` são resolvidas antes da execução e não consomem PCs.

Para memória direta, use `STORE 255, 9` / `LOAD DUST0, 255`. `[DUST0]` acessa
as primeiras 16 células; `[high:low]` permite acessar todas as 256.

## Regras importantes

- **Saturação:** `ADD DUST0, 12, 7` produz 15, não 3; subtração nunca fica negativa.
- **Bits:** `SHL DUST0, 15, 1` produz 14; bits altos são descartados.
- **Duas inversões diferentes:** `NOT DUST0, 3` produz 12; `INVERT DUST0, 3` produz 0.
- **Componentes valem para toda escrita:** `LOAD TORCH, 0` inverte o valor lido
  da RAM; `POP REPEATER` só será visível depois de uma virada de tick.
- **Igualdade:** use `JE a, b, destino`; subtração saturada igual a zero não
  distingue igualdade de `a < b`.
- **Chamadas:** até 64 níveis; retornos são PCs inteiros, não nibbles. Registros
  não são preservados automaticamente; use `PUSH`/`POP` quando necessário.
- **Erros explícitos:** divisão por zero, pilha vazia/cheia, memória inválida,
  constantes duplicadas e sobreposição de `.DATA` têm diagnóstico localizado.
- **Tempo:** somente `TICKDELAY` avança ticks; há orçamento contra loops sem espera.

Mnemônicos/registradores/diretivas não diferenciam caixa. Constantes e rótulos
diferenciam. Literais aceitam `15`, `0xF`, `0b1111`. Novos mnemônicos são
reservados: renomeie um rótulo antigo chamado `add` ou `call`, por exemplo.

## Integrar com Python

Execute este trecho com `redasm` como diretório de trabalho:

```python
from parser import compile_source
from vm import RedVM

source = "IN DUST0, 0\nADD DUST0, DUST0, 1\nOUT 1, DUST0\nPRINT DUST0\nHALT"
events = []
vm = RedVM(compile_source(source), on_output=events.append)
vm.set_input(0, 6)
vm.run()
print(vm.outputs[1])       # 7
print(events[0].value)     # 7
```

`IN` lê uma porta sem consumir seu valor; `OUT` altera um latch independente.
`PRINT` emite `OutputEvent(tick, value)`: a VM não imprime nem acumula histórico.
Para controlar o relógio, use `step_tick()` e atualize entradas entre chamadas
com `set_input()` ou `inject()`. Não misture `run()` e stepping na mesma VM.

`vm.snapshot()` oferece cópia dos registradores, tuplas de memória, pilha e
portas, profundidade de chamadas e estado dos componentes. A API continua
adequada a testes, jogos e ferramentas sem depender de um terminal.

## Ferramentas de terminal

- `--check`: apenas monta e valida; não executa instruções.
- `--input PORT=VALUE`: entrada inicial, repetível; decimal/hex/binário, 0..15.
- `--output-only`: somente PRINT, um decimal por linha, sem animação ou sumário.
- `--dump-memory`: RAM final em 16 linhas de 16 valores hexadecimais.
- `--trace`: tick/PC/instrução; `--max-speed`: sem espera, painel final.
- `--ticks N`: padrão 64; zero ilimitado. Na API, use `max_ticks=None` para ilimitado.
- `--tick-rate MS`: padrão 100 ms; `--ascii`, `--no-color`, `--no-clear` controlam o painel.

`--output-only` não combina com `--check`, `--trace` ou `--dump-memory`.
`OUT` não imprime; para saída no terminal, use `PRINT`. Com PRINT, a CLI não
limpa a tela para evitar apagar resultados. Sem TTY, não emite cores ANSI.
O limite de ticks também encerra com código 0; inspecione o sumário no modo
normal para diferenciar limite de `HALT`.

## Exemplos disponíveis

| Programa | Demonstração |
|---|---|
| [clock.rasm](examples/clock.rasm) | Oscilador e inversão de tocha |
| [rslatch.rasm](examples/rslatch.rasm) | Memória de um bit com SET/RESET |
| [fader.rasm](examples/fader.rasm) | Atenuação de sinal por tick |
| [comparator.rasm](examples/comparator.rasm) | Comparação e subtração analógicas |
| [arithmetic.rasm](examples/arithmetic.rasm) | Soma, multiplicação, bits e saturação |
| [memory.rasm](examples/memory.rasm) | Tabela inicializada e cópia indireta de RAM |
| [subroutines.rasm](examples/subroutines.rasm) | Soma recursiva com duas pilhas |
| [ports.rasm](examples/ports.rasm) | Entradas externas e trava de combinação |

## Testes

Na raiz do workspace:

```bash
python -m unittest discover -s redasm/tests -v
python -m unittest discover -s redengine/tests -v
```

**103 testes do RedAsm + 27 do RedEngine**, com subcasos exaustivos sobre os
valores de 4 bits. Cobertura dos circuitos antigos, parser, memória, pilhas,
recursão, E/S, snapshots, stepping, erros e execução real da CLI.

## Limitações

Sem strings, floats, números negativos, macros, includes, interrupções,
bytecode ou acesso direto a arquivos/rede. Dados maiores devem ser representados
com múltiplos nibbles. RAM fixa em 256 células; pilhas limitadas a 64 entradas.
A execução é single-thread e determinística para a mesma sequência de entradas.
