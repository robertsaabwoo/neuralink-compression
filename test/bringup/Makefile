# Fault injection to validate the output-diff diagnosis tool (scripts/bringup/diagnose.py).
# nlc_core inside a test-only wrapper with fault hooks (nlc_fault_top.v), the shared
# environment test/env/nlc_env.py, real data. Every test injects one fault, captures the
# output bytes like the board would, runs the diagnoser and checks it names the right block.
#   make -C test/bringup                                  all faults
#   make -C test/bringup COCOTB_TEST_FILTER=f_a_dp2       one fault
#   make -C test/bringup ICG_SIM=yes COCOTB_TEST_FILTER=f_a_gclk
#        the clock-gate fault itself: nlc_greg's real gate (+define+NLC_ICG_SIM), GCLK forced 0
#   NLC_RESULTS=<dir>   captures, stimulus, diagnosis per fault (default test/results/bringup)

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC = $(CURDIR)/../../src
COMPILE_ARGS += -g2012 -I$(SRC)
TOPLEVEL = nlc_fault_top
COCOTB_TEST_MODULES ?= test_faults
VERILOG_SOURCES = $(addprefix $(SRC)/,nlc_core.v nlc_cfg.v nlc_slot_sel.v nlc_out_fifo.v \
                  nlc_encoder.v nlc_lossy.sv nlc_lift53.sv nlc_rans.sv nlc_lossy_rom.v nlc_icg.sv) \
                  $(CURDIR)/nlc_fault_top.v
ifeq ($(ICG_SIM),yes)
COMPILE_ARGS += -DNLC_ICG_SIM
SIM_BUILD = sim_build/icg
export NLC_ICG_SIM = 1
else
SIM_BUILD = sim_build/rtl
endif
export PYTHONPATH := $(CURDIR)/../env:$(CURDIR)/../../scripts/bringup:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
