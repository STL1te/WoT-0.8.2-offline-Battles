# -*- coding: utf-8 -*-
"""Self-test for offh_math: does native code really scale on cores?

Answers three questions before anything depends on the answer:
  1. How many cores does the process see, and is it pinned by affinity?
  2. Does a parallel native call scale against the serial one?
  3. Is the GIL actually released during the call?  (A Python thread that
     keeps counting while C runs proves it.)
"""

import ctypes
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(HERE, 'offh_math.so')
if not os.path.exists(LIB_PATH):
    LIB_PATH = os.path.join(HERE, 'offh_math.dll')

LIB = ctypes.CDLL(LIB_PATH)
LIB.offh_selftest_serial.restype = ctypes.c_double
LIB.offh_selftest_serial.argtypes = [ctypes.c_int]
LIB.offh_selftest_parallel.restype = ctypes.c_double
LIB.offh_selftest_parallel.argtypes = [ctypes.c_int, ctypes.c_int]
LIB.offh_cpu_count.restype = ctypes.c_int
LIB.offh_affinity_mask.restype = ctypes.c_ulonglong

ITER = 400000


def popcount(value):
    return bin(value).count('1')


def gil_probe(seconds_budget):
    """Count Python-level iterations in a thread for a fixed wall window."""
    stop = [False]
    count = [0]

    def spin():
        n = 0
        while not stop[0]:
            n += 1
            if n % 1000 == 0:
                time.sleep(0)
        count[0] = n

    thread = threading.Thread(target=spin)
    thread.daemon = True
    thread.start()
    t0 = time.time()
    while time.time() - t0 < seconds_budget:
        LIB.offh_selftest_parallel(1, ITER // 4)
    stop[0] = True
    thread.join(2.0)
    return count[0]


def main():
    print('library           : %s' % LIB_PATH)
    print('offh_math version : %d' % LIB.offh_math_version())
    cores = LIB.offh_cpu_count()
    mask = LIB.offh_affinity_mask()
    print('cores (C view)    : %d   affinity mask: 0x%x (%d cores allowed)'
          % (cores, mask, popcount(mask) if mask else -1))
    print('cpu_count (python): %d' % (os.cpu_count() if hasattr(os, 'cpu_count')
                                       else -1))
    print()

    serial = LIB.offh_selftest_serial(ITER)
    print('serial   %9d iters : %7.2f ms' % (ITER, serial))
    print()
    print('threads  total work   wall ms   speedup vs serial-equivalent')
    for threads in (1, 2, 4, 6, 8):
        if threads > max(1, cores * 2):
            break
        wall = LIB.offh_selftest_parallel(threads, ITER)
        equivalent = serial * threads
        print('%7d  %9d iters  %7.2f   %5.2fx'
              % (threads, ITER * threads, wall,
                 (equivalent / wall) if wall > 0 else 0.0))
    print()

    baseline = gil_probe(0.60)
    print('GIL probe: python-thread iterations while C ran in 1 thread : %d'
          % baseline)
    parallel = gil_probe(0.60)
    print('           (same probe, for reference)                      : %d'
          % parallel)
    print('If the counter is > 0, ctypes released the GIL during the call')
    print('and Python threads keep running next to native ones.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
