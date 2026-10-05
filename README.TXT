================================================================================
                          P A R E N O S   v 1 . 0
          A tiny operating system whose source uses only four characters
================================================================================

  1. WHAT IS PARENOS?

  ParenOS is a small, real, bootable x86_64 operating system. It boots from
  an El Torito ISO image (the file "parenos.iso" in this distribution), runs
  entirely in its own kernel, and presents an interactive shell plus a tiny
  graphical desktop with windows, mouse, and keyboard.

  The kernel is written in C with a small amount of x86_64 assembly. It is
  loaded by the GRUB boot loader (which is embedded inside the ISO).

  After the kernel boots, it loads a ParenCode program from /boot/program.bin
  inside the initramfs and executes it via the in-kernel ParenCode VM. That
  program prints the boot arithmetic result and drops you into the shell.

  The Python program "parenc.py" is the development tool. It compiles
  .par source files into bytecode and assembles the final bootable ISO.
  Python is ONLY required at build time; the booted ISO does not need
  Python.


  2. WHAT IS PARENCODE?

  ParenCode is a tiny stack-based bytecode language whose source code uses
  ONLY four characters:

        +    (plus)
        /    (slash)
        (    (open paren)
        )    (close paren)

  Every other character (spaces, newlines, letters, digits, punctuation)
  is silently ignored, so they may be used as comments.


  3. THE FOUR SOURCE CHARACTERS

        +   = positive unit (+1)
        /   = negative unit (-1)
        (   = open group
        )   = close group


  4. NUMBER REPRESENTATION

  A "number" is a maximal run of + and / characters. Its value is the
  running balance of + (each +1) minus / (each -1).

        +        = 1
        ++       = 2
        +++      = 3
        /        = -1
        //       = -2
        +/       = 0     (1 - 1)
        ++/      = 1     (2 - 1)
        +++++++  = 7
        ++++++++++++++/   = 14   (15 - 1, but commonly just written
                                  as 14 plus signs)


  5. GROUP SYNTAX

  A group is "( ... )". Inside a group can be numbers and nested groups.
  The value of a group is the sum of the values of its children.

  Examples:
        (+++++++)       = 7    (one child: the number 7)
        (+(+++++++))    = an instruction (see below)


  6. INSTRUCTION FORMAT

  An instruction is a group whose first child is the opcode number:

        ( opcode arguments... )

  Arguments are themselves numbers or nested groups, evaluated at compile
  time as constants.

  Examples:
        (+(+++++++))    means PUSH 7
        - opcode is +   = 1 = PUSH
        - argument is (+++++++) which evaluates to 7

        (+++)            means ADD  (opcode 3, no arguments)
        (++++++++++++++++++++)  means PRINT (opcode 20, no arguments)


  7. EVERY OPCODE

  0   NOP              no operation
  1   PUSH n           push integer n onto the stack
  2   POP              discard top of stack
  3   ADD              b=pop, a=pop, push a+b
  4   SUB              b=pop, a=pop, push a-b
  5   MUL              b=pop, a=pop, push a*b
  6   DIV              b=pop, a=pop, push a/b (truncated toward zero)
  7   MOD              b=pop, a=pop, push a%b
  8   NEG              a=pop, push -a
  9   INC              a=pop, push a+1
  10  DEC              a=pop, push a-1
  11  EQ               b=pop, a=pop, push (a==b)?1:0
  12  NE               b=pop, a=pop, push (a!=b)?1:0
  13  LT               b=pop, a=pop, push (a<b)?1:0
  14  LE               b=pop, a=pop, push (a<=b)?1:0
  15  GT               b=pop, a=pop, push (a>b)?1:0
  16  GE               b=pop, a=pop, push (a>=b)?1:0
  17  JUMP pc          set program counter to pc
  18  JZ   pc          pop v; if v==0 jump to pc
  19  JNZ  pc          pop v; if v!=0 jump to pc
  20  PRINT            print top of stack as a decimal integer
  21  NEWLINE          print "\n"
  22  SET slot         pop v; var[slot] = v
  23  GET slot         push var[slot]
  24  CLEAR slot       var[slot] = 0
  25  HELP             print a short list of shell commands
  26  SHELL            drop into the interactive ParenOS shell
  27  MKDIR id         make a directory (stub on read-only initramfs)
  28  LS               list files in the initramfs
  29  PWD              print working directory
  30  CD id            change directory (stub)
  31  READ id          read a file from the initramfs and print it
  32  WRITE id         write to a file (read-only: prints a notice)
  33  DELETE id        delete a file (read-only: prints a notice)
  34  TIME             push packed current time (HHMMSS as int)
  35  SLEEP ms         sleep ms milliseconds
  36  RANDOM           push a pseudo-random integer
  37  BEEP              emit a short PC-speaker beep
  38  WINDOW id        initialize the window manager / desktop
  39  PIXEL            x=pop, y=pop, color=pop; draw a pixel
  40  RECT             x=pop, y=pop, w=pop, h=pop, color=pop; fill a rect
  41  CLS_GRAPHICS     clear the framebuffer
  42  KEY              block until a key is pressed; push its ASCII code
  43  MOUSE            push button state, then Y, then X (top of stack = X)
  44  SPAWN prog       spawn a process (single-threaded kernel: prints a notice)
  45  KILL pid         kill a process (single-threaded kernel: stub)
  46  SAVE             save state (no persistent storage driver: stub)
  47  LOAD             load state (no persistent storage driver: stub)
  48  ABOUT            print a short description of ParenOS
  49  HALT             halt the VM


  8. HOW VARIABLES / REGISTERS WORK

  ParenCode has 256 variable slots numbered 0..255. They are addressed by
  the immediate operand of SET / GET / CLEAR. Variables persist for the
  lifetime of the VM.

        (+(+++++++))   PUSH 7
        (+(+))          PUSH 1
        (++++++++++++++(+/))   SET 0      # var[0] = 1
        (+++++++++++++++(+/))  GET 0      # push var[0]


  9. STACK USAGE

  The VM has a 65536-entry stack. PUSH pushes; arithmetic pops two and
  pushes one; PRINT pops one; etc. Stack underflow halts the VM.


  10. ARITHMETIC

  7 + 5 = 12:
        (+(+++++++))   PUSH 7
        (+(+++++))     PUSH 5
        (+++)          ADD
        (++++++++++++++++++++)  PRINT


  11. COMPARISONS

  LT (opcode 13): a < b -> push 1 else push 0.

        (+(++))        PUSH 2
        (+(+++))       PUSH 3
        (+++++++++++++)  LT      # 2 < 3 -> pushes 1


  12. JUMPS AND LOOPS

  JUMP pc, JZ pc, JNZ pc let you build loops. PC is the byte offset of the
  target instruction (each instruction is 9 bytes).

  Example: count down from 3 (see source/scheduler.par).

        PUSH 3   SET 0          # var[0] = 3
  loop: GET 0   DEC  SET 0      # decrement
        GET 0   PRINT            # print
        GET 0   JNZ loop         # repeat while non-zero
        HALT


  13. FUNCTIONS / PROGRAM STRUCTURE

  ParenCode does not have first-class functions in this release. JUMP is
  the only control-flow primitive. Use SET/GET for local state, and
  structure programs as one flat bytecode stream.


  14. FILESYSTEM COMMANDS

  The initramfs is mounted read-only at /. LS prints every file in it;
  READ id prints the contents of /etc/welcome.txt (id 0). WRITE/DELETE
  are stubs because the initramfs is read-only.

  Filesystem layout inside the ISO:
        /boot/kernel.elf       - the kernel binary
        /boot/program.bin      - the compiled ParenCode bytecode
        /boot/initramfs.tar    - the USTAR archive mounted at /
        /system/fonts/         - bitmap font files
        /system/apps/          - application slot directory
        /system/drivers/        - driver slot directory
        /system/lib/           - library slot directory
        /system/bin/           - bin slot directory
        /programs/             - user programs
        /home/                 - user home
        /downloads/            - downloads
        /tmp/                  - temporary files
        /etc/                  - configuration
        /source/               - the ParenOS source tree (this README
                                  lives here too)


  15. GRAPHICS

  PIXEL (opcode 39) draws a single pixel. Stack order: push color, push y,
  push x; the order of popping is reversed, so pop x, pop y, pop color.
  RECT (opcode 40) fills a rectangle. Stack: color, h, w, y, x.

  Colors are 0x00RRGGBB (32-bit RGB).


  16. WINDOWS

  WINDOW (opcode 38) initializes the window manager / desktop. Once active,
  you can press single-letter shortcuts in the desktop:
        a  About window
        c  Calculator
        f  File manager
        e  Editor (opens /source/parenos.par)
        t  Terminal
        ESC  return to shell


  17. KEYBOARD

  KEY (opcode 42) blocks until a key is pressed, then pushes its ASCII
  code. Inside the shell, typing is line-buffered (Enter to execute).


  18. MOUSE

  MOUSE (opcode 43) pushes three values: button flags (top), Y, X. Pop
  X first, then Y, then button. The mouse cursor is composited with
  XOR so it never permanently paints the framebuffer.


  19. SOUND

  BEEP (opcode 37) plays a 100 ms 440 Hz tone on the PC speaker. This is
  the only sound interface implemented in this release.


  20. PROCESSES

  ParenOS is single-threaded. SPAWN and KILL are acknowledged but do not
  create real OS processes in this release.


  21. HOW TO WRITE A .par PROGRAM

  Create a file with .par extension. Use only the four source characters
  for code; any other characters are treated as comments.

  Example (prints "42"):
        # My first ParenOS program.
        (+(++++++))         PUSH 6
        (+(+++++++))        PUSH 7
        (+++++)             MUL
        (++++++++++++++++++++)  PRINT
        (+++++++++++++++++++++++++++++++++++++++++++++++++)  HALT


  22. HOW TO COMPILE / RUN

  To run a program in the development VM (no ISO needed):
        python3 parenc.py program.par

  To dump the compiled bytecode:
        python3 parenc.py program.par --dump

  To build the bootable ISO:
        python3 parenc.py parenos.par --iso parenos.iso


  23. HOW TO TEST WITH QEMU

  Boot the ISO:
        qemu-system-x86_64 -cdrom parenos.iso

  With more memory:
        qemu-system-x86_64 -cdrom parenos.iso -m 256M

  With serial console (the kernel writes debug logs to COM1):
        qemu-system-x86_64 -cdrom parenos.iso -serial stdio

  With a mouse and a larger display:
        qemu-system-x86_64 -cdrom parenos.iso -m 256M -serial stdio


  24. EXPERIMENTAL / NOT IMPLEMENTED FEATURES

  - Persistent storage: there is no ATA / AHCI / NVMe driver. The
    filesystem is a read-only initramfs loaded by GRUB.
  - Sound: only the PC speaker (BEEP). No PCI audio.
  - Networking: no NIC driver. No TCP/IP stack.
  - Processes: SPAWN/KILL are stubs. Single-threaded kernel.
  - TrueType / scalable fonts: only the 8x16 bitmap font is supported.
  - USB: no USB stack.
  - SMP: single-core only.
  - Save/Load: no persistent storage, so SAVE/LOAD are stubs.

  The Python dev VM (used by `python3 parenc.py program.par`) implements
  the same opcode set as the kernel VM, with the following caveats:
  - Graphics and sound opcodes print a textual representation rather
    than draw/beep.
  - SHELL drops into a Python-side interactive shell that supports a
    subset of the kernel shell's commands.

================================================================================
                          End of README.TXT
================================================================================
