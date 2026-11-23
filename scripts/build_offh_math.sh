#!/bin/sh
# Rebuild offh_math for both the 32-bit WoT 0.8.2 client and the host
# benchmark harness.  Run from WSL.  Never use -march=native: the shipped
# binary must also run on machines without SSE2.
set -e
cd "$(dirname "$0")/../src/native"  # sources, selftest and outputs live there

if ! command -v i686-w64-mingw32-gcc >/dev/null 2>&1; then
	echo "i686-w64-mingw32-gcc not found. Install: apt install gcc-mingw-w64-i686" >&2
	exit 1
fi

echo "== client DLL (i686, pentiumpro/387, static libgcc) =="
i686-w64-mingw32-gcc -O2 -shared -static-libgcc -Wall -Wextra \
	-o offh_math.dll offh_math.c
file offh_math.dll
i686-w64-mingw32-objdump -p offh_math.dll | grep -i "DLL Name" || true

if command -v gcc >/dev/null 2>&1; then
	echo "== host library (selftest only, never shipped) =="
	gcc -O2 -shared -fPIC -pthread -Wall -Wextra -o offh_math.so offh_math.c
	python3 selftest.py
else
	echo "host gcc missing, skipped the selftest build" >&2
fi

echo "done"
