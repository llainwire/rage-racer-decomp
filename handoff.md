# Rage Racer Decomp --- Handoff Notes

## Project and working environment

-   **Repository:** `https://github.com/khasinski/rage-racer-decomp`
-   **Windows checkout:** `D:\Recomps and Decomps\rage-racer-decomp`
-   **WSL path:** `/mnt/d/Recomps and Decomps/rage-racer-decomp`
-   **Build environment:** Ubuntu 24.04 under WSL, using Python 3.12.3.
-   Work from the repository root in the **Ubuntu/WSL terminal** for the
    Linux build steps below.

## Python environment

The Windows `.venv` is a Windows virtual environment and should not be
activated from WSL. A separate Linux virtual environment was created for
this project:

``` bash
python3 -m venv .venv-linux
source .venv-linux/bin/activate
python -m pip install -r requirements.txt
```

When active, the prompt shows `(.venv-linux)`. Verify the interpreter
with:

``` bash
which python
python --version
```

The path should include `.venv-linux/bin/python`. Keep this environment
separate from the Python installation used for AI-model work.

## MIPS toolchain

Ubuntu's cross-binutils were installed, providing these commands:

-   `mipsel-linux-gnu-as`
-   `mipsel-linux-gnu-ld`
-   `mipsel-linux-gnu-nm`
-   `mipsel-linux-gnu-objcopy`
-   `mipsel-linux-gnu-readelf`
-   `mipsel-linux-gnu-cpp`

The project expects the corresponding `mipsel-none-elf-*` names. Local
symbolic links were created in `build/toolchain/bin`:

``` bash
mkdir -p build/toolchain/bin
for tool in as ld nm objcopy readelf cpp; do
    ln -sf "/usr/bin/mipsel-linux-gnu-$tool" \
        "build/toolchain/bin/mipsel-none-elf-$tool"
done
export PATH="$PWD/build/toolchain/bin:$PATH"
```

**Important:** the `export PATH=...` applies only to the current shell
session. If opening a new WSL terminal, `cd` to the repository root,
activate `.venv-linux`, and run the export command again before
building.

These are naming links to Ubuntu's MIPS Linux-targeted binutils, not a
separately installed `mipsel-none-elf` bare-metal toolchain. If a future
link or relocation step reports an architecture/emulation problem,
revisit the toolchain choice rather than assuming every stage is
interchangeable.

## Legacy PS1 compiler

The project uses the original **GCC 2.6.3 PlayStation compiler
frontend** (`cc1-psx-263`) via `tools/scripts/cc.sh`. The executable was
copied into:

``` text
build/toolchain/bin/cc1-psx-263
```

It was identified as a statically linked 32-bit Linux i386 ELF
executable. The wrapper can run it directly if supported; otherwise it
has fallback paths for `qemu-i386`, `qemu-i386-static`, or Docker. Do
not test it with `--version`; that option is not supported by this old
compiler.

The wrapper also uses `maspsx`, which it is designed to clone into
`build/toolchain/maspsx` when needed. It runs the C preprocessor, the
legacy compiler, the `maspsx.py` conversion step, and the MIPS
assembler.

## Line-ending fix

The build reached C compilation but failed because `tools/scripts/cc.sh`
had Windows CRLF line endings. It was fixed from WSL with:

``` bash
sed -i 's/\r$//' tools/scripts/cc.sh
```

If a script later fails with an error such as
`/usr/bin/env: 'bash\r': No such file or directory`, check its line
endings. Avoid converting unrelated files unless they show the same
issue.

## Build and verification

With `.venv-linux` active and the local toolchain directory on `PATH`:

``` bash
make split VERSION=PAL
make build VERSION=PAL
make check VERSION=PAL
```

The PAL build completed, and the check output was:

``` text
build/PAL/main.exe: OK
```

That confirms the repository's check passed for the built PAL
executable. It does **not** mean the output is a native Windows game.

### Report and progress

`make progress VERSION=PAL` requires `build/PAL/report.json`, so
generate the report first:

``` bash
make report VERSION=PAL
make progress VERSION=PAL
```

The report-generation step found 389 objects and 3,257 symbols from the
verified build, with 389 units assembled and matching the
linker-assigned sizes. It then failed because
`build/toolchain/bin/objdiff-cli` was missing.

A Linux `objdiff-cli` download was subsequently attempted/started at
that expected path. Confirm it exists and runs before retrying the
report:

``` bash
ls -l build/toolchain/bin/objdiff-cli
build/toolchain/bin/objdiff-cli --help
make report VERSION=PAL
make progress VERSION=PAL
```

If the download or CLI command fails, consult the upstream `objdiff`
release instructions and choose a binary compatible with the WSL
architecture. Do not treat a missing report as a failed game build.

## PlayStation libraries and disc tools

This is a PlayStation 1 decompilation project. The source/build process
uses the project's headers, assembly, reconstructed C, legacy PS1
compiler, and MIPS tools described above. No separate PlayStation
SDK/library installation was confirmed during this setup; do not assume
that a full Psy-Q SDK was installed. The report listed 170 PsyQ-related
objects, but that is not evidence that a separate SDK installation was
performed.

**PowerISO** is a Windows disc-image utility, not a compiler or a
required part of the WSL C build. It can be useful for inspecting or
extracting files from a legally obtained PlayStation disc image. It does
not by itself turn the decompilation output into a complete disc image.
Keep any original disc image and extracted files separate from the build
tools, and follow the project's own instructions for any asset
extraction or packaging.

## What `build/PAL/main.exe` is

Despite the `.exe` extension, this is a PlayStation-targeted build
output, not a normal Windows executable. Windows will not run it as a PC
game. To test it, determine the repository's intended packaging/boot
process and use a PS1 emulator such as DuckStation where appropriate.
The build output may need to be combined with original game data or
packaged according to project instructions; do not assume `main.exe`
alone is a complete, bootable game image.

## Useful source locations

-   `src/main/` --- reconstructed C source.
-   `include/` --- headers.
-   `asm/PAL/main/` --- PAL assembly and extracted data.
-   `build/PAL/` --- generated objects, executable, and report
    artifacts.
-   `tools/scripts/cc.sh` --- C compilation pipeline.
-   `Makefile` --- build/report targets.

## Current state / next actions

1.  Keep using WSL and `.venv-linux` for the build.
2.  Reapply the local toolchain `PATH` export in each new shell session.
3.  Separately investigate the project's documented process for turning
    the build output plus any required original assets into something
    bootable in a PS1 emulator.
