# nlc_core (slot selector + encoder + output FIFO + config) at the real 256-slot interface,
# driven by the shared environment test/env/nlc_env.py. docs/testing.md 4.3 (T-IF, T-BW, T-OVF,
# T-ROB, T-LAT, T-PWR-2).
#   make -C test/core                              RTL, all tests
#   make -C test/core COCOTB_TEST_FILTER=t_if_1    one test
#   make -C test/core GATES=yes NETLIST=<core_netlist.v>   gate level (sky130 models in $NLC_PDK)
#   PLUSARGS=+vcd=<file> (gate level)              dump nlc_core's nets for power (gl_dump.v)
#   NLC_LONG=1   T-ROB-1 (70 packets, ~1 h)        NLC_SEEDS=N   T-ROB-6 random runs (default 4)
#   CLK_NS=200   clock period (default 200 = 5 MHz, D2)
#   NLC_RESULTS=<dir>  JSON per test (default test/results/core)

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC = $(CURDIR)/../../src
COMPILE_ARGS += -g2012 -I$(SRC)
TOPLEVEL = nlc_core
COCOTB_TEST_MODULES ?= test_core
ifneq ($(GATES),yes)
SIM_BUILD = sim_build/rtl
VERILOG_SOURCES = $(addprefix $(SRC)/,nlc_core.v nlc_cfg.v nlc_slot_sel.v \
                  nlc_encoder.v nlc_lossy.sv nlc_lift53.sv nlc_rans.sv nlc_lossy_rom.v nlc_icg.sv)
else
NLC_PDK ?= /pdk
SIM_BUILD = sim_build/gl
VERILOG_SOURCES = $(NETLIST) $(NLC_PDK)/verilog/primitives.v $(NLC_PDK)/verilog/sky130_fd_sc_hd.v \
                  $(CURDIR)/gl_dump.v
COMPILE_ARGS += -DFUNCTIONAL -DUNIT_DELAY=\#1 -s gl_dump
endif
export PYTHONPATH := $(CURDIR)/../env:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
