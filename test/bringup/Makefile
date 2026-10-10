# Fault injection to validate the output-diff diagnosis tool (scripts/bringup/diagnose.py).
# Every test injects one fault, captures the output like the board would, runs the diagnoser
# and checks it names the right block. Real data (challenge recordings in data/raw, else
# synthetic). Two benches:
#   make -C test/bringup                       nlc_core in a test-only wrapper with fault hooks
#                                              (nlc_fault_top.v), 256-slot interface, test_faults
#   make -C test/bringup TOP=tt                the TT top (test/tb.v) at the pins: 32-slot frames,
#                                              1 slot / 2 clocks, config readback; test_pins
#   COCOTB_TEST_FILTER=f_a_dp2                 one fault
#   NLC_RESULTS=<dir>   captures, stimulus, diagnosis per fault (default test/results/bringup)

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
TOP ?= core
SRC = $(CURDIR)/../../src
COMPILE_ARGS += -g2012 -I$(SRC)
CORE_SRC = $(addprefix $(SRC)/,nlc_core.v nlc_cfg.v nlc_slot_sel.v nlc_encoder.v nlc_lossy.sv \
           nlc_lift53.sv nlc_rans.sv nlc_lossy_rom.v nlc_icg.sv)
ifeq ($(TOP),tt)
TOPLEVEL = tb
COCOTB_TEST_MODULES ?= test_pins
VERILOG_SOURCES = $(SRC)/project.v $(CORE_SRC) $(CURDIR)/../tb.v
SIM_BUILD = sim_build/tt
PLUSARGS += +vcd=/dev/null
else
TOPLEVEL = nlc_fault_top
COCOTB_TEST_MODULES ?= test_faults
VERILOG_SOURCES = $(CORE_SRC) $(CURDIR)/nlc_fault_top.v
SIM_BUILD = sim_build/core
endif
export PYTHONPATH := $(CURDIR)/../env:$(CURDIR)/..:$(CURDIR)/../../scripts/bringup:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
