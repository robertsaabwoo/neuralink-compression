# nlc_core (slot selector + encoder + output FIFO + config) at the real 256-slot interface,
# driven by the shared environment test/env/nlc_env.py. docs/testing.md 4.3 (T-IF, T-BW, T-OVF,
# T-ROB, T-LAT).
#   make -C test/core                              RTL, all tests
#   make -C test/core COCOTB_TEST_FILTER=t_if_1    one test
#   NLC_LONG=1   T-ROB-1 (70 packets, ~1 h)        NLC_SEEDS=N   T-ROB-6 random runs (default 4)
#   CLK_NS=200   clock period (default 200 = 5 MHz, D2)
#   NLC_RESULTS=<dir>  JSON per test (default test/results/core)

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC = $(CURDIR)/../../src
COMPILE_ARGS += -g2012 -I$(SRC)
TOPLEVEL = nlc_core
COCOTB_TEST_MODULES ?= test_core
SIM_BUILD = sim_build/rtl
VERILOG_SOURCES = $(addprefix $(SRC)/,nlc_core.v nlc_cfg.v nlc_slot_sel.v nlc_out_fifo.v \
                  nlc_encoder.v nlc_lossy.sv nlc_lift53.sv nlc_rans.sv nlc_lossy_rom.v nlc_icg.sv)
export PYTHONPATH := $(CURDIR)/../env:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
