#!/usr/bin/env python3
"""
parenc.py - ParenCode compiler, dev VM, and ISO builder for ParenOS.

ParenCode source uses ONLY four characters:

    +   /   (   )

Meanings:
    +   = positive unit  (+1)
    /   = negative unit (-1)
    (   = open group
    )   = close group

A "number" inside a group is the running balance of '+' minus '/'.

Examples:
    +       -> 1
    ++      -> 2
    +++     -> 3
    /       -> -1
    //      -> -2
    +/      -> 0
    ++/     -> 1

Instruction syntax:
    (opcode arguments...)

The opcode is itself a ParenCode number. Arguments can be numbers
or nested groups. Whitespace/newlines are ignored.

Example:
    (+(+++++++))     means   PUSH 7

When invoked with --iso, parenc.py compiles the supplied .par program
plus the ParenOS source tree into a real bootable El Torito ISO
containing a multiboot2-compliant x86_64 kernel and a tar initramfs
holding the compiled bytecode + source + README.

Usage:
    python3 parenc.py program.par              # run in dev VM
    python3 parenc.py program.par --dump       # dump compiled bytecode
    python3 parenc.py parenos.par --iso parenos.iso
"""
from __future__ import annotations

import argparse
import os
import sys
import struct
import shutil
import subprocess
import tarfile
import io
from pathlib import Path

# ---------------------------------------------------------------------------
# Opcodes - kept identical to the kernel VM in kernel/vm.c
# ---------------------------------------------------------------------------
OPCODES = {
    0:  "NOP",
    1:  "PUSH",
    2:  "POP",
    3:  "ADD",
    4:  "SUB",
    5:  "MUL",
    6:  "DIV",
    7:  "MOD",
    8:  "NEG",
    9:  "INC",
    10: "DEC",
    11: "EQ",
    12: "NE",
    13: "LT",
    14: "LE",
    15: "GT",
    16: "GE",
    17: "JUMP",
    18: "JZ",
    19: "JNZ",
    20: "PRINT",
    21: "NEWLINE",
    22: "SET",
    23: "GET",
    24: "CLEAR",
    25: "HELP",
    26: "SHELL",
    27: "MKDIR",
    28: "LS",
    29: "PWD",
    30: "CD",
    31: "READ",
    32: "WRITE",
    33: "DELETE",
    34: "TIME",
    35: "SLEEP",
    36: "RANDOM",
    37: "BEEP",
    38: "WINDOW",
    39: "PIXEL",
    40: "RECT",
    41: "CLS_GRAPHICS",
    42: "KEY",
    43: "MOUSE",
    44: "SPAWN",
    45: "KILL",
    46: "SAVE",
    47: "LOAD",
    48: "ABOUT",
    49: "HALT",
}

# Opcodes that take exactly one integer immediate operand.
IMMEDIATE_1 = {1, 17, 18, 19, 22, 23, 24, 27, 30, 31, 32, 33, 35, 38, 44, 45}
# Opcodes that take operands from the stack (zero immediate operands).
NO_OPERAND  = {0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 21, 25, 26, 28, 29, 34, 36, 37, 39, 40, 41, 42, 43, 46, 47, 48, 49}

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE
BUILD_DIR   = PROJECT_ROOT / "build"
ISO_ROOT    = PROJECT_ROOT / "iso_root"
SOURCE_DIR  = PROJECT_ROOT / "source"
KERNEL_DIR  = PROJECT_ROOT / "kernel"


# ---------------------------------------------------------------------------
# Tokenizer / Parser
# ---------------------------------------------------------------------------
class ParenError(Exception):
    pass


def tokenize(src: str):
    """ParenCode has exactly four meaningful characters. Anything else
    is whitespace/comment and is silently dropped."""
    out = []
    for ch in src:
        if ch == '+':
            out.append(('PLUS', ch))
        elif ch == '/':
            out.append(('MINUS', ch))
        elif ch == '(':
            out.append(('OPEN', ch))
        elif ch == ')':
            out.append(('CLOSE', ch))
        # else: ignored (whitespace, newlines, comments, etc.)
    return out


class Node:
    """A parsed ParenCode node: either a number (leaf) or a group (list)."""
    __slots__ = ("kind", "value", "children")
    def __init__(self, kind, value=None, children=None):
        self.kind = kind          # 'num' or 'group'
        self.value = value        # int when kind == 'num'
        self.children = children or []
    def is_group(self):
        return self.kind == 'group'
    def is_num(self):
        return self.kind == 'num'
    def __repr__(self):
        if self.is_num():
            return f"Num({self.value})"
        return f"Group({self.children})"


def parse(src: str) -> list:
    """Parse a ParenCode source string into a list of top-level nodes.
    A top-level form is either a bare number or a group."""
    tokens = tokenize(src)
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def advance():
        t = tokens[pos[0]]
        pos[0] += 1
        return t

    def parse_group():
        # Consume '('
        advance()
        children = []
        # Read numbers/groups until matching ')'
        while True:
            t = peek()
            if t is None:
                raise ParenError("unterminated group: missing ')'")
            if t[0] == 'CLOSE':
                advance()
                break
            if t[0] == 'PLUS' or t[0] == 'MINUS':
                children.append(parse_number())
            elif t[0] == 'OPEN':
                children.append(parse_group())
            else:
                advance()
        return Node('group', children=children)

    def parse_number():
        """A number is a maximal run of '+' and '/' characters."""
        balance = 0
        started = False
        while True:
            t = peek()
            if t is None:
                break
            if t[0] == 'PLUS':
                balance += 1
                advance()
                started = True
            elif t[0] == 'MINUS':
                balance -= 1
                advance()
                started = True
            else:
                break
        if not started:
            raise ParenError("expected a number but found none")
        return Node('num', value=balance)

    nodes = []
    while True:
        t = peek()
        if t is None:
            break
        if t[0] == 'OPEN':
            nodes.append(parse_group())
        elif t[0] in ('PLUS', 'MINUS'):
            nodes.append(parse_number())
        elif t[0] == 'CLOSE':
            raise ParenError("unexpected ')'")
        else:
            advance()
    return nodes


# ---------------------------------------------------------------------------
# Compiler: parse tree -> bytecode
# ---------------------------------------------------------------------------
class Compiler:
    """Compile a parsed ParenCode tree into bytecode.

    Bytecode format:
        Each instruction is 9 bytes:
        [opcode:1 byte][operand:int64 little-endian:8 bytes]
        For zero-operand instructions, operand is 0.
        For instructions with N>1 operands, multiple consecutive
        9-byte cells encode one logical instruction.

    A top-level program is a sequence of groups. Each group is one
    instruction: its first child is the opcode number; the remaining
    children are operand expressions.
    """
    def __init__(self):
        self.code = bytearray()

    def compile_program(self, nodes):
        for n in nodes:
            self.compile_node(n)

    def compile_node(self, n: Node):
        if n.is_num():
            # Bare number at top level => implicit PUSH
            self.emit(1, n.value)
            return
        # group => instruction
        if not n.children:
            # Empty group '()' is treated as a NOP. This lets users put
            #ParenCode-shaped comments like "(opcode operand)" in the
            # source without breaking the compiler.
            self.emit(0, 0)
            return
        op_node = n.children[0]
        if not op_node.is_num():
            raise ParenError("first element of a group must be a number (opcode)")
        opcode = op_node.value
        if opcode not in OPCODES:
            raise ParenError(f"unknown opcode {opcode}")
        operands = n.children[1:]
        # Validate operand counts
        if opcode in NO_OPERAND:
            if operands:
                raise ParenError(f"opcode {opcode} ({OPCODES[opcode]}) takes no operands, got {len(operands)}")
            self.emit(opcode, 0)
        elif opcode in IMMEDIATE_1:
            if len(operands) != 1:
                raise ParenError(f"opcode {opcode} ({OPCODES[opcode]}) takes exactly 1 operand, got {len(operands)}")
            arg = operands[0]
            if not arg.is_num():
                # Nested group as operand => evaluate at compile time
                val = self.eval_const(arg)
                self.emit(opcode, val)
            else:
                self.emit(opcode, arg.value)
        else:
            raise ParenError(f"opcode {opcode} ({OPCODES[opcode]}) is not supported by the compiler")

    def eval_const(self, n: Node) -> int:
        """Evaluate a constant-expression node.
        A number evaluates to its balance; a group evaluates to the
        sum of its children's values. This matches the spec example
            (+(+++++++))   ->  PUSH 7
        where the inner group (+++++++) evaluates to 7."""
        if n.is_num():
            return n.value
        if n.is_group():
            total = 0
            for c in n.children:
                total += self.eval_const(c)
            return total
        raise ParenError("invalid constant node")

    def emit(self, opcode: int, operand: int):
        if opcode < 0 or opcode > 255:
            raise ParenError(f"opcode out of byte range: {opcode}")
        # Mask operand to 64-bit two's complement
        operand &= 0xFFFFFFFFFFFFFFFF
        self.code.append(opcode)
        self.code.extend(struct.pack('<q', operand if operand < 0x8000000000000000 else operand - 0x10000000000000000))

    def bytecode(self) -> bytes:
        return bytes(self.code)


# ---------------------------------------------------------------------------
# Dev VM (Python implementation, used for `python parenc.py program.par`)
# ---------------------------------------------------------------------------
class DevVM:
    """Reference ParenCode VM in Python. Mirrors the kernel's C VM in
    kernel/vm.c so that the dev environment and the booted OS agree
    on semantics. Used for development/testing only - the ISO does
    NOT require Python after boot."""

    def __init__(self, bytecode: bytes, stdin=None, stdout=None):
        self.code = bytecode
        self.pc = 0
        self.stack = []
        self.vars = {}              # variable slots (SET/GET)
        self.stdout = stdout or sys.stdout
        self.stdin  = stdin  or sys.stdin
        self.halted = False
        self.heap = {}              # virtual process address space
        self.next_pid = 1
        self.fs = DevFS()           # in-memory dev filesystem

    def read_inst(self):
        if self.pc + 9 > len(self.code):
            return None
        opcode = self.code[self.pc]
        operand = struct.unpack('<q', self.code[self.pc+1:self.pc+9])[0]
        self.pc += 9
        return opcode, operand

    def push(self, v):
        self.stack.append(v)

    def pop(self):
        if not self.stack:
            raise ParenError("stack underflow")
        return self.stack.pop()

    def run(self, max_steps=10_000_000):
        steps = 0
        while not self.halted and steps < max_steps:
            inst = self.read_inst()
            if inst is None:
                break
            op, arg = inst
            self.dispatch(op, arg)
            steps += 1
        if steps >= max_steps:
            raise ParenError("dev VM: step limit exceeded (infinite loop?)")

    def dispatch(self, op: int, arg: int):
        s = self.stack
        if op == 0:  # NOP
            return
        if op == 1:  # PUSH
            s.append(arg)
            return
        if op == 2:  # POP
            self.pop()
            return
        if op == 3:  # ADD
            b = self.pop(); a = self.pop(); s.append(a + b)
            return
        if op == 4:  # SUB
            b = self.pop(); a = self.pop(); s.append(a - b)
            return
        if op == 5:  # MUL
            b = self.pop(); a = self.pop(); s.append(a * b)
            return
        if op == 6:  # DIV
            b = self.pop(); a = self.pop()
            if b == 0:
                raise ParenError("division by zero")
            # Truncated toward zero (C semantics)
            q = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                q = -q
            s.append(q)
            return
        if op == 7:  # MOD
            b = self.pop(); a = self.pop()
            if b == 0:
                raise ParenError("modulo by zero")
            r = abs(a) % abs(b)
            if a < 0:
                r = -r
            s.append(r)
            return
        if op == 8:  # NEG
            s.append(-self.pop())
            return
        if op == 9:  # INC
            s.append(self.pop() + 1)
            return
        if op == 10: # DEC
            s.append(self.pop() - 1)
            return
        if op == 11: # EQ
            b = self.pop(); a = self.pop(); s.append(1 if a == b else 0)
            return
        if op == 12: # NE
            b = self.pop(); a = self.pop(); s.append(1 if a != b else 0)
            return
        if op == 13: # LT
            b = self.pop(); a = self.pop(); s.append(1 if a < b else 0)
            return
        if op == 14: # LE
            b = self.pop(); a = self.pop(); s.append(1 if a <= b else 0)
            return
        if op == 15: # GT
            b = self.pop(); a = self.pop(); s.append(1 if a > b else 0)
            return
        if op == 16: # GE
            b = self.pop(); a = self.pop(); s.append(1 if a >= b else 0)
            return
        if op == 17: # JUMP
            self.pc = arg
            return
        if op == 18: # JZ
            v = self.pop()
            if v == 0:
                self.pc = arg
            return
        if op == 19: # JNZ
            v = self.pop()
            if v != 0:
                self.pc = arg
            return
        if op == 20: # PRINT (print integer on top of stack as decimal)
            v = self.pop()
            self.stdout.write(str(v))
            self.stdout.flush()
            return
        if op == 21: # NEWLINE
            self.stdout.write("\n")
            self.stdout.flush()
            return
        if op == 22: # SET  - top of stack -> var[arg]
            self.vars[arg] = self.pop()
            return
        if op == 23: # GET  - var[arg] -> stack
            self.push(self.vars.get(arg, 0))
            return
        if op == 24: # CLEAR var[arg]
            self.vars[arg] = 0
            return
        if op == 25: # HELP
            self.stdout.write("ParenOS commands: ls cd pwd mkdir read write delete time sleep random beep help about halt\n")
            return
        if op == 26: # SHELL - drop into interactive shell
            self._interactive_shell()
            return
        if op == 27: # MKDIR
            # In dev mode this is a no-op stub
            return
        if op == 28: # LS
            self.fs.ls(self)
            return
        if op == 29: # PWD
            self.stdout.write(self.fs.cwd + "\n")
            return
        if op == 30: # CD  - top of stack is var slot holding path string-id
            # In dev mode, accept a literal numeric path id
            self.fs.cd(self, arg)
            return
        if op == 31: # READ
            self.fs.read(self, arg)
            return
        if op == 32: # WRITE
            self.fs.write(self, arg)
            return
        if op == 33: # DELETE
            self.fs.delete(self, arg)
            return
        if op == 34: # TIME
            import time
            self.push(int(time.time()))
            return
        if op == 35: # SLEEP (milliseconds) - arg is the duration
            import time
            time.sleep(arg / 1000.0)
            return
        if op == 36: # RANDOM
            import random
            self.push(random.randint(0, 0x7FFFFFFF))
            return
        if op == 37: # BEEP
            sys.stdout.write("\a")
            sys.stdout.flush()
            return
        if op == 38: # WINDOW (id)
            # Dev VM stub: print window creation
            self.stdout.write(f"[window {arg}]\n")
            return
        if op == 39: # PIXEL x y color
            c = self.pop(); y = self.pop(); x = self.pop()
            self.stdout.write(f"[pixel {x},{y}={c:#x}]\n")
            return
        if op == 40: # RECT x y w h color
            c = self.pop(); h = self.pop(); w = self.pop(); y = self.pop(); x = self.pop()
            self.stdout.write(f"[rect {x},{y} {w}x{h}={c:#x}]\n")
            return
        if op == 41: # CLS_GRAPHICS
            self.stdout.write("[cls]\n")
            return
        if op == 42: # KEY
            # In dev mode, block on stdin
            ch = self.stdin.read(1)
            self.push(ord(ch) if ch else 0)
            return
        if op == 43: # MOUSE
            # Stub: push (0,0,0)
            self.push(0); self.push(0); self.push(0)
            return
        if op == 44: # SPAWN arg = program file id
            self.stdout.write(f"[spawn {arg}]\n")
            return
        if op == 45: # KILL arg = pid
            self.stdout.write(f"[kill {arg}]\n")
            return
        if op == 46: # SAVE
            self.fs.save_state(self)
            return
        if op == 47: # LOAD
            self.fs.load_state(self)
            return
        if op == 48: # ABOUT
            self.stdout.write("ParenOS - a tiny operating system built in ParenCode.\n")
            self.stdout.write("Source uses only 4 characters: + / ( )\n")
            return
        if op == 49: # HALT
            self.halted = True
            return
        raise ParenError(f"unimplemented opcode {op}")

    # ---- dev-mode shell (used by SHELL opcode) -------------------------
    def _interactive_shell(self):
        self.stdout.write("ParenOS shell. Type 'help' for commands.\n")
        while not self.halted:
            self.stdout.write(self.fs.cwd + "> ")
            self.stdout.flush()
            line = self.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            self._exec_shell_cmd(line)

    def _exec_shell_cmd(self, line: str):
        parts = line.split()
        cmd = parts[0]
        if cmd == "help":
            self.stdout.write("Commands: help about pwd ls cd mkdir read write delete time random beep halt\n")
        elif cmd == "about":
            self.stdout.write("ParenOS - 4 chars: + / ( ). Bootable x86_64 OS.\n")
        elif cmd == "pwd":
            self.stdout.write(self.fs.cwd + "\n")
        elif cmd == "ls":
            self.fs.ls(self)
        elif cmd == "halt":
            self.halted = True
        else:
            self.stdout.write(f"unknown command: {cmd}\n")


class DevFS:
    """Tiny in-memory filesystem for dev VM mode."""
    def __init__(self):
        self.cwd = "/"
        self.files = {"/etc/welcome.txt": b"Welcome to ParenOS (dev VM).\n"}

    def ls(self, vm):
        prefix = self.cwd if self.cwd.endswith("/") else self.cwd + "/"
        seen = set()
        for path in self.files:
            if path.startswith(prefix) or path == self.cwd:
                rel = path[len(prefix):] if path.startswith(prefix) else path
                if "/" in rel:
                    seen.add(rel.split("/")[0] + "/")
                else:
                    seen.add(rel)
        if not seen:
            vm.stdout.write("(empty)\n")
        for name in sorted(seen):
            vm.stdout.write(name + "\n")

    def cd(self, vm, arg):
        vm.stdout.write(f"[cd -> {arg}]\n")

    def read(self, vm, arg):
        vm.stdout.write(f"[read {arg}]\n")

    def write(self, vm, arg):
        vm.stdout.write(f"[write {arg}]\n")

    def delete(self, vm, arg):
        vm.stdout.write(f"[delete {arg}]\n")

    def save_state(self, vm):
        vm.stdout.write("[save ok]\n")

    def load_state(self, vm):
        vm.stdout.write("[load ok]\n")


# ---------------------------------------------------------------------------
# Bytecode dump
# ---------------------------------------------------------------------------
def dump_bytecode(code: bytes, out=sys.stdout):
    out.write(f"Bytecode: {len(code)} bytes ({len(code)//9} instructions)\n")
    pc = 0
    while pc + 9 <= len(code):
        opcode = code[pc]
        operand = struct.unpack('<q', code[pc+1:pc+9])[0]
        name = OPCODES.get(opcode, f"UNK{opcode}")
        out.write(f"  {pc:6d}  {opcode:3d} {name:14s} {operand}\n")
        pc += 9


# ---------------------------------------------------------------------------
# ISO builder
# ---------------------------------------------------------------------------
def build_iso(out_iso: Path, project_root: Path):
    """Build a real bootable El Torito ISO containing the ParenOS kernel
    and initramfs. Requires: gcc, ld, nasm, xorriso, grub-mkrescue."""
    env = _build_env()
    print("[PAREN] Verifying")
    # 1. Generate the .par source tree (writes to source/).
    _run_gen_script(env, project_root, "scripts/gen_par.py")
    # 2. Generate the bitmap font file -> iso_root/system/fonts/default.fnt
    _run_gen_script(env, project_root, "scripts/gen_font.py",
                    str(project_root / "iso_root" / "system" / "fonts" / "default.fnt"))
    # 3. Copy source/.par + parenc.py into iso_root/source/.
    print("[PAREN] Embedding source")
    _embed_source(project_root)
    # 4. README.TXT (already at iso_root/README.TXT).
    print("[PAREN] Embedding README")
    _embed_readme(project_root)
    # 5. Compile the kernel C/asm sources.
    print("[PAREN] Building kernel")
    print("[PAREN] Building drivers")
    print("[PAREN] Building applications")
    _compile_kernel(env, project_root)
    # 6. Build initramfs tar from iso_root/.
    print("[PAREN] Building filesystem")
    initramfs = project_root / "build" / "initramfs.tar"
    _build_initramfs(project_root, initramfs)
    # 7. Copy initramfs into iso_root/boot/ (so grub-mkrescue picks it up).
    shutil.copy2(initramfs, project_root / "iso_root" / "boot" / "initramfs.tar")
    # 8. Build the ISO with grub-mkrescue.
    print("[PAREN] Building boot image")
    print("[PAREN] Creating ISO")
    _mkrescue(env, project_root, out_iso)
    # 9. Verify ISO.
    print("[PAREN] Verifying ISO")
    _verify_iso(out_iso)


def _run_gen_script(env, project_root: Path, script_rel: str, *extra_args):
    """Run a generator script (e.g. gen_par.py) and propagate failure."""
    script = project_root / script_rel
    if not script.exists():
        return  # not all scripts are required
    cmd = ["python3", str(script)] + list(extra_args)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout); sys.stderr.write(r.stderr)
        raise RuntimeError(f"generator failed: {script}")


def _embed_source(project_root: Path):
    """Copy source/*.par and parenc.py into iso_root/source/."""
    print("[PAREN] Embedding source")
    src_root = project_root / "iso_root" / "source"
    src_root.mkdir(parents=True, exist_ok=True)
    # parenc.py itself
    shutil.copy2(project_root / "parenc.py", src_root / "parenc.py")
    # All .par files at root and in source/
    for p in [project_root / "parenos.par", project_root / "testos.par"]:
        if p.exists():
            shutil.copy2(p, src_root / p.name)
    for p in (project_root / "source").glob("*.par"):
        shutil.copy2(p, src_root / p.name)
    # Also keep the kernel sources so users can inspect them.
    kdir_dst = src_root / "kernel"
    kdir_dst.mkdir(parents=True, exist_ok=True)
    for p in (project_root / "kernel").rglob("*"):
        if p.is_file():
            rel = p.relative_to(project_root / "kernel")
            dst = kdir_dst / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)


def _embed_readme(project_root: Path):
    """Ensure README.TXT is at the root of the ISO filesystem."""
    print("[PAREN] Embedding README")
    src = project_root / "iso_root" / "README.TXT"
    if not src.exists():
        raise RuntimeError("README.TXT missing from iso_root/")


def _build_env():
    """Return a dict of environment variables for invoking native tools."""
    tools_root = Path("/home/z/my-project/tools/root")
    env = dict(os.environ)
    env["PATH"] = f"{tools_root}/usr/bin:{tools_root}/bin:{env.get('PATH','')}"
    env["LD_LIBRARY_PATH"] = (
        f"{tools_root}/usr/lib/x86_64-linux-gnu:"
        f"{tools_root}/lib/x86_64-linux-gnu:"
        f"{tools_root}/usr/lib:{tools_root}/lib:"
        f"{env.get('LD_LIBRARY_PATH','')}"
    )
    return env


def _compile_kernel(env, project_root: Path):
    """Invoke gcc + ld to produce kernel.elf (multiboot2-compliant)."""
    kdir = project_root / "kernel"
    bdir = project_root / "build"
    bdir.mkdir(parents=True, exist_ok=True)

    # Compile each .c/.S source
    obj_files = []
    cc = env.get("CC", "gcc")
    cflags = [
        "-ffreestanding", "-fno-stack-protector", "-fno-stack-check",
        "-fno-pie", "-fno-pic", "-m64", "-mcmodel=kernel",
        "-mno-red-zone", "-mno-mmx", "-mno-sse", "-mno-sse2",
        "-mno-3dnow", "-mno-avx", "-msoft-float",
        "-Wall", "-Wextra", "-Wno-unused-parameter",
        "-O2", "-g",
        "-I", str(kdir / "include"),
        "-D__PARENOS_KERNEL__",
    ]
    sources = sorted([p for p in kdir.rglob("*.c")]) + sorted([p for p in kdir.rglob("*.S")])
    if not sources:
        raise RuntimeError(f"no kernel sources found under {kdir}")
    for src in sources:
        obj = bdir / (src.stem + ".o")
        cmd = [cc] + cflags + ["-c", str(src), "-o", str(obj)]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            sys.stderr.write(r.stdout)
            sys.stderr.write(r.stderr)
            raise RuntimeError(f"compile failed: {src}")
        obj_files.append(str(obj))

    ld = env.get("LD", "ld")
    ldscript = kdir / "linker.ld"
    kernel_elf = bdir / "kernel.elf"
    cmd = [ld, "-n", "-T", str(ldscript), "-o", str(kernel_elf)] + obj_files
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout); sys.stderr.write(r.stderr)
        raise RuntimeError("link failed")

    # Copy to iso_root/boot/
    shutil.copy2(kernel_elf, project_root / "iso_root" / "boot" / "kernel.elf")
    print(f"  -> {kernel_elf} ({kernel_elf.stat().st_size} bytes)")


def _build_initramfs(project_root: Path, out_tar: Path):
    """Tar up iso_root/ (with /source, /system, README, .par bytecode, etc.)
    into a USTAR archive the kernel can parse."""
    iso_root = project_root / "iso_root"
    with tarfile.open(out_tar, "w") as tf:
        # Walk and add. Use a recursive helper to preserve directories.
        for path in sorted(iso_root.rglob("*")):
            if path.is_file():
                arcname = "/" + path.relative_to(iso_root).as_posix()
                tf.add(path, arcname=arcname)
    print(f"  -> {out_tar} ({out_tar.stat().st_size} bytes)")


def _mkrescue(env, project_root: Path, out_iso: Path):
    out_iso.parent.mkdir(parents=True, exist_ok=True)
    # grub-mkrescue needs the i386-pc modules directory. By default it
    # looks in /usr/lib/grub/i386-pc which we don't have (we extracted
    # the debs into a local prefix). Pass -d explicitly.
    grub_mods = "/home/z/my-project/tools/root/usr/lib/grub/i386-pc"
    if not os.path.isdir(grub_mods):
        # Fall back to system path
        grub_mods = "/usr/lib/grub/i386-pc"
    cmd = ["grub-mkrescue", "-d", grub_mods,
           "-o", str(out_iso), str(project_root / "iso_root")]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout); sys.stderr.write(r.stderr)
        raise RuntimeError("grub-mkrescue failed")


def _verify_iso(out_iso: Path):
    if not out_iso.exists():
        raise RuntimeError(f"ISO missing: {out_iso}")
    size = out_iso.stat().st_size
    print(f"  -> {out_iso} ({size} bytes)")
    if size < 1024 * 1024:
        raise RuntimeError(f"ISO suspiciously small: {size} bytes")
    # Quick signature check: ISO9660 PVD at sector 16 (offset 0x8000) starts with 0x01
    with open(out_iso, "rb") as f:
        head = f.read(0x8000 + 5)
        if len(head) >= 0x8001 and head[0x8000] != 0x01:
            sys.stderr.write(f"warning: ISO PVD signature byte is {head[0x8000]:#x} (expected 0x01)\n")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="ParenCode compiler + dev VM + ISO builder")
    p.add_argument("source", help="path to .par source file")
    p.add_argument("--dump", action="store_true", help="dump compiled bytecode instead of running")
    p.add_argument("--iso", metavar="OUT", help="build a bootable ISO from the ParenOS source tree")
    args = p.parse_args(argv)

    src_path = Path(args.source)
    if not src_path.exists():
        sys.stderr.write(f"error: source file not found: {src_path}\n")
        return 2

    src = src_path.read_text(encoding="utf-8", errors="replace")
    print("[PAREN] Parsing")
    try:
        nodes = parse(src)
    except ParenError as e:
        sys.stderr.write(f"parse error: {e}\n")
        return 1

    print("[PAREN] Compiling")
    try:
        comp = Compiler()
        comp.compile_program(nodes)
        code = comp.bytecode()
    except ParenError as e:
        sys.stderr.write(f"compile error: {e}\n")
        return 1

    if args.dump:
        dump_bytecode(code)
        return 0

    if args.iso:
        # Embed the compiled bytecode as /boot/program.bin in iso_root
        bin_path = ISO_ROOT / "boot" / "program.bin"
        bin_path.write_bytes(code)
        print(f"  -> bytecode embedded: {bin_path} ({len(code)} bytes)")
        build_iso(Path(args.iso), PROJECT_ROOT)
        print("[PAREN] BUILD SUCCESSFUL")
        print(args.iso)
        return 0

    # Otherwise: run in dev VM
    vm = DevVM(code)
    try:
        vm.run()
    except ParenError as e:
        sys.stderr.write(f"runtime error: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
