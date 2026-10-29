# REDASM SPEC v2.0 — Minecraft Edition

RedAsm é uma linguagem assembly de sinais de 4 bits, com memória, sub-rotinas,
E/S e tempo explícito. A implementação de referência usa Python 3.11+ e apenas
a biblioteca padrão. Executa AST, não bytecode, e não é um mod de Minecraft.

## 1. Modelo de máquina

| Recurso | Capacidade / estado inicial |
|---|---|
| Fios | `DUST0..DUST7`, todos em 0 |
| Componentes | `TORCH`, `COMPARATOR`, `REPEATER`, todos em 0 |
| Dados | Inteiros sem sinal de 4 bits: 0..15 |
| RAM | 256 células de 4 bits, inicializadas com zero ou `.DATA` |
| Pilha de dados | Até 64 nibbles; inicialmente vazia |
| Pilha de chamadas | Até 64 endereços de retorno; inicialmente vazia |
| Portas | 16 de entrada e 16 de saída, todas em 0 |
| Controle | `pc=0`, `tick=0`, `halted=False` |
| Orçamento | 100.000 instruções por tick |

**Dados e endereços são coisas distintas.** Endereços de RAM ocupam 8 bits
(0..255). PCs são índices de instrução e não são truncados para 4 bits.
A pilha de chamadas guarda PCs inteiros, separadamente da pilha de dados.
Registradores, RAM, pilha de dados e valores das portas continuam sendo nibbles.

`ADD`, `MUL` e `INC` saturam em 15; `SUBTRACT` e `FADE` saturam em 0.
Não há flags de carry, borrow ou overflow. Operações bit a bit atuam somente
nos quatro bits; `SHL` descarta bits altos, **não satura**.

## 2. Sintaxe e montagem

```ebnf
program      = { line } ;
line         = [ statement ] [ comment ] newline ;
statement    = directive | [ label ] [ instruction ] ;
label        = IDENT ":" ;
instruction  = MNEMONIC [ operand { "," operand } ] ;
operand      = REGISTER | NUMBER | IDENT | indirect ;
indirect     = "[" REGISTER [ ":" REGISTER ] "]" ;
directive    = ".CONST" IDENT "," NUMBER
             | ".DATA" scalar "," scalar { "," scalar } ;
scalar       = NUMBER | IDENT ;
comment      = ("#" | ";") { any_char_except_newline } ;
```

- Uma instrução por linha; vírgulas obrigatórias entre operandos.
- Mnemônicos, registradores e diretivas não diferenciam maiúsculas/minúsculas.
- Rótulos e constantes **diferenciam caixa**. Não podem coincidir entre si,
  ser duplicados ou usar nomes reservados de instruções/registradores.
- Rótulos podem estar sozinhos ou antes de uma instrução: `loop: INC DUST0`.
  Uma linha admite apenas um rótulo. Não se coloca rótulo antes de diretiva.
- Saltos e chamadas aceitam rótulos definidos antes ou depois do uso.
- Um rótulo após a última instrução aponta ao fim do programa.
- Literais: decimal `15`, hexadecimal `0xF`, binário `0b1111` (prefixos também
  em maiúsculas). Não há números negativos, strings ou expressões aritméticas.
- `;` inicia comentário; não separa duas instruções na mesma linha.

### 2.1 Constantes

```asm
POWER DUST0, MAX_LEVEL
TICKDELAY WAIT_TICKS
HALT
.CONST MAX_LEVEL, 0b1111
.CONST WAIT_TICKS, 2
```

`.CONST NOME, literal` define um inteiro entre 0 e 10.000. A definição pode
aparecer depois do uso. Não aceita outra constante nem expressão como valor.
O intervalo é validado novamente no uso: uma constante 200 é válida como
endereço, mas inválida como potência. Constantes não ocupam registradores,
RAM, PCs ou ticks. Não podem ser usadas como alvos de salto.

### 2.2 Inicialização da RAM

```asm
.CONST TABLE, 0xA0
.DATA TABLE, 2, 4, 6, 8
LOAD DUST0, TABLE
HALT
```

`.DATA endereço, valor[, valor...]` inicializa células consecutivas na
montagem. Endereço e valores podem usar constantes. O endereço deve ficar
em 0..255, cada valor em 0..15 e o bloco inteiro deve caber na RAM.
Blocos sobrepostos são erro, mesmo se os valores forem iguais. As demais
células começam em zero. Cada VM recebe sua própria cópia da imagem inicial.
A posição da diretiva no arquivo não altera o momento da inicialização.

## 3. Operandos e componentes

Na tabela de instruções:

- `dust`: somente `DUST0..DUST7`.
- `dest`, `reg`: qualquer um dos 11 registradores.
- `value`, `a`, `b`: registrador, literal 0..15 ou constante nessa faixa.
- `imm`: literal/constante 0..15, nunca registrador.
- `label`: rótulo de código resolvido na montagem.
- `address`: literal/constante 0..255 ou indireção descrita na seção 5.
- `port`: mesmo formato de `value`, selecionando uma porta 0..15.

Todas as origens são lidas antes da escrita. `ADD DUST0, DUST0, 1` é seguro.
Todas as instruções com destino registrador passam pela mesma regra elétrica:

| Destino | Efeito da escrita |
|---|---|
| `DUST0..DUST7` | Guarda o valor limitado a 0..15 |
| `TORCH` | Guarda 15 quando recebe 0; guarda 0 quando recebe 1..15 |
| `REPEATER` | Agenda o valor; a última escrita vence na próxima virada de tick |
| `COMPARATOR` | Guarda o valor; só `COMPARE`/`SUBTRACT` preservam metadados |

Isso também vale para `ADD`, `LOAD`, `POP` e `IN`, não apenas para `WIRE`.
`POWER`, `INC` e `FADE` exigem destino `dust`.

`INVERT` é a inversão de redstone: qualquer sinal positivo vira 0, zero vira 15.
`NOT` é inversão bit a bit: `NOT DUST0, 3` produz 12.
`INVERT TORCH, 7` aplica duas inversões e produz 15.
`NOT TORCH, 3` calcula 12, mas a transformação da tocha produz 0.

`COMPARE COMPARATOR, a, b` guarda `(a,b,"COMPARE")` para inspeção;
`SUBTRACT COMPARATOR, a, b` guarda `(a,b,"SUBTRACT")`. Qualquer outra escrita
nesse componente limpa os metadados. Não há comparação automática ao lê-lo.

## 4. ISA completa — 38 instruções

### 4.1 Sinais, aritmética e lógica

| Instrução | Resultado antes da transformação do destino |
|---|---|
| `POWER dust, imm` | `imm` |
| `WIRE dest, value` | Cópia do valor |
| `INVERT dest, value` | `0` se positivo, senão `15` |
| `COMPARE dest, a, b` | `a` se `a >= b`, senão `0` |
| `SUBTRACT dest, a, b` | `max(0, a-b)` |
| `ADD dest, a, b` | `min(15, a+b)` |
| `MUL dest, a, b` | `min(15, a*b)` |
| `DIV dest, a, b` | Quociente inteiro `a // b` |
| `MOD dest, a, b` | Resto `a % b` |
| `INC dust` | `min(15, dust+1)` |
| `FADE dust` | `max(0, dust-1)` |
| `AND dest, a, b` | `a & b` |
| `OR dest, a, b` | `a \| b` |
| `XOR dest, a, b` | `a ^ b` |
| `NOT dest, value` | `value ^ 15` |
| `SHL dest, a, b` | `(a << b) & 15` |
| `SHR dest, a, b` | `a >> b` |

`DIV`/`MOD` com divisor zero geram `VMError` com arquivo e linha, sem alterar
o destino. Deslocamentos aceitam 0..15 posições; com quatro ou mais, o
resultado é zero. `SHL DUST0, 15, 1` produz 14, enquanto `ADD DUST0, 15, 1`
produz 15. Não existem `DEC` ou `SUB`: use `FADE` e `SUBTRACT`.

### 4.2 Fluxo e tempo

| Instrução | Efeito |
|---|---|
| `JUMP label` | Salta incondicionalmente |
| `JZ reg, label` | Salta se o registrador for zero |
| `JNZ reg, label` | Salta se o registrador for positivo |
| `JE a, b, label` | Salta se `a == b` |
| `JNE a, b, label` | Salta se `a != b` |
| `JLT a, b, label` | Salta se `a < b` |
| `JLE a, b, label` | Salta se `a <= b` |
| `JGT a, b, label` | Salta se `a > b` |
| `JGE a, b, label` | Salta se `a >= b` |
| `CALL label` | Empilha o PC seguinte e salta |
| `RET` | Desempilha o PC de retorno |
| `NOP` | Não altera estado além do PC/contador de instruções |
| `TICKDELAY ticks` | Espera de 1 a 10.000 ticks; literal/constante |
| `HALT` | Encerra imediatamente; marca `halted=True` |

Saltos relacionais comparam valores diretamente e não modificam registradores,
flags ou metadados do comparador. `JE` distingue corretamente 0 de 0, ao
contrário de inferir igualdade a partir da saída de um comparador de redstone.
`SUBTRACT a,b == 0` significa `a <= b`, **não** `a == b`.

### 4.3 Memória, pilhas e E/S

| Instrução | Efeito |
|---|---|
| `LOAD dest, address` | Lê RAM e escreve no destino |
| `STORE address, value` | Grava nibble na RAM |
| `PUSH value` | Empilha nibble na pilha de dados |
| `POP dest` | Retira o topo e escreve no destino |
| `IN dest, port` | Lê a porta de entrada sem consumi-la |
| `OUT port, value` | Atualiza o latch da porta de saída |
| `PRINT value` | Emite um evento de saída numérico para o host |

## 5. Endereçamento

```asm
STORE 255, 9
LOAD DUST0, 0xFF
POWER DUST1, 4
STORE [DUST1], DUST0
POWER DUST2, 0xF
POWER DUST3, 0xF
LOAD DUST4, [DUST2:DUST3]
```

- `255`/`0xFF`/constante: endereço direto de 8 bits.
- `[DUST1]`: endereço igual ao nível de DUST1 (primeiras 16 células).
- `[DUST2:DUST3]`: `DUST2 * 16 + DUST3`, cobrindo toda a RAM.
- Dentro de colchetes só entram registradores, incluindo componentes especiais.
- Um registrador sem colchetes não é endereço válido.
- `LOAD DUST0, [DUST0]` usa o valor antigo de DUST0 como endereço.
- `STORE` não tem transformação de componente: RAM guarda o nibble lido.
- A RAM não contém código; não há auto-modificação nem salto indireto.

## 6. Sub-rotinas e recursão

```asm
POWER DUST0, 3
CALL sum_down
PRINT DUST2
HALT

sum_down:
    JZ DUST0, done
    PUSH DUST0
    FADE DUST0
    CALL sum_down
    POP DUST1
    ADD DUST2, DUST2, DUST1
done: RET
```

O exemplo imprime 6 (3+2+1). As pilhas são LIFO e independentes: `PUSH`/`POP`
não podem sobrescrever endereços de retorno. Não há preservação automática
de registradores: a sub-rotina deve salvar o que precisar. `TICKDELAY` dentro
de uma sub-rotina preserva ambas as pilhas entre ticks.

`POP` ou `RET` sem itens e tentativa de exceder 64 itens geram `VMError`.
Recursão é suportada dentro desse limite. `HALT` termina inclusive dentro
de uma chamada; não executa retornos pendentes. Chegar ao fim do código
termina naturalmente, sem retorno implícito.

## 7. Portas e saída

As entradas são latches independentes das saídas. `IN` não espera teclado,
não remove valores e lê zero em portas que o host ainda não definiu.
`OUT` não altera a entrada da mesma porta e não escreve no terminal.
O último valor de saída permanece disponível em `vm.outputs[port]`.

```python
from parser import compile_source
from vm import RedVM

source = "IN DUST0, 0\nADD DUST0, DUST0, 1\nOUT 2, DUST0\nPRINT DUST0\nHALT"
events = []
vm = RedVM(compile_source(source), on_output=events.append)
vm.set_input(0, 6)
vm.run()
assert vm.outputs[2] == 7
assert events[0].value == 7
assert events[0].tick == 0
```

`set_input(port, level)` exige inteiros 0..15: tipo inválido gera `TypeError`,
faixa inválida gera `ValueError`. O host pode atualizar entradas entre ticks,
ou no callback `on_tick` de `run()` antes de avançar o relógio.

`PRINT` chama `on_output(OutputEvent(tick, value))` no instante de execução.
A VM não imprime e não acumula histórico; sem callback, o evento é descartado.
A CLI instala um callback que imprime `OUT t=0000 value=7`; com `--output-only`,
imprime apenas `7`. Erros no callback pertencem ao host e são propagados.
Não há acesso direto a arquivos, rede ou chamadas de sistema na linguagem.

## 8. Tempo, stepping e segurança de execução

Instruções são instantâneas no tick atual; só `TICKDELAY` avança o relógio.
Em `TICKDELAY N`, a VM repete N vezes:

1. captura o estado do tick corrente;
2. entrega esse frame a `on_tick` no modo `run()`;
3. incrementa `tick`;
4. confirma a última escrita agendada do `REPEATER`.

Depois retoma o programa no tick T+N. A escrita no repetidor feita em T aparece
na leitura em T+1. `HALT` não confirma escritas pendentes: exige virada de tick.
O frame T contém o estado antes dessa virada. A CLI mostra também o estado final.

```python
from isa import Register
from parser import compile_source
from vm import RedVM

vm = RedVM(compile_source("IN DUST0, 0\nTICKDELAY 1\nHALT"))
vm.set_input(0, 9)
vm.inject(Register.DUST1, 7)
step = vm.step_tick()
assert step.frame.registers[Register.DUST0] == 9
assert step.ticks_advanced == 1
assert vm.step_tick().ended
```

`step_tick()` devolve `StepResult(frame, ticks_advanced, ended)`. Durante uma
espera longa avança apenas um tick por chamada. `on_tick` não é chamado nesse
modo; `on_execute` e `on_output` continuam ativos. Ao alcançar HALT/fim/limite,
retorna `ended=True`, `ticks_advanced=0` e um snapshot final.

`inject(reg, level)` continua disponível para o RedEngine; exige registrador e
nível inteiro, satura em 0..15 e respeita os componentes. Diferentemente de
`set_input`, escreve diretamente num registrador, não numa porta.

Não se mistura `run()` e `step_tick()` na mesma VM em nenhuma ordem, nem se
reentra na execução a partir de callbacks. Ambos os modos respeitam
`max_ticks`: ao atingir o limite, não executam instruções do tick seguinte.
Na API, `max_ticks=0` não executa nada; `None` é ilimitado. Na CLI, por convenção,
`--ticks 0` significa ilimitado e o padrão é 64.

Cada tick tem orçamento de 100.000 instruções para detectar loops sem espera.
A VM é single-thread e determinística para o mesmo programa e a mesma sequência
de entradas. O tick-rate em milissegundos controla apenas a reprodução na CLI.

### 8.1 Inspeção

- `vm.registers`: estado dos 11 componentes.
- `vm.memory`: RAM de 256 nibbles; `vm.stack`: pilha de dados.
- `vm.call_stack`: PCs de retorno; `vm.inputs` / `vm.outputs`: portas.
- `vm.snapshot()`: frame com cópia de registradores, tuplas de RAM/pilha/portas,
  profundidade de chamadas (`call_depth`), PC, tick, repetidor e comparador.
- `vm.reason`: motivo de término, ou `None` enquanto ativo.
- `RunResult`: tick, halted, reason e total de instruções executadas.

Snapshots são independentes do estado vivo. O dicionário `frame.registers`
é uma cópia, não um mapping profundamente imutável. Para entradas externas use
`inject`/`set_input`; alterar diretamente as listas/dicionários da VM pode violar
as invariantes e não é uma API de entrada validada.

## 9. CLI

A partir da raiz deste workspace:

```bash
python redasm/redvm.py redasm/examples/arithmetic.rasm --output-only
python redasm/redvm.py redasm/examples/memory.rasm --max-speed --dump-memory
python redasm/redvm.py redasm/examples/subroutines.rasm --check
python redasm/redvm.py redasm/examples/ports.rasm --input 0=7 --input 1=5 --output-only
```

| Opção | Comportamento |
|---|---|
| `--check` | Monta e valida, informa instruções/constantes; não executa |
| `--input PORT=VALUE` | Define uma entrada; repetível, última definição vence |
| `--output-only` | Somente valores de PRINT, um decimal por linha; sem pausas |
| `--dump-memory` | Acrescenta a RAM final em 16 linhas de 16 valores hexadecimais |
| `--ticks N` | Limite de ticks; padrão 64, zero ilimitado |
| `--tick-rate MS` | Milissegundos por tick; padrão 100, zero máxima velocidade |
| `--max-speed` | Sem pausas/animação, mostra frame final; PRINT continua ativo |
| `--trace` | Mostra tick, PC e texto de cada instrução |
| `--no-color` | Desativa cores |
| `--no-clear` | Não limpa a tela entre frames |
| `--ascii` | Barras com `#`/`-`, em vez de blocos Unicode |
| `--version` | Versão |

Valores de `--input` aceitam decimal, hexadecimal e binário. `--output-only`
não pode ser combinado com `--check`, `--trace` ou `--dump-memory`.
`--check` valida inclusive fontes só com diretivas; executar uma fonte sem
instruções na CLI é erro. Ele não detecta erros que dependem de execução,
como divisão por zero ou pilha vazia.

Quando existe PRINT no programa, a CLI não limpa a tela, para preservar a saída.
Sem TTY, a renderização não usa cores nem limpeza de tela. O painel de barras
continua focado nos registradores; RAM é inspecionada por `--dump-memory` ou API.
A saída exclusiva não inclui o motivo de término; use o modo normal para
inspecionar se o programa chegou a HALT ou ao limite de ticks.

Códigos: **0** sucesso (inclui limite de ticks), **1** erro de arquivo/linguagem,
**2** argumentos inválidos, **130** Ctrl+C. Erros de linguagem incluem arquivo,
linha e, quando disponível, coluna. A VM não executa comandos do sistema.

## 10. Compatibilidade e limites

Os programas de exemplo v1 e o RedEngine continuam usando os mesmos
registradores, componentes e semântica de ticks. `POWER`/`FADE` mantêm suas
restrições de destino. `WIRE`, `INVERT`, `COMPARE` e `SUBTRACT` agora aceitam
imediatos, e `JZ`/`JNZ` também aceitam registradores de componentes.

Novos mnemônicos são palavras reservadas: um rótulo antigo chamado `add`, `in`
ou `call`, por exemplo, deve ser renomeado. A v2 também impede execução de
instruções depois de atingir `max_ticks` em `run()`, alinhando-o ao stepping.

Limites deliberados:

- Ainda não há floats, strings, números negativos, structs ou memória dinâmica.
- Sem macros, includes, módulos, expressões de montagem, interrupções ou debugger.
- Dados maiores que 15 precisam ser representados com vários nibbles.
- RAM fixa em 256 células; cada pilha limitada a 64 entradas.
- Sem acesso nativo a teclado/arquivos/rede: o host integra dispositivos pelas portas.
- Sem bytecode/ROM executável. A proposta antiga de opcode de 4 bits da v1 não
  comporta as 38 instruções; não é um formato binário suportado pela v2.

## 11. Arquitetura e testes

| Arquivo | Responsabilidade |
|---|---|
| `isa.py` | Registradores, limites, assinaturas e mnemônicos |
| `lexer.py` | Tokens, números, diretivas, comentários e localização |
| `parser.py` | Constantes, RAM inicial, operandos, AST e resolução de rótulos |
| `vm.py` | Execução, ticks, RAM, pilhas, portas, callbacks e snapshots |
| `cli.py` | Argumentos, validação, entrada inicial, painel e saída numérica |
| `redvm.py` | Ponto de entrada da CLI |
| `examples/` | Quatro circuitos v1 e quatro programas v2 |
| `tests/` | Regressões da v1 e cobertura dos recursos v2 |

Fluxo: fonte → tokens → constantes/AST/RAM inicial → resolução de rótulos → VM.
`Program` contém `instructions`, `labels`, `filename`, `data` e `constants`.
Diretivas são processadas na montagem e não aparecem em `instructions`.

Na raiz:

```bash
python -m unittest discover -s redasm/tests -v
python -m unittest discover -s redengine/tests -v
```

A suíte cobre ALU/relacionais sobre o domínio de 4 bits, limites, aliases,
componentes, diretivas, RAM, pilhas, recursão, portas, snapshots, stepping,
callbacks, exemplos e CLI. A suíte do RedEngine verifica a integração existente.
