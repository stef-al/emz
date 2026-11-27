# emz build / test helpers
#
# The coder must be built with -ffp-contract=off: its mixers use float
# arithmetic, and fused multiply-add contraction (enabled by default by GCC
# for -march levels with FMA) would make archives non-portable between builds.
CC      ?= gcc
CFLAGS  ?= -O3
ifeq ($(NATIVE),1)
CFLAGS  += -march=native
endif
CFLAGS  += -ffp-contract=off -fno-fast-math

all: emcm

emcm_permsg: scripts/emcm_permsg.c emcm.c
	$(CC) $(CFLAGS) -o $@ scripts/emcm_permsg.c -lm

emcm: emcm.c
	$(CC) $(CFLAGS) -o $@ $< -lm

test: emcm
	python3 -m unittest discover -s tests -v

clean:
	rm -f emcm emcm_permsg

.PHONY: all test clean
